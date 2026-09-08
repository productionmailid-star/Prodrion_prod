"""Tests for worker.py: dispatch, done/fail flow, and process_feedback."""

from __future__ import annotations

import threading
from dataclasses import asdict

import pytest

import feedback_platform as fp
import jobs
import worker


@pytest.fixture(autouse=True)
def _isolate(tmp_db):
    fp.VECTOR_STORE.index = None
    fp.VECTOR_STORE.ticket_keys = []
    yield


def _drain_one(worker_id="w1"):
    """Claim + dispatch + settle exactly one job the way run_forever would."""
    job = jobs.claim_next(worker_id)
    assert job is not None
    try:
        worker._dispatch(job)
    except Exception as exc:  # noqa: BLE001
        jobs.mark_failed(job["id"], f"{type(exc).__name__}: {exc}")
    else:
        jobs.mark_done(job["id"])
    return job["id"]


def test_process_feedback_job_runs_the_pipeline(tmp_db):
    fb = fp.StandardFeedbackObject(
        text="cannot log in via SSO", source="webhook", external_id="w-1"
    )
    jid = jobs.enqueue("process_feedback", asdict(fb))

    _drain_one()

    assert jobs.get_job(jid)["status"] == "done"
    with fp.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM feedback WHERE external_id='w-1'").fetchone()[0]
        labels = conn.execute("SELECT COUNT(*) FROM feedback_labels").fetchone()[0]
    assert n == 1
    assert labels >= 1  # fake LLM -> Uncategorized label row


def test_unknown_job_type_is_marked_failed(tmp_db):
    jid = jobs.enqueue("does_not_exist", {})
    _drain_one()
    job = jobs.get_job(jid)
    assert job["status"] in {"pending", "dead"}  # failed -> retry or bury
    assert "no handler" in job["last_error"]


def test_run_forever_stops_on_event(tmp_db):
    seen: list[str] = []

    @worker.handler("_test_noop")
    def _noop(payload):
        seen.append(payload["tag"])

    try:
        jobs.enqueue("_test_noop", {"tag": "a"})
        stop = threading.Event()
        t = threading.Thread(target=worker.run_forever, args=(stop,))
        t.start()
        # give the loop a moment to drain the single job, then stop it
        for _ in range(50):
            if seen:
                break
            threading.Event().wait(0.05)
        stop.set()
        t.join(timeout=5)
        assert not t.is_alive()
        assert seen == ["a"]
    finally:
        worker._HANDLERS.pop("_test_noop", None)
