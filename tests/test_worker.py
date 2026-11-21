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
    """Claim + run exactly one job the way run_forever would."""
    job = jobs.claim_next(worker_id)
    assert job is not None
    worker.run_job(job)
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


DIFF = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
    " def f():\n-    return 1\n+    return 2\n"
)


def _ok_result(diff=DIFF):
    return {
        "status": "ok",
        "summary": "Return the right value.",
        "root_cause": "f() returned a stale constant.",
        "diff": diff,
        "files_changed": ["x.py"],
        "verification": {
            "syntax": {"ok": True, "checked": ["x.py"], "unchecked": [], "errors": {}},
            "tests": {
                "ran": False,
                "ok": True,
                "lines": [],
                "regressions": [],
                "reason": "sandbox off",
            },
            "test_files_changed": [],
        },
    }


@pytest.fixture
def repo_job(tmp_db, tmp_path, monkeypatch):
    """Stub GitHub + the agent; returns what the job did with them."""
    import github_app
    import repo_agent_core

    cloned = tmp_path / "clone"
    cloned.mkdir()
    (cloned / "x.py").write_text("def f():\n    return 1\n")
    seen = {"analyze": [], "issues": [], "result": _ok_result()}

    def fake_analyze(path, goal, **kwargs):
        seen["analyze"].append({"path": path, "goal": goal, **kwargs})
        if isinstance(seen["result"], Exception):
            raise seen["result"]
        return seen["result"]

    def fake_create_issue(inst, repo, title, body):
        seen["issues"].append({"repo": repo, "title": title, "body": body})
        return {"number": 9, "url": "https://gh/acme/app/9"}

    monkeypatch.setattr(github_app, "clone_repo", lambda i, r, d: cloned)
    monkeypatch.setattr(repo_agent_core, "analyze", fake_analyze)
    monkeypatch.setattr(github_app, "create_issue", fake_create_issue)
    monkeypatch.setattr(worker, "_CREATE_ISSUE_RETRY_DELAYS", ())
    return seen


def _fence_lines(body):
    return [line for line in body.splitlines() if line.startswith("```")]


def test_analyze_repo_job_creates_issue(repo_job):
    jid = fp.enqueue_repo_analysis("777", "acme/app", "Fix the SSO timeout")
    _drain_one()

    assert jobs.get_job(jid)["status"] == "done"
    (issue,) = repo_job["issues"]
    assert issue["repo"] == "acme/app"
    assert "SSO timeout" in issue["title"]
    body = issue["body"]
    assert "## Root cause\nf() returned a stale constant." in body
    assert "- Syntax: all 1 checked file(s) parse." in body
    assert "- Tests: **not run** (sandbox off)" in body
    # exactly one fenced block, holding the diff verbatim so `git apply` works
    assert _fence_lines(body) == ["```diff", "```"]
    assert body.split("```diff\n")[1].split("\n```")[0] + "\n" == DIFF
    # the agent ran on the disposable clone, in place
    assert repo_job["analyze"][0]["in_place"] is True


def test_issue_fence_survives_backticks_inside_the_diff():
    diff = DIFF + "+    doc = '```python'\n"
    body = worker._issue_body("goal", _ok_result(diff))
    assert _fence_lines(body) == ["````diff", "````"]
    assert body.split("````diff\n")[1].split("\n````")[0] + "\n" == diff


def test_issue_reports_test_results_and_edited_tests():
    result = _ok_result()
    result["verification"]["tests"] = {
        "ran": True, "ok": True, "regressions": [], "reason": "",
        "lines": ["`pytest`: 12 passed before -> 13 passed after"],
    }  # fmt: skip
    result["verification"]["test_files_changed"] = ["tests/test_x.py"]
    body = worker._issue_body("goal", result, report_count=4)
    assert "_Based on 4 customer report(s) in this bucket._" in body
    assert "- Tests: `pytest`: 12 passed before -> 13 passed after" in body
    assert "- No test regressions against the pre-change baseline." in body
    assert "edits test files (`tests/test_x.py`)" in body


def _seed_bucket(label="Checkout > Payment > Crash"):
    for i, text in enumerate(["app crashes when I pay", "checkout froze, lost my cart"]):
        fb = fp.StandardFeedbackObject(text=text, source="webhook", external_id=f"b-{i}")
        fb.labels, fb.sentiment = [label], "negative"
        fp.save_feedback(fb)
    (bucket,) = fp.rebuild_buckets()
    return fp.get_bucket(fp.settings.default_workspace_id, bucket["id"])


def test_bucket_job_passes_customer_reports_and_marks_the_bucket_ticketed(repo_job):
    bucket = _seed_bucket()
    jid = fp.enqueue_repo_analysis(
        "777",
        "acme/app",
        bucket["suggested_goal"],
        bucket_id=bucket["id"],
        bucket_label=bucket["label"],
    )
    _drain_one()

    assert jobs.get_job(jid)["status"] == "done"
    assert sorted(repo_job["analyze"][0]["reports"]) == [
        "app crashes when I pay",
        "checkout froze, lost my cart",
    ]
    assert "_Based on 2 customer report(s)" in repo_job["issues"][0]["body"]
    assert fp.get_bucket(bucket["workspace_id"], bucket["id"])["status"] == "ticketed"


def test_no_verified_fix_means_no_issue_no_retry_and_a_refund(repo_job, monkeypatch):
    import credits

    ws = fp.settings.default_workspace_id
    monkeypatch.setattr(fp.settings, "credits_enabled", True)
    credits.grant(ws, 100, "test")
    bucket = _seed_bucket()
    repo_job["result"] = {"status": "no_fix", "error": "step budget exhausted", "summary": ""}

    jid = fp.enqueue_repo_analysis(
        "777",
        "acme/app",
        bucket["suggested_goal"],
        bucket_id=bucket["id"],
        bucket_label=bucket["label"],
    )
    assert credits.get_balance(ws) == 100 - fp.settings.credits_per_repo_analysis
    _drain_one()

    job = jobs.get_job(jid)
    assert job["status"] == "dead" and job["attempts"] == 1  # buried at once, not retried
    assert "no verified fix (no_fix): step budget exhausted" in job["last_error"]
    assert repo_job["issues"] == []  # nothing unverified reaches the customer's repo
    assert credits.get_balance(ws) == 100  # refunded
    assert fp.get_bucket(bucket["workspace_id"], bucket["id"])["status"] == "failed"


def test_transient_failure_is_retried_and_refunded_only_when_attempts_run_out(
    repo_job, monkeypatch
):
    import credits

    ws = fp.settings.default_workspace_id
    monkeypatch.setattr(fp.settings, "credits_enabled", True)
    monkeypatch.setattr(jobs.settings, "job_max_attempts", 2)
    monkeypatch.setattr(jobs.settings, "job_backoff_base_seconds", 0.001)
    credits.grant(ws, 100, "test")
    repo_job["result"] = RuntimeError("model API unreachable")

    jid = fp.enqueue_repo_analysis("777", "acme/app", "Fix the SSO timeout")
    _drain_one()
    assert jobs.get_job(jid)["status"] == "pending"  # will be retried
    assert credits.get_balance(ws) == 100 - fp.settings.credits_per_repo_analysis

    with fp.get_conn() as conn:  # make the retry runnable now
        conn.execute(
            "UPDATE jobs SET run_after = datetime('now', '-1 minute') WHERE id = ?", (jid,)
        )
    _drain_one()
    assert jobs.get_job(jid)["status"] == "dead"
    assert credits.get_balance(ws) == 100


def test_exhausted_model_quota_postpones_the_job_until_it_resets(repo_job):
    class QuotaError(RuntimeError):
        retry_after = 6 * 3600.0

    repo_job["result"] = QuotaError("model quota exhausted; retry in 6.0h")
    jid = fp.enqueue_repo_analysis("777", "acme/app", "Fix the SSO timeout")
    _drain_one()

    job = jobs.get_job(jid)
    assert job["status"] == "pending" and "quota exhausted" in job["last_error"]
    with fp.get_conn() as conn:
        wait = conn.execute(
            "SELECT (julianday(run_after) - julianday('now')) * 86400 FROM jobs WHERE id = ?",
            (jid,),
        ).fetchone()[0]
    assert 6 * 3600 - 60 < wait <= 6 * 3600  # scheduled for the reset, not 5 seconds from now
    assert jobs.claim_next("w1") is None  # and not picked up before then


def test_issue_creation_is_retried_without_rerunning_the_agent(repo_job, monkeypatch):
    import github_app

    attempts = []

    def flaky(inst, repo, title, body):
        attempts.append(title)
        if len(attempts) < 3:
            raise github_app.GitHubError("502 bad gateway")
        return {"number": 11}

    monkeypatch.setattr(github_app, "create_issue", flaky)
    monkeypatch.setattr(worker, "_CREATE_ISSUE_RETRY_DELAYS", (0.0, 0.0))
    jid = fp.enqueue_repo_analysis("777", "acme/app", "Fix the SSO timeout")
    _drain_one()

    assert jobs.get_job(jid)["status"] == "done"
    assert len(attempts) == 3 and len(repo_job["analyze"]) == 1


def test_generate_fix_end_to_end_with_the_real_agent(tmp_db, tmp_path, monkeypatch):
    """Bucket -> job -> real fix agent on the clone -> ticket whose patch applies.

    Only GitHub (clone + issue API) and the model are stood in for.
    """
    import json
    import shutil
    import subprocess

    import github_app
    import llm
    from llm.fake import FakeLLM

    if shutil.which("git") is None:
        pytest.skip("git not installed")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "cart.py").write_text("def total(prices):\n    return sum(prices) + 1\n")

    def fake_clone(inst, repo, dest):
        shutil.copytree(upstream, dest)
        return dest

    issues = []
    monkeypatch.setattr(github_app, "clone_repo", fake_clone)
    monkeypatch.setattr(
        github_app,
        "create_issue",
        lambda inst, repo, title, body: issues.append(body) or {"number": 3},
    )

    def reply(tool, args):
        return json.dumps({"thought": "t", "actions": [{"tool": tool, "args": args}]})

    model = FakeLLM(
        script=[
            reply("read_file", {"path": "cart.py"}),
            reply("edit_file", {"path": "cart.py", "old": "sum(prices) + 1", "new": "sum(prices)"}),
            reply("finish", {"summary": "Removed the stray +1.", "root_cause": "Off-by-one."}),
        ]
    )
    llm.reset_cache()
    monkeypatch.setattr(llm, "_llm", model)

    bucket = _seed_bucket("Cart > Total > Overcharged")
    jid = fp.enqueue_repo_analysis(
        "777",
        "acme/app",
        bucket["suggested_goal"],
        bucket_id=bucket["id"],
        bucket_label=bucket["label"],
    )
    _drain_one()

    assert jobs.get_job(jid)["status"] == "done", jobs.get_job(jid)["last_error"]
    assert fp.get_bucket(bucket["workspace_id"], bucket["id"])["status"] == "ticketed"
    # the agent was shown the customers' words, not just the label
    assert "app crashes when I pay" in model.calls[0]["prompt"]
    # clone and dependency dirs are cleaned up
    assert not any((tmp_path / "gh-clones" / "777").iterdir())

    (body,) = issues
    assert "## Root cause\nOff-by-one." in body
    patch = tmp_path / "fix.patch"
    patch.write_text(body.split("```diff\n")[1].split("\n```")[0] + "\n")
    subprocess.run(["git", "apply", str(patch)], cwd=upstream, check=True)
    assert (upstream / "cart.py").read_text() == "def total(prices):\n    return sum(prices)\n"


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
