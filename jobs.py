"""Durable background-job queue backed by the SQLite `jobs` table.

Single-node design: no external broker. One or more `worker.py` processes call
:func:`claim_next` in a loop; the claim is serialised with ``BEGIN IMMEDIATE``
so two workers never take the same row.

All timestamps use SQLite's ``datetime('now')`` (UTC, ``YYYY-MM-DD HH:MM:SS``)
so ``run_after`` comparisons stay consistent.
"""

from __future__ import annotations

import json
import logging
import uuid

from config import settings
from db import get_conn

log = logging.getLogger("prodrion.jobs")

PENDING, RUNNING, DONE, FAILED, DEAD = "pending", "running", "done", "failed", "dead"


def enqueue(job_type: str, payload: dict, *, max_attempts: int | None = None) -> str:
    """Add a job and return its id."""
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, type, payload, status, max_attempts)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (
                job_id,
                job_type,
                json.dumps(payload),
                max_attempts or settings.job_max_attempts,
            ),
        )
    log.info("Enqueued job %s type=%s", job_id, job_type)
    return job_id


def claim_next(worker_id: str) -> dict | None:
    """Atomically move the oldest runnable job to `running` and return it."""
    conn = get_conn(autocommit=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM jobs
            WHERE status = 'pending' AND run_after <= datetime('now')
            ORDER BY created_at
            LIMIT 1
            """).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, "
            "updated_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    job = dict(row)
    job["attempts"] += 1
    job["status"] = RUNNING
    job["payload"] = json.loads(job["payload"])
    log.info("Claimed job %s by %s (attempt %d)", job["id"], worker_id, job["attempts"])
    return job


def mark_done(job_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', updated_at=datetime('now') WHERE id=?",
            (job_id,),
        )


def mark_failed(job_id: str, error: str) -> None:
    """Record a failure: reschedule with exponential backoff, or bury as `dead`."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return
        if row["attempts"] >= row["max_attempts"]:
            conn.execute(
                "UPDATE jobs SET status='dead', last_error=?, "
                "updated_at=datetime('now') WHERE id=?",
                (error[:2000], job_id),
            )
            log.error("Job %s is DEAD after %d attempts: %s", job_id, row["attempts"], error)
            return
        delay = int(
            min(
                settings.job_backoff_base_seconds * (2 ** (row["attempts"] - 1)),
                settings.job_backoff_max_seconds,
            )
        )
        conn.execute(
            """
            UPDATE jobs
            SET status='pending', last_error=?, updated_at=datetime('now'),
                run_after=datetime('now', ?)
            WHERE id=?
            """,
            (error[:2000], f"+{delay} seconds", job_id),
        )
        log.warning(
            "Job %s failed (attempt %d), retrying in %ds",
            job_id,
            row["attempts"],
            delay,
        )


def get_job(job_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"] = json.loads(job["payload"])
    return job


_LIST_COLUMNS = (
    "id, type, status, attempts, max_attempts, last_error, " "run_after, created_at, updated_at"
)


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM jobs WHERE status=? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
