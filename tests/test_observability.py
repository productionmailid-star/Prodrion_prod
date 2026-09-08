"""Tests for observability.py: request-id propagation, JSON logs, error bodies."""

from __future__ import annotations

import json
import logging

import pytest

import observability as obs

# --- JSON formatter ------------------------------------------------


def test_json_formatter_shape():
    fmt = obs._JsonFormatter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    rec.request_id = "abc123"
    rec.custom_field = "kept"
    out = json.loads(fmt.format(rec))
    assert out["level"] == "INFO"
    assert out["message"] == "hello world"
    assert out["request_id"] == "abc123"
    assert out["custom_field"] == "kept"


def test_json_formatter_includes_exception():
    fmt = obs._JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    out = json.loads(fmt.format(rec))
    assert "ValueError: boom" in out["exception"]


# --- request id context ------------------------------------------


def test_request_id_default_and_set():
    obs.set_request_id("-")
    assert obs.get_request_id() == "-"
    obs.set_request_id("req-1")
    assert obs.get_request_id() == "req-1"
    obs.set_request_id("-")


# --- middleware + exception handlers via the app -----------------


@pytest.fixture
def client(tmp_db):
    from fastapi.testclient import TestClient

    import feedback_platform as fp

    return TestClient(fp.app, raise_server_exceptions=False)


def test_response_carries_request_id_header(client):
    r = client.get("/health")
    assert r.headers.get("x-request-id")


def test_incoming_request_id_is_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-42"})
    assert r.headers["x-request-id"] == "trace-42"


def test_http_exception_body_shape(client):
    # unknown job id -> HTTPException(404) (auth disabled in dev via tmp_db)
    r = client.get("/jobs/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["type"] == "http_error"
    assert body["request_id"]


def test_validation_error_body_shape(client):
    # /query requires the `q` query param
    r = client.get("/query")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "validation_error"
    assert body["request_id"]


def test_unhandled_exception_returns_500_envelope(client, monkeypatch):
    import feedback_platform as fp

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    # tmp_db leaves api_auth_token blank -> auth is disabled in dev, so the
    # handler runs and the injected error hits the catch-all.
    monkeypatch.setattr(fp, "get_revenue_impact_report", _boom)
    r = client.get("/revenue-impact")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "internal_error"
    assert body["error"]["message"] == "internal server error"
    assert body["request_id"]
