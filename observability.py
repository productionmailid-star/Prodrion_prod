"""Logging, request-id propagation, and FastAPI exception handling.

* :func:`configure_logging` — idempotent root-logger setup (text or JSON,
  level from config, optional Sentry).
* :class:`RequestIdMiddleware` — assigns/propagates ``X-Request-ID`` and makes
  it available to every log record via a ContextVar.
* :func:`install_exception_handlers` — consistent JSON error bodies that never
  leak internals and always carry the request id.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar

from config import settings

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

_STD_LOGRECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> None:
    _request_id.set(value)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "service": settings.service_name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STD_LOGRECORD_KEYS and key not in payload:
                payload[key] = value
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(force: bool = False) -> None:
    """Attach a single stdout handler to the root logger. Safe to call twice."""
    global _configured
    if _configured and not force:
        return
    _configured = True

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    if settings.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.environment.value,
                release=None,
            )
            logging.getLogger("prodrion").info("Sentry error reporting enabled.")
        except Exception as exc:  # pragma: no cover - depends on optional dep
            logging.getLogger("prodrion").warning("Sentry init failed: %s", exc)


class RequestIdMiddleware:
    """Pure-ASGI: set the request-id ContextVar and echo it on the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id")
        rid = incoming.decode("latin-1")[:128] if incoming else uuid.uuid4().hex
        token = _request_id.set(rid)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", rid.encode("latin-1")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _request_id.reset(token)


def install_exception_handlers(app) -> None:
    from fastapi import HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    log = logging.getLogger("prodrion.api")

    def _body(status: int, err_type: str, message) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "error": {"type": err_type, "message": message},
                "request_id": get_request_id(),
            },
        )

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException):
        return _body(exc.status_code, "http_error", exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError):
        return _body(422, "validation_error", exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _body(500, "internal_error", "internal server error")
