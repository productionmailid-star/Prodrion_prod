"""Reusable security helpers for the FastAPI services.

* bearer-token auth for the admin/query API
* shared-secret / Slack-signature verification for webhooks
* SSRF guard for server-side URL fetches
* a Content-Length request-size limiter

Every function reads configuration through ``config.get_settings()`` so tests
can swap it via ``get_settings.cache_clear()``.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import socket
import time
from hashlib import sha256
from urllib.parse import urlparse

from fastapi import Header, HTTPException, status

from config import get_settings

log = logging.getLogger("prodrion.security")

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        log.warning(message)


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Bearer-token auth (admin / query endpoints)
# ---------------------------------------------------------------------------


async def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency enforcing ``Authorization: Bearer <api_auth_token>``.

    When no token is configured and the environment is not production, the
    request is allowed (with a one-time warning) to keep local dev frictionless.
    """
    settings = get_settings()
    expected = settings.api_auth_token

    if not expected:
        if settings.is_production:  # pragma: no cover - blocked by config fail-fast
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "server auth is misconfigured"
            )
        _warn_once("PRODRION_API_AUTH_TOKEN is not set - API auth is DISABLED (dev only).")
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _eq(authorization[len("Bearer ") :].strip(), expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid bearer token")


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------


def verify_webhook_secret(provided: str | None) -> None:
    """Check the ``X-Webhook-Secret`` header for POST /webhook/feedback."""
    settings = get_settings()
    expected = settings.webhook_secret

    if not expected:
        if settings.is_production:  # pragma: no cover - blocked by config fail-fast
            raise HTTPException(500, "webhook secret is misconfigured")
        _warn_once("PRODRION_WEBHOOK_SECRET is not set - webhook auth is DISABLED (dev only).")
        return

    if not provided or not _eq(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")


def verify_slack_signature(body: bytes, timestamp: str | None, signature: str | None) -> None:
    """Verify a Slack request signature (X-Slack-Signature / -Request-Timestamp)."""
    settings = get_settings()
    secret = settings.slack_signing_secret

    if not secret:
        if settings.is_production:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "slack endpoint disabled: no signing secret configured",
            )
        _warn_once(
            "PRODRION_SLACK_SIGNING_SECRET is not set - Slack signature check "
            "DISABLED (dev only)."
        )
        return

    if not timestamp or not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Slack signature headers")
    try:
        skew = abs(time.time() - float(timestamp))
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad Slack timestamp") from None
    if skew > 300:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale Slack request")

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = "v0=" + hmac.new(secret.encode("utf-8"), basestring, sha256).hexdigest()
    if not _eq(digest, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad Slack signature")


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def is_safe_public_url(url: str) -> bool:
    """True only if ``url`` is http(s) and every resolved IP is public.

    Note: this is a best-effort check. It does not defend against DNS rebinding
    between this call and a later fetch.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# Request-size limiter (ASGI middleware)
# ---------------------------------------------------------------------------


class RequestSizeLimitMiddleware:
    """Reject HTTP requests whose declared Content-Length exceeds the cap."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers") or []:
                if name == b"content-length":
                    try:
                        too_big = int(value) > self.max_bytes
                    except ValueError:
                        too_big = False
                    if too_big:
                        await send(
                            {
                                "type": "http.response.start",
                                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                "headers": [(b"content-type", b"application/json")],
                            }
                        )
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b'{"detail":"request body too large"}',
                            }
                        )
                        return
        await self.app(scope, receive, send)
