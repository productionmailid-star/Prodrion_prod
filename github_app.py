"""GitHub App integration: auth, sandboxed clone, issue creation.

Only the app private key is a secret at rest. Installation access tokens are
short-lived (1h); we fetch them on demand and cache them in memory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from config import settings

log = logging.getLogger("prodrion.github")

# installation_id -> (token, expiry_epoch)
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_MARGIN = 300  # refresh 5 min before expiry


class GitHubError(RuntimeError):
    pass


def _private_key() -> str:
    key = settings.github_app_private_key
    if not key:
        raise GitHubError("PRODRION_GITHUB_APP_PRIVATE_KEY is not configured")
    if "BEGIN" not in key and os.path.isfile(key):
        return Path(key).read_text()
    return key


def app_jwt(now: int | None = None) -> str:
    """A short-lived RS256 JWT identifying the GitHub App."""
    import jwt  # PyJWT

    if not settings.github_app_id:
        raise GitHubError("PRODRION_GITHUB_APP_ID is not configured")
    now = int(time.time()) if now is None else now
    payload = {"iat": now - 60, "exp": now + 600, "iss": settings.github_app_id}
    return jwt.encode(payload, _private_key(), algorithm="RS256")


def installation_token(installation_id: str) -> str:
    """Return a cached (or freshly minted) installation access token."""
    cached = _token_cache.get(installation_id)
    if cached and cached[1] - _TOKEN_TTL_MARGIN > time.time():
        return cached[0]

    resp = requests.post(
        f"{settings.github_api_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise GitHubError(f"installation token request failed: {resp.status_code} {resp.text}")
    data = resp.json()
    # expires_at like "2026-01-01T01:00:00Z"
    expiry = time.time() + 3300
    _token_cache[installation_id] = (data["token"], expiry)
    return data["token"]


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def clone_repo(installation_id: str, repo_full_name: str, dest: Path) -> Path:
    """Shallow-clone a repo into ``dest`` (which must sit under the sandbox base).

    Nothing from the cloned tree is executed; the caller only reads it.
    """
    base = Path(settings.github_clone_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    if base not in dest.parents:
        raise GitHubError(f"clone destination must be inside {base}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    token = installation_token(installation_id)
    url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                "--config",
                "submodule.recurse=false",
                url,
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=settings.github_clone_timeout_seconds,
        )
    except subprocess.CalledProcessError as exc:
        raise GitHubError(
            f"git clone failed: {exc.stderr.decode('utf-8', 'ignore')[:500]}"
        ) from None
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitHubError("git clone timed out") from None

    size = _dir_size(dest)
    if size > settings.github_clone_max_bytes:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitHubError(
            f"cloned repo is {size} bytes, over the {settings.github_clone_max_bytes} cap"
        )
    log.info("Cloned %s into %s (%d bytes)", repo_full_name, dest, size)
    return dest


def create_issue(installation_id: str, repo_full_name: str, title: str, body: str) -> dict:
    """Open an issue (the 'ticket') on the user's repo."""
    resp = requests.post(
        f"{settings.github_api_url}/repos/{repo_full_name}/issues",
        headers={
            "Authorization": f"Bearer {installation_token(installation_id)}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": body},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise GitHubError(f"create issue failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    log.info("Opened issue %s#%s", repo_full_name, data.get("number"))
    return {"number": data.get("number"), "url": data.get("html_url")}


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    """Verify an ``X-Hub-Signature-256`` header from a GitHub App webhook."""
    import hashlib
    import hmac

    secret = settings.github_webhook_secret
    if not secret:
        return not settings.is_production
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def clear_token_cache() -> None:
    _token_cache.clear()
