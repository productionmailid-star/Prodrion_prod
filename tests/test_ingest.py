"""Milestone 4: idempotent ingestion, normalised labels, per-label rollups."""

from __future__ import annotations

import pytest

import feedback_platform as fp


@pytest.fixture
def db(tmp_db):
    return fp


def _fb(**kw):
    base = {"text": "login broken", "source": "webhook", "labels": ["Auth > Login"]}
    base.update(kw)
    return fp.StandardFeedbackObject(**base)


# --- external_id on normalisers ----------------------------------------


def test_normalisers_set_external_id():
    assert fp.Normalizer.from_zendesk({"id": 55, "description": "x"}).external_id == "55"
    assert fp.Normalizer.from_play_store({"reviewId": "gp1", "content": "x"}).external_id == "gp1"
    assert (
        fp.Normalizer.from_slack({"text": "x", "ts": "1700000000.1"}).external_id == "1700000000.1"
    )
    # app store has no id field -> a stable hash, identical for identical input
    a = fp.Normalizer.from_app_store({"userName": "k", "date": "d", "title": "t"})
    b = fp.Normalizer.from_app_store({"userName": "k", "date": "d", "title": "t"})
    assert a.external_id == b.external_id and len(a.external_id) == 40


# --- save_feedback dedup + labels -----------------------------------


def test_save_feedback_dedups_on_source_external_id(db):
    db.save_feedback(_fb(source="zendesk", external_id="Z1", labels=["A"]))
    db.save_feedback(_fb(source="zendesk", external_id="Z1", labels=["A", "B"]))
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE source='zendesk' AND external_id='Z1'"
        ).fetchone()[0]
    assert n == 1


def test_save_feedback_without_external_id_is_not_deduped(db):
    db.save_feedback(_fb(external_id=""))
    db.save_feedback(_fb(external_id=""))
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


def test_save_feedback_populates_feedback_labels(db):
    fb = db.save_feedback(_fb(external_id="W1", labels=["Auth > Login", "Support > Quality"]))
    with db.get_conn() as conn:
        labels = {
            r["label"]
            for r in conn.execute("SELECT label FROM feedback_labels WHERE feedback_id=?", (fb.id,))
        }
    assert labels == {"Auth > Login", "Support > Quality"}


def test_embed_model_is_recorded(db):
    fb = db.save_feedback(_fb(external_id="W2"))
    with db.get_conn() as conn:
        row = conn.execute("SELECT embed_model FROM feedback WHERE id=?", (fb.id,)).fetchone()
    assert row["embed_model"] == "fake"


# --- per-label revenue rollup -------------------------------------


def test_revenue_report_aggregates_per_label(db):
    db.save_feedback(
        _fb(external_id="a", user_email="u1@x.com", arr=100.0, labels=["Auth > Login", "Billing"])
    )
    db.save_feedback(_fb(external_id="b", user_email="u2@x.com", arr=50.0, labels=["Auth > Login"]))
    report = {r["label"]: r for r in db.get_revenue_impact_report()}

    assert report["Auth > Login"]["feedback_count"] == 2
    assert report["Auth > Login"]["user_count"] == 2
    assert report["Auth > Login"]["total_arr"] == 150.0
    assert report["Billing"]["feedback_count"] == 1
    assert "Uncategorized" not in "".join(report)


def test_revenue_report_excludes_uncategorized(db):
    db.save_feedback(_fb(external_id="c", labels=[fp.UNCATEGORIZED_LABEL]))
    assert db.get_revenue_impact_report() == []


# --- sync cursor ------------------------------------------------


def test_sync_cursor_roundtrip(db):
    assert db.get_sync_cursor("zendesk") is None
    db.set_sync_cursor("zendesk", "2026-01-01T00:00:00Z")
    assert db.get_sync_cursor("zendesk") == "2026-01-01T00:00:00Z"
    db.set_sync_cursor("zendesk", "2026-02-01T00:00:00Z")
    assert db.get_sync_cursor("zendesk") == "2026-02-01T00:00:00Z"
