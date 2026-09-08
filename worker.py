"""Background worker: drains the jobs queue and runs the periodic scheduler.

Run as its own process/service:

    python worker.py

It owns everything that must not block an HTTP request: the per-feedback
pipeline (via the jobs queue) and the APScheduler polling / drift / velocity
jobs. The API process only enqueues.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

import jobs
from config import settings
from db import init_db
from observability import configure_logging, set_request_id

log = logging.getLogger("prodrion.worker")

# job type -> handler(payload: dict) -> None
_HANDLERS: dict = {}


def handler(job_type: str):
    def _register(fn):
        _HANDLERS[job_type] = fn
        return fn

    return _register


@handler("process_feedback")
def _process_feedback(payload: dict) -> None:
    import feedback_platform as fp

    fp.pipeline(fp.StandardFeedbackObject(**payload))


def _issue_body(goal: str, result: dict) -> str:
    lines = [
        f"**Goal:** {goal}",
        "",
        f"Files to review: {', '.join(result['plan']['files_to_modify']) or '(none)'}",
        "",
        "_AI-generated suggestion — review before applying._",
        "",
    ]
    for i, entry in enumerate(result.get("results", []), 1):
        lines.append(f"### Change {i}")
        for w in entry.get("warnings", []):
            lines.append(f"> ⚠ {w}")
        lines.append("```diff")
        lines.append(entry.get("patch", "").strip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


@handler("analyze_repo")
def _analyze_repo(payload: dict) -> None:
    import shutil
    from pathlib import Path

    import github_app
    import repo_agent_core

    inst = str(payload["installation_id"])
    repo = payload["repo_full_name"]
    goal = payload["goal"]
    safe = repo.replace("/", "__")
    dest = Path(settings.github_clone_dir).resolve() / inst / safe

    github_app.clone_repo(inst, repo, dest)
    try:
        result = repo_agent_core.analyze(str(dest), goal)
    finally:
        shutil.rmtree(dest, ignore_errors=True)

    if result["status"] != "ok":
        raise RuntimeError(f"repo analysis failed: {result.get('error', result['status'])}")

    issue = github_app.create_issue(
        inst, repo, f"[Prodrion] {goal[:70]}", _issue_body(goal, result)
    )
    log.info("analyze_repo -> %s issue #%s", repo, issue.get("number"))


def _dispatch(job: dict) -> None:
    fn = _HANDLERS.get(job["type"])
    if fn is None:
        raise RuntimeError(f"no handler registered for job type {job['type']!r}")
    fn(job["payload"])


def run_forever(stop: threading.Event, worker_id: str | None = None) -> None:
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    idle = settings.worker_poll_interval_seconds
    log.info("Worker %s started; handlers=%s", worker_id, sorted(_HANDLERS))

    while not stop.is_set():
        try:
            job = jobs.claim_next(worker_id)
        except Exception:
            log.exception("claim_next failed; backing off")
            stop.wait(idle)
            continue

        if job is None:
            stop.wait(idle)
            continue

        set_request_id(job["id"])
        try:
            _dispatch(job)
        except Exception as exc:
            log.exception("Job %s (%s) failed", job["id"], job["type"])
            jobs.mark_failed(job["id"], f"{type(exc).__name__}: {exc}")
        else:
            jobs.mark_done(job["id"])
            log.info("Job %s (%s) done", job["id"], job["type"])
        finally:
            set_request_id("-")

    log.info("Worker %s stopped", worker_id)


def main() -> None:
    configure_logging()
    log.info("=" * 60)
    log.info("  PRODRION WORKER")
    log.info("=" * 60)

    init_db()

    import feedback_platform as fp

    fp.EmbeddingEngine.get()  # preload embedder
    fp.sync_jira_tickets()  # build the in-process vector index
    scheduler = fp.start_scheduler()  # periodic polling + drift/velocity

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    try:
        run_forever(stop)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
