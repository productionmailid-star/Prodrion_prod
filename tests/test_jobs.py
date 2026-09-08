"""Tests for the SQLite-backed job queue (jobs.py)."""

from __future__ import annotations

import jobs


def test_enqueue_and_get(tmp_db):
    jid = jobs.enqueue("demo", {"n": 1})
    job = jobs.get_job(jid)
    assert job["type"] == "demo"
    assert job["status"] == "pending"
    assert job["attempts"] == 0
    assert job["payload"] == {"n": 1}


def test_claim_moves_to_running_and_is_exclusive(tmp_db):
    jobs.enqueue("demo", {"n": 1})
    first = jobs.claim_next("w1")
    assert first["status"] == "running"
    assert first["attempts"] == 1
    assert first["payload"] == {"n": 1}
    assert jobs.claim_next("w2") is None  # nothing left to claim


def test_mark_done(tmp_db):
    jid = jobs.enqueue("demo", {})
    jobs.claim_next("w1")
    jobs.mark_done(jid)
    assert jobs.get_job(jid)["status"] == "done"


def test_mark_failed_retries_with_backoff(tmp_db):
    jid = jobs.enqueue("demo", {}, max_attempts=3)
    jobs.claim_next("w1")
    jobs.mark_failed(jid, "boom")
    job = jobs.get_job(jid)
    assert job["status"] == "pending"
    assert job["last_error"] == "boom"
    assert job["run_after"] > job["created_at"]  # scheduled into the future


def test_mark_failed_buries_after_max_attempts(tmp_db):
    jid = jobs.enqueue("demo", {}, max_attempts=1)
    jobs.claim_next("w1")  # attempts -> 1
    jobs.mark_failed(jid, "boom")
    assert jobs.get_job(jid)["status"] == "dead"
    assert [j["id"] for j in jobs.list_jobs(status="dead")] == [jid]


def test_backoff_delays_reclaim(tmp_db):
    jid = jobs.enqueue("demo", {}, max_attempts=5)
    jobs.claim_next("w1")
    jobs.mark_failed(jid, "boom")
    # run_after is a few seconds out (default backoff), so not immediately claimable
    assert jobs.claim_next("w2") is None
