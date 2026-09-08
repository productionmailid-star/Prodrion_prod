"""Tests for the credit ledger (credits.py) and its API wiring."""

from __future__ import annotations

import pytest

import credits
from credits import InsufficientCredits

# --- ledger core ------------------------------------------------


def test_grant_updates_balance_and_ledger(tmp_db):
    assert credits.get_balance("w1") == 0
    assert credits.grant("w1", 100, "seed") == 100
    assert credits.get_balance("w1") == 100
    row = credits.ledger("w1")[0]
    assert row["delta"] == 100
    assert row["balance_after"] == 100
    assert row["reason"] == "seed"


def test_grant_rejects_non_positive(tmp_db):
    with pytest.raises(ValueError):
        credits.grant("w1", 0, "x")


def test_charge_is_noop_when_disabled(tmp_db):
    credits.grant("w1", 10, "seed")
    assert credits.settings.credits_enabled is False
    assert credits.charge("w1", 5, "use") == 10
    assert credits.get_balance("w1") == 10
    assert len(credits.ledger("w1")) == 1  # only the grant recorded


def test_charge_deducts_when_enabled(tmp_db, monkeypatch):
    monkeypatch.setattr(credits.settings, "credits_enabled", True)
    credits.grant("w1", 10, "seed")
    assert credits.charge("w1", 4, "ingest") == 6
    assert credits.get_balance("w1") == 6


def test_charge_raises_and_does_not_deduct_when_insufficient(tmp_db, monkeypatch):
    monkeypatch.setattr(credits.settings, "credits_enabled", True)
    credits.grant("w1", 3, "seed")
    with pytest.raises(InsufficientCredits):
        credits.charge("w1", 5, "ingest")
    assert credits.get_balance("w1") == 3


def test_sequential_charges_cannot_overdraw(tmp_db, monkeypatch):
    monkeypatch.setattr(credits.settings, "credits_enabled", True)
    credits.grant("w1", 5, "seed")
    credits.charge("w1", 3, "a")
    with pytest.raises(InsufficientCredits):
        credits.charge("w1", 3, "b")
    assert credits.get_balance("w1") == 2


# --- API wiring -----------------------------------------------


@pytest.fixture
def client(tmp_db, monkeypatch):
    monkeypatch.setattr(credits.settings, "credits_enabled", True)
    from fastapi.testclient import TestClient

    import feedback_platform as fp

    return fp, TestClient(fp.app)


def test_webhook_returns_402_when_out_of_credits(client):
    _fp, c = client
    r = c.post("/webhook/feedback", json={"text": "hi"})
    assert r.status_code == 402


def test_grant_endpoint_then_webhook_and_balance(client):
    _fp, c = client
    g = c.post("/admin/credits/grant", json={"amount": 5, "reason": "promo"})
    assert g.status_code == 200 and g.json()["balance"] == 5

    r = c.post("/webhook/feedback", json={"text": "cannot log in"})
    assert r.status_code == 202

    info = c.get("/credits").json()
    assert info["enabled"] is True
    assert info["balance"] == 4
    assert info["ledger"][0]["reason"] == "ingest_feedback"


def test_stripe_stub_grants_credits(client):
    _fp, c = client
    r = c.post("/webhook/stripe", json={"credits": 20})
    assert r.status_code == 200
    assert c.get("/credits").json()["balance"] == 20
