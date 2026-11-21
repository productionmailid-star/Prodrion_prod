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


# --- credential scrubbing ---------------------------------


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def clone_with_token(tmp_path):
    """A real clone whose git metadata holds an access token, as a
    `git clone https://x-access-token:<token>@github.com/...` leaves it."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    token = "ghs_SECRETTOKEN123"
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", cwd=origin)
    (origin / "app.py").write_text("x = 1\n")
    _git("add", ".", cwd=origin)
    _git("commit", "-q", "-m", "init", cwd=origin)

    dest = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(dest), cwd=tmp_path)
    url = f"https://x-access-token:{token}@github.com/acme/app.git"
    _git("remote", "set-url", "origin", url, cwd=dest)
    (dest / ".git" / "logs").mkdir(exist_ok=True)
    (dest / ".git" / "logs" / "HEAD").write_text(f"0000 1111 t <t> 0 +0000\tclone: from {url}\n")
    return dest, token


def _files_containing(root, needle):
    return [p for p in root.rglob("*") if p.is_file() and needle.encode() in p.read_bytes()]


def test_clone_credentials_are_scrubbed(clone_with_token):
    dest, token = clone_with_token
    assert _files_containing(dest, token)  # the leak is real before scrubbing

    github_app._scrub_clone_credentials(dest, "acme/app", token)

    assert _files_containing(dest, token) == []
    assert (dest / ".git").is_dir() and (dest / "app.py").exists()
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=dest, capture_output=True, text=True
    ).stdout.strip()
    assert remote == "https://github.com/acme/app.git"


def test_scrub_removes_git_dir_if_the_token_survives_elsewhere(clone_with_token):
    dest, token = clone_with_token
    (dest / ".git" / "FETCH_HEAD").write_text(f"abc\t\thttps://x:{token}@github.com/acme/app\n")

    github_app._scrub_clone_credentials(dest, "acme/app", token)

    assert not (dest / ".git").exists()
    assert (dest / "app.py").read_text() == "x = 1\n"
