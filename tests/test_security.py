"""Tests for security.py helpers and the auth wiring on the FastAPI app."""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

import pytest
from fastapi import HTTPException

import security
from config import Settings


def _settings(**overrides) -> Settings:
    base = {"environment": "development", "llm_provider": "fake"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def use_settings(monkeypatch):
    """Point security.get_settings() at an explicit Settings for the test."""

    def _apply(**overrides):
        s = _settings(**overrides)
        monkeypatch.setattr(security, "get_settings", lambda: s)
        return s

    return _apply


# --- require_bearer_token -------------------------------------------------


async def test_bearer_disabled_in_dev_when_no_token(use_settings):
    use_settings(api_auth_token="")
    assert await security.require_bearer_token(authorization=None) is None


async def test_bearer_accepts_correct_token(use_settings):
    use_settings(api_auth_token="sekret")
    assert await security.require_bearer_token(authorization="Bearer sekret") is None


async def test_bearer_rejects_missing_header(use_settings):
    use_settings(api_auth_token="sekret")
    with pytest.raises(HTTPException) as exc:
        await security.require_bearer_token(authorization=None)
    assert exc.value.status_code == 401


async def test_bearer_rejects_wrong_token(use_settings):
    use_settings(api_auth_token="sekret")
    with pytest.raises(HTTPException) as exc:
        await security.require_bearer_token(authorization="Bearer nope")
    assert exc.value.status_code == 403


# --- verify_webhook_secret --------------------------------------------


def test_webhook_secret_disabled_in_dev(use_settings):
    use_settings(webhook_secret="")
    security.verify_webhook_secret(None)  # no raise


def test_webhook_secret_match(use_settings):
    use_settings(webhook_secret="hook")
    security.verify_webhook_secret("hook")


def test_webhook_secret_mismatch(use_settings):
    use_settings(webhook_secret="hook")
    with pytest.raises(HTTPException) as exc:
        security.verify_webhook_secret("wrong")
    assert exc.value.status_code == 401


# --- verify_slack_signature ------------------------------------------


def _slack_sig(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, sha256).hexdigest()


def test_slack_signature_valid(use_settings):
    use_settings(slack_signing_secret="s3cr3t")
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    security.verify_slack_signature(body, ts, _slack_sig("s3cr3t", ts, body))


def test_slack_signature_tampered(use_settings):
    use_settings(slack_signing_secret="s3cr3t")
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    with pytest.raises(HTTPException):
        security.verify_slack_signature(body, ts, _slack_sig("s3cr3t", ts, b"other"))


def test_slack_signature_stale_timestamp(use_settings):
    use_settings(slack_signing_secret="s3cr3t")
    body = b"{}"
    ts = str(int(time.time()) - 4000)
    with pytest.raises(HTTPException):
        security.verify_slack_signature(body, ts, _slack_sig("s3cr3t", ts, body))


def test_slack_signature_skipped_in_dev_without_secret(use_settings):
    use_settings(slack_signing_secret="")
    security.verify_slack_signature(b"{}", None, None)  # no raise


# --- is_safe_public_url (SSRF guard) ---------------------------------


def _fake_getaddrinfo(ip):
    return lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))]


def test_ssrf_rejects_non_http():
    assert security.is_safe_public_url("ftp://example.com/x") is False
    assert security.is_safe_public_url("file:///etc/passwd") is False


def test_ssrf_rejects_loopback_and_link_local(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert security.is_safe_public_url("http://internal.example/") is False
    monkeypatch.setattr(security.socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    assert security.is_safe_public_url("http://metadata/") is False


def test_ssrf_allows_public_ip(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert security.is_safe_public_url("https://example.com/docs") is True


# --- RequestSizeLimitMiddleware ------------------------------------


async def test_size_limit_rejects_large_content_length():
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    mw = security.RequestSizeLimitMiddleware(inner, max_bytes=100)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await mw({"type": "http", "headers": [(b"content-length", b"9999")]}, receive, send)
    assert inner_called is False
    assert sent[0]["status"] == 413


async def test_size_limit_passes_small_request():
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True

    mw = security.RequestSizeLimitMiddleware(inner, max_bytes=100)
    await mw(
        {"type": "http", "headers": [(b"content-length", b"10")]},
        lambda: None,
        lambda m: None,
    )
    assert inner_called is True


# --- app-level auth wiring --------------------------------------------


@pytest.fixture
def api(tmp_db, monkeypatch):
    import db as _db
    import feedback_platform as fp
    import jobs as _jobs

    # tmp_db already wired a fresh DB; overlay the auth secrets for these tests.
    s = _settings(
        db_path=tmp_db.db_path,
        api_auth_token="sekret",
        webhook_secret="hook",
    )
    for mod in (_db, _jobs, fp):
        monkeypatch.setattr(mod, "settings", s)
    monkeypatch.setattr(security, "get_settings", lambda: s)

    from fastapi.testclient import TestClient

    return fp, TestClient(fp.app)


def test_health_is_open(api):
    _fp, client = api
    assert client.get("/health").status_code == 200


def test_taxonomy_requires_auth(api):
    _fp, client = api
    assert client.get("/taxonomy").status_code == 401
    assert client.get("/taxonomy", headers={"Authorization": "Bearer nope"}).status_code == 403
    ok = client.get("/taxonomy", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
    assert ok.json() == {"taxonomy": []}


def test_webhook_feedback_requires_secret(api):
    fp, client = api
    assert client.post("/webhook/feedback", json={"text": "hi"}).status_code == 401
    ok = client.post(
        "/webhook/feedback",
        json={"text": "cannot log in", "email": "a@b.com"},
        headers={"X-Webhook-Secret": "hook"},
    )
    assert ok.status_code == 202
    body = ok.json()
    assert body["status"] == "queued"
    # the item is now a pending job, not yet processed
    job = fp.jobs.get_job(body["job_id"])
    assert job["type"] == "process_feedback" and job["status"] == "pending"


def test_import_users_accepts_csv_body(api):
    fp, client = api
    csv_body = "email,user_id,arr,plan_type,company\na@b.com,u1,1200,pro,Acme\n"
    resp = client.post(
        "/import-users",
        content=csv_body,
        headers={"Authorization": "Bearer sekret", "Content-Type": "text/csv"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "imported", "rows": 1}
    with fp.get_conn() as conn:
        row = conn.execute("SELECT arr FROM users WHERE email='a@b.com'").fetchone()
    assert row["arr"] == 1200.0


# --- text-to-SQL fencing (/query) -----------------------------------


def _query(client, canned_sql, fp, monkeypatch):
    from llm.fake import FakeLLM

    monkeypatch.setattr(fp, "get_llm", lambda: FakeLLM(canned=canned_sql))
    return client.get(
        "/query", params={"q": "anything"}, headers={"Authorization": "Bearer sekret"}
    )


def test_query_runs_select_and_caps_rows(api, monkeypatch):
    fp, client = api
    resp = _query(client, "SELECT * FROM taxonomy", fp, monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_query_rejects_non_select(api, monkeypatch):
    fp, client = api
    resp = _query(client, "DROP TABLE feedback", fp, monkeypatch)
    assert "error" in resp.json()
    # table still there
    with fp.get_conn() as conn:
        conn.execute("SELECT 1 FROM feedback LIMIT 1")


def test_query_rejects_pragma_and_multi_statement(api, monkeypatch):
    fp, client = api
    assert "error" in _query(client, "PRAGMA table_info(users)", fp, monkeypatch).json()
    # second statement after ';' is dropped, first still runs
    resp = _query(client, "SELECT 1 AS n; DROP TABLE feedback", fp, monkeypatch)
    assert resp.json().get("rows") == [{"n": 1}]
    with fp.get_conn() as conn:
        conn.execute("SELECT 1 FROM feedback LIMIT 1")


def test_query_connection_is_read_only(api, monkeypatch):
    fp, client = api
    resp = _query(client, "INSERT INTO users (email) VALUES ('x@y.com')", fp, monkeypatch)
    assert "error" in resp.json()
