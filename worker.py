"""Background worker: drains the jobs queue, including the self-rescheduling
periodic jobs (ingestion polling, bucket rebuilds, Jira/HubSpot sync, drift /
velocity checks — see the "*_tick" handlers below).

Run as its own process/service:

    python worker.py

It owns everything that must not block an HTTP request: the per-feedback
pipeline, the repo-analysis -> GitHub issue job, and all periodic work. The
API process only enqueues.
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


# ---- Periodic ("*_tick") jobs ---------------------------------------------
#
# Each tick does its work, then re-enqueues itself with a delay. A tick that
# finds its reason to exist gone (a disconnected hook) simply returns without
# re-enqueueing, ending its own chain.

_JIRA_SYNC_INTERVAL = 3600.0
_HUBSPOT_SYNC_INTERVAL = 6 * 3600.0
_VELOCITY_INTERVAL = 24 * 3600.0
_DRIFT_INTERVAL = 7 * 24 * 3600.0
_BUCKET_REBUILD_INTERVAL = 900.0


@handler("poll_tick")
def _poll_tick(payload: dict) -> None:
    import feedback_platform as fp
    import supabase_sync

    hook_id = payload["hook_id"]
    hook = supabase_sync.get_hook(hook_id)
    if hook is None or hook.get("status") != "connected":
        log.info("poll_tick: hook %s is gone/disconnected; ending its chain.", hook_id)
        return

    count = fp.poll_hook(hook)
    try:
        supabase_sync.update_hook_sync_status(hook_id, reviews_synced_delta=count)
        if count:
            supabase_sync.insert_activity_log(
                hook["workspace_id"],
                type="sync",
                icon="sync",
                title=f"Synced {count} new review(s)",
                description=f"{hook['type']} connector",
            )
    except Exception:
        log.exception("poll_tick: failed to record sync status for hook %s", hook_id)

    sync_settings = supabase_sync.get_sync_settings(hook["workspace_id"])
    interval = fp._parse_sync_frequency(sync_settings.get("sync_frequency"))
    jobs.enqueue(
        "poll_tick",
        {"hook_id": hook_id, "workspace_id": hook["workspace_id"]},
        delay_seconds=interval,
    )


@handler("rebuild_buckets_tick")
def _rebuild_buckets_tick(payload: dict) -> None:
    import feedback_platform as fp

    workspace_id = payload["workspace_id"]
    fp.rebuild_buckets(workspace_id)
    jobs.enqueue(
        "rebuild_buckets_tick",
        {"workspace_id": workspace_id},
        delay_seconds=_BUCKET_REBUILD_INTERVAL,
    )


@handler("jira_sync_tick")
def _jira_sync_tick(payload: dict) -> None:
    import feedback_platform as fp

    fp.sync_jira_tickets()
    jobs.enqueue("jira_sync_tick", {}, delay_seconds=_JIRA_SYNC_INTERVAL)


@handler("hubspot_sync_tick")
def _hubspot_sync_tick(payload: dict) -> None:
    import feedback_platform as fp

    fp.sync_hubspot_contacts()
    jobs.enqueue("hubspot_sync_tick", {}, delay_seconds=_HUBSPOT_SYNC_INTERVAL)


@handler("velocity_tick")
def _velocity_tick(payload: dict) -> None:
    import feedback_platform as fp

    fp.check_velocity_spikes()
    jobs.enqueue("velocity_tick", {}, delay_seconds=_VELOCITY_INTERVAL)


@handler("drift_tick")
def _drift_tick(payload: dict) -> None:
    import feedback_platform as fp

    fp.detect_taxonomy_drift()
    jobs.enqueue("drift_tick", {}, delay_seconds=_DRIFT_INTERVAL)


def seed_periodic_jobs() -> None:
    """Start (or resume) every periodic job chain, skipping any that's
    already pending/running — safe to call on every worker startup.
    """
    import supabase_sync

    for job_type in ("jira_sync_tick", "hubspot_sync_tick", "velocity_tick", "drift_tick"):
        if not jobs.has_active(job_type):
            jobs.enqueue(job_type, {})

    if not supabase_sync.configured():
        log.info("Supabase sync not configured; skipping per-workspace poll_tick seeding.")
        return

    seen_workspaces: set[str] = set()
    for hook in supabase_sync.list_connected_hooks():
        if not jobs.has_active("poll_tick", {"hook_id": hook["id"]}):
            jobs.enqueue("poll_tick", {"hook_id": hook["id"], "workspace_id": hook["workspace_id"]})
        seen_workspaces.add(hook["workspace_id"])

    for workspace_id in seen_workspaces:
        if not jobs.has_active("rebuild_buckets_tick", {"workspace_id": workspace_id}):
            jobs.enqueue("rebuild_buckets_tick", {"workspace_id": workspace_id})


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
    seed_periodic_jobs()  # resume/start every "*_tick" job chain

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    run_forever(stop)


if __name__ == "__main__":
    main()
