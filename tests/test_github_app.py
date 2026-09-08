"""Tests for github_app.py (JWT, token cache, issue creation, clone guards)."""

from __future__ import annotations

import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_app
from github_app import GitHubError


@pytest.fixture(scope="session")
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


@pytest.fixture
def gh(rsa_keys, monkeypatch):
    priv, _pub = rsa_keys
    monkeypatch.setattr(github_app.settings, "github_app_id", "12345")
    monkeypatch.setattr(github_app.settings, "github_app_private_key", priv)
    monkeypatch.setattr(github_app.settings, "github_api_url", "https://api.github.test")
    github_app.clear_token_cache()
    yield github_app
    github_app.clear_token_cache()


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


# --- app JWT ---------------------------------------------------


def test_app_jwt_round_trips(gh, rsa_keys):
    import jwt

    _priv, pub = rsa_keys
    token = gh.app_jwt(now=1_000_000)
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345"
    assert claims["exp"] == 1_000_600
    assert claims["iat"] == 1_000_000 - 60


def test_app_jwt_requires_config(monkeypatch):
    monkeypatch.setattr(github_app.settings, "github_app_id", "")
    with pytest.raises(GitHubError):
        github_app.app_jwt()


# --- installation token cache -------------------------------


def test_installation_token_is_cached(gh, monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _Resp(201, {"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"})

    monkeypatch.setattr(github_app.requests, "post", fake_post)
    assert gh.installation_token("42") == "ghs_abc"
    assert gh.installation_token("42") == "ghs_abc"
    assert len(calls) == 1  # second call served from cache


def test_installation_token_error(gh, monkeypatch):
    monkeypatch.setattr(github_app.requests, "post", lambda *a, **k: _Resp(404, {"message": "no"}))
    with pytest.raises(GitHubError):
        gh.installation_token("42")


# --- create issue -----------------------------------------


def test_create_issue(gh, monkeypatch):
    monkeypatch.setattr(gh, "installation_token", lambda _i: "ghs_abc")
    monkeypatch.setattr(
        github_app.requests,
        "post",
        lambda *a, **k: _Resp(201, {"number": 7, "html_url": "https://gh/x/7"}),
    )
    out = gh.create_issue("42", "acme/app", "title", "body")
    assert out == {"number": 7, "url": "https://gh/x/7"}


# --- webhook signature ----------------------------------


def test_verify_webhook_signature(monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setattr(github_app.settings, "github_webhook_secret", "s3cr3t")
    body = b'{"action":"created"}'
    good = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert github_app.verify_webhook_signature(body, good) is True
    assert github_app.verify_webhook_signature(body, "sha256=deadbeef") is False
    assert github_app.verify_webhook_signature(body, None) is False


def test_verify_webhook_signature_dev_without_secret(monkeypatch):
    monkeypatch.setattr(github_app.settings, "github_webhook_secret", "")
    monkeypatch.setattr(github_app.settings, "environment", github_app.settings.environment)
    assert github_app.verify_webhook_signature(b"x", None) is True


# --- clone guards --------------------------------------


def test_clone_rejects_dest_outside_sandbox(gh, tmp_path, monkeypatch):
    monkeypatch.setattr(github_app.settings, "github_clone_dir", str(tmp_path / "sandbox"))
    monkeypatch.setattr(gh, "installation_token", lambda _i: "ghs_abc")
    with pytest.raises(GitHubError, match="inside"):
        gh.clone_repo("42", "acme/app", tmp_path / "elsewhere" / "app")


def test_clone_enforces_size_cap(gh, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(github_app.settings, "github_clone_dir", str(sandbox))
    monkeypatch.setattr(github_app.settings, "github_clone_max_bytes", 1_000_000)
    monkeypatch.setattr(gh, "installation_token", lambda _i: "ghs_abc")

    def fake_run(cmd, **kw):
        dest = cmd[-1]
        import os

        os.makedirs(dest, exist_ok=True)
        with open(f"{dest}/big.bin", "wb") as f:
            f.write(b"0" * 2_000_000)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(github_app.subprocess, "run", fake_run)
    dest = sandbox / "42" / "acme__app"
    with pytest.raises(GitHubError, match="over the"):
        gh.clone_repo("42", "acme/app", dest)
    assert not dest.exists()  # cleaned up
