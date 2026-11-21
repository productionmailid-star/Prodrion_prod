"""Regression tests for the launch blockers found by the 2026-11-17 end-to-end run:
connectors (App Store feed, silent failures, re-poll billing), new sources being
picked up, per-workspace taxonomy, per-workspace webhooks, GitHub installation
ownership, one bucket per review + duplicate tickets, and fix results for the
dashboard."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest

import credits
import feedback_platform as fp
import jobs
import security
import supabase_sync
import worker
from llm.fake import FakeLLM

WS = "11111111-2222-3333-4444-555555555555"


def _drain(max_jobs=50):
    ran = []
    for _ in range(max_jobs):
        job = jobs.claim_next("test-worker")
        if job is None:
            break
        ran.append((job["type"], worker.run_job(job)))
    return ran


def _taxonomy(workspace_id, *labels):
    with fp.get_conn() as conn:
        for label in labels:
            parts = (label.split(" > ") + [None, None])[:3]
            conn.execute(
                "INSERT INTO taxonomy (id, workspace_id, level1, level2, level3) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, workspace_id, *parts),
            )


class _Resp:
    def __init__(self, status=200, payload=None, text="", ctype="text/html"):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = {"content-type": ctype}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fp.requests.HTTPError(f"HTTP {self.status_code}")


# ── 1/2. Connectors ──────────────────────────────────────────────────────


def _feed_entry(review_id, rating=1, title="Crash", content="App crashes on pay"):
    return {
        "id": {"label": str(review_id)},
        "title": {"label": title},
        "content": {"label": content},
        "im:rating": {"label": str(rating)},
        "im:version": {"label": "2.1"},
        "author": {"name": {"label": "sam"}},
        "updated": {"label": "2026-11-15T13:51:47-07:00"},
    }


def test_app_store_reads_apples_public_feed(tmp_db, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp(payload={"feed": {"entry": [_feed_entry(1), _feed_entry(2, rating=5)]}})

    monkeypatch.setattr(fp.requests, "get", fake_get)
    reviews = fp.fetch_app_store_reviews("id324684580")
    assert calls[0] == (
        "https://itunes.apple.com/us/rss/customerreviews/page=1/id=324684580/sortby=mostrecent/json"
    )
    assert [r["review_id"] for r in reviews] == ["1", "2"]
    assert reviews[0] == {
        "review_id": "1", "title": "Crash", "content": "App crashes on pay", "rating": 1,
        "userName": "sam", "date": "2026-11-15T13:51:47-07:00", "version": "2.1",
    }  # fmt: skip


def test_app_store_failures_are_reported_not_swallowed(tmp_db, monkeypatch):
    with pytest.raises(fp.ConnectorError, match="numeric"):
        fp.fetch_app_store_reviews("com.example.app")
    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(status=404))
    with pytest.raises(fp.ConnectorError, match="could not read reviews"):
        fp.poll_app_store("ws", app_id="123")


def test_repolling_the_same_reviews_charges_credits_only_once(tmp_db, monkeypatch):
    tmp_db.credits_enabled = True
    credits.grant("ws", 10, "trial")
    feed = {"feed": {"entry": [_feed_entry(1), _feed_entry(2)]}}
    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(payload=feed))

    assert fp.poll_app_store("ws", app_id="123") == 2
    _drain()  # store them
    assert credits.get_balance("ws") == 8
    # the next poll sees the same newest reviews: nothing new, nothing charged
    assert fp.poll_app_store("ws", app_id="123") == 0
    assert credits.get_balance("ws") == 8


def test_failing_connector_shows_as_error_and_keeps_polling(tmp_db, monkeypatch):
    statuses = []
    hook = {"id": "h1", "workspace_id": "ws", "type": "appstore", "status": "connected",
            "config": {"app_id": "123"}}  # fmt: skip
    monkeypatch.setattr(supabase_sync, "get_hook", lambda hook_id: dict(hook))
    monkeypatch.setattr(
        supabase_sync,
        "update_hook_sync_status",
        lambda hook_id, status=None, reviews_synced_delta=0: statuses.append(status),
    )
    monkeypatch.setattr(supabase_sync, "get_sync_settings", lambda ws: {})
    monkeypatch.setattr(supabase_sync, "insert_activity_log", lambda *a, **k: None)
    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(status=503))

    jobs.enqueue("poll_tick", {"hook_id": "h1", "workspace_id": "ws"})
    _drain(1)
    assert statuses == ["error"]
    assert jobs.has_active("poll_tick", {"hook_id": "h1"})  # still scheduled

    hook["status"] = "error"  # it recovers on a later poll
    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(payload={"feed": {}}))
    worker._poll_tick({"hook_id": "h1", "workspace_id": "ws"})
    assert statuses == ["error", "connected"]


# ── 8. New sources and webhook-only workspaces get picked up ─────────────


def test_discover_tick_starts_polling_newly_connected_sources(tmp_db, monkeypatch):
    monkeypatch.setattr(supabase_sync, "configured", lambda: True)
    hooks = []
    monkeypatch.setattr(supabase_sync, "list_connected_hooks", lambda: list(hooks))
    fp.save_feedback(
        fp.StandardFeedbackObject(text="x", source="webhook", workspace_id="ws-hookless")
    )

    worker._discover_tick({})
    assert not jobs.has_active("poll_tick")
    assert jobs.has_active("rebuild_buckets_tick", {"workspace_id": "ws-hookless"})

    hooks.append({"id": "h-new", "workspace_id": "ws-new", "type": "playstore", "config": {}})
    worker._discover_tick({})
    assert jobs.has_active("poll_tick", {"hook_id": "h-new"})
    assert jobs.has_active("rebuild_buckets_tick", {"workspace_id": "ws-new"})
    worker._discover_tick({})  # idempotent
    with fp.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs WHERE type = 'poll_tick'").fetchone()[0]
    assert n == 1


# ── 3. Taxonomy ──────────────────────────────────────────────────────────

TAXONOMY_JSON = (
    '{"taxonomy": [{"level1": "Checkout", "level2": "Payment", "level3": "Errors"},'
    ' {"level1": "Pricing", "level2": "Prices", "level3": "Level"}]}'
)


def test_taxonomy_is_never_built_from_an_error_page(tmp_db, monkeypatch):
    monkeypatch.setattr(fp, "is_safe_public_url", lambda url: True)
    _taxonomy("ws", "Existing > Area > Kept")

    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(status=403, text="Forbidden"))
    with pytest.raises(fp.TaxonomyError, match="HTTP 403"):
        fp.bootstrap_taxonomy_from_url("https://help.example.com", "ws")

    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(text="<p>Enable JS</p>"))
    with pytest.raises(fp.TaxonomyError, match="too little readable text"):
        fp.bootstrap_taxonomy_from_url("https://help.example.com", "ws")

    page = "<h1>Help</h1>" + "<p>Robot policy: set a User-Agent.</p>" * 30
    monkeypatch.setattr(fp.requests, "get", lambda url, **kw: _Resp(text=page))
    monkeypatch.setattr(fp, "get_llm", lambda: FakeLLM(canned='{"taxonomy": []}'))
    with pytest.raises(fp.TaxonomyError, match="no product areas"):
        fp.bootstrap_taxonomy_from_url("https://help.example.com", "ws")

    assert fp.get_taxonomy_labels("ws") == ["Existing > Area > Kept"]  # untouched


def test_new_workspace_learns_its_taxonomy_from_its_reviews(tmp_db, monkeypatch):
    tmp_db.taxonomy_auto_min_feedback = 3
    for i, text in enumerate(["pay button errors", "checkout fails", "too expensive"]):
        fp.save_feedback(
            fp.StandardFeedbackObject(
                text=text, source="webhook", workspace_id="ws", external_id=f"e{i}",
                labels=[fp.UNCATEGORIZED_LABEL], sentiment="negative",
            )
        )  # fmt: skip
    assert fp.rebuild_buckets("ws") == []  # all Uncategorized: nothing to fix yet

    classify = '{"labels": ["Checkout > Payment > Errors"], "sentiment": "negative"}'
    llm = FakeLLM(canned=classify, script=[TAXONOMY_JSON])
    monkeypatch.setattr(fp, "get_llm", lambda: llm)

    worker._rebuild_buckets_tick({"workspace_id": "ws"})
    assert fp.get_taxonomy_labels("ws") == [
        "Checkout > Payment > Errors",
        "Pricing > Prices > Level",
    ]
    assert "pay button errors" in llm.calls[0]["prompt"]  # learned from the reviews
    buckets = fp.list_bucket_fixes("ws")
    assert [(b["label"], b["feedback_count"]) for b in buckets] == [
        ("Checkout > Payment > Errors", 3)
    ]


def test_operator_docs_url_is_not_applied_to_customer_workspaces(tmp_db, monkeypatch):
    tmp_db.taxonomy_bootstrap_url = "https://docs.operator.example"
    called = []
    monkeypatch.setattr(fp, "bootstrap_taxonomy_from_url", lambda url, ws: called.append(ws))
    fp.auto_bootstrap_taxonomy_if_needed("webhook", "customer-ws")
    assert called == []
    fp.auto_bootstrap_taxonomy_if_needed("webhook", tmp_db.default_workspace_id)
    assert called == [tmp_db.default_workspace_id]


# ── 6. One bucket per review, no duplicate tickets ───────────────────────


def test_a_review_counts_in_one_bucket_only(tmp_db):
    for i in range(2):
        fp.save_feedback(
            fp.StandardFeedbackObject(
                text=f"pay fails {i}", source="webhook", workspace_id="ws", external_id=f"p{i}",
                labels=["Checkout > Payment", "Checkout > Coupons"], sentiment="negative",
            )
        )  # fmt: skip
    assert [b["label"] for b in fp.rebuild_buckets("ws")] == ["Checkout > Payment"]
    assert len(fp.get_bucket_reports("ws", "Checkout > Payment")) == 2
    assert fp.get_bucket_reports("ws", "Checkout > Coupons") == []


FIX_A = (
    "diff --git a/shop/pricing.py b/shop/pricing.py\n--- a/shop/pricing.py\n"
    "+++ b/shop/pricing.py\n@@ -5,4 +5,6 @@\n def apply_coupon(amount, coupon):\n"
    "+    if not coupon:\n+        return amount\n     percent = coupon['percent_off']\n"
)
FIX_A_VARIANT = FIX_A.replace("return amount", "return round(amount, 2)")
FIX_OTHER = (
    "diff --git a/shop/users.py b/shop/users.py\n--- a/shop/users.py\n+++ b/shop/users.py\n"
    "@@ -10,3 +10,3 @@\n def find(email):\n-    key = email\n+    key = email.lower()\n"
    "     return USERS.get(key)\n"
)


def test_diff_footprint_matches_changes_to_the_same_lines():
    assert fp.diff_footprint(FIX_A) & fp.diff_footprint(FIX_A_VARIANT)
    assert not fp.diff_footprint(FIX_A) & fp.diff_footprint(FIX_OTHER)
    assert fp.diff_footprint(FIX_OTHER) == {("shop/users.py", "-11"), ("shop/users.py", "+12")}


@pytest.fixture
def fix_job(tmp_db, tmp_path, monkeypatch):
    import github_app
    import repo_agent_core

    tmp_db.credits_enabled = True
    credits.grant(WS, 100, "trial")
    clone = tmp_path / "clone"
    clone.mkdir()
    state = {"results": [], "issues": []}

    def fake_analyze(path, goal, **kwargs):
        return state["results"].pop(0)

    def fake_issue(inst, repo, title, body):
        state["issues"].append(title)
        return {"number": len(state["issues"]), "url": f"https://github.com/{repo}/issues/1"}

    monkeypatch.setattr(github_app, "clone_repo", lambda i, r, d: clone)
    monkeypatch.setattr(repo_agent_core, "analyze", fake_analyze)
    monkeypatch.setattr(github_app, "create_issue", fake_issue)
    monkeypatch.setattr(supabase_sync, "insert_activity_log", lambda *a, **k: None)
    return state


def _ok(diff, files=("shop/pricing.py",)):
    return {
        "status": "ok", "summary": "Handle a missing coupon.", "root_cause": "coupon=None crashed",
        "diff": diff, "files_changed": list(files),
        "verification": {"syntax": {"checked": list(files)}, "tests": {"ran": False}},
    }  # fmt: skip


def _generate(label):
    bucket = fp.get_bucket_by_label(WS, label) or fp.get_bucket(
        WS, fp._upsert_bucket(WS, label, 3, 0.0)
    )
    job_id = fp.enqueue_repo_analysis(
        "1", "acme/shop", bucket["suggested_goal"], WS, bucket_id=bucket["id"], bucket_label=label
    )
    fp.set_bucket_fix(WS, bucket["id"], status="queued", reset=True, fix_job_id=job_id)
    _drain()
    return {f["label"]: f for f in fp.list_bucket_fixes(WS)}[label]


def test_fix_result_is_recorded_for_the_dashboard(fix_job):
    fix_job["results"].append(_ok(FIX_A))
    fix = _generate("Checkout > Payment")
    assert fix["status"] == "ticketed"
    assert fix["issue_number"] == 1 and fix["issue_url"].endswith("/issues/1")
    assert fix["files"] == ["shop/pricing.py"]
    assert fix["root_cause"] == "coupon=None crashed" and fix["diff"] == FIX_A
    assert credits.get_balance(WS) == 75  # charged once, kept


def test_same_fix_from_a_second_bucket_links_the_existing_ticket(fix_job):
    fix_job["results"] += [_ok(FIX_A), _ok(FIX_A_VARIANT)]
    _generate("Checkout > Payment")
    dup = _generate("Checkout > Coupons")
    assert len(fix_job["issues"]) == 1  # no second ticket
    assert dup["status"] == "ticketed" and dup["duplicate_of"] == "Checkout > Payment"
    assert dup["issue_number"] == 1
    assert credits.get_balance(WS) == 75  # second run refunded

    fix_job["results"].append(_ok(FIX_OTHER, files=("shop/users.py",)))
    other = _generate("Account > Login")
    assert other["duplicate_of"] is None and len(fix_job["issues"]) == 2


def test_no_fix_records_the_reason_and_refunds(fix_job):
    fix_job["results"].append(
        {"status": "not_actionable", "summary": "Prices are data, not a bug.", "diff": ""}
    )
    fix = _generate("Pricing > Level")
    assert fix["status"] == "failed"
    assert fix["error"] == "Not a code problem: Prices are data, not a bug."
    assert credits.get_balance(WS) == 100


# ── API: webhooks, taxonomy, fixes, GitHub linking ───────────────────────


def _token(sub="owner-1"):
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "exp": int(time.time()) + 3600},
        "jwtsecret",
        algorithm="HS256",
    )


@pytest.fixture
def api(tmp_db, monkeypatch):
    from fastapi.testclient import TestClient

    tmp_db.api_auth_token = "sekret"
    tmp_db.webhook_secret = "server-hook-secret"
    tmp_db.supabase_jwt_secret = "jwtsecret"
    monkeypatch.setattr(security, "_resolve_workspace", lambda uid: WS, raising=False)
    return TestClient(fp.app)


def _as_customer(monkeypatch, configured=False):
    monkeypatch.setattr(supabase_sync, "configured", lambda: configured)
    monkeypatch.setattr(supabase_sync, "get_workspace_by_owner", lambda uid: {"id": WS})
    security.clear_workspace_cache()
    return {"Authorization": f"Bearer {_token()}"}


def test_customer_webhook_lands_in_their_workspace(api, tmp_db, monkeypatch):
    monkeypatch.setattr(supabase_sync, "configured", lambda: False)
    secret = security.workspace_webhook_secret(WS)
    assert secret and secret != security.workspace_webhook_secret("another-workspace")

    url = f"/webhook/feedback/{WS}"
    assert api.post(url, json={"text": "hi"}).status_code == 401
    assert api.post(url, json={"text": "hi"}, headers={"X-Webhook-Secret": "server-hook-secret"}).status_code == 401  # fmt: skip

    ok = api.post(
        url, json={"text": "checkout broke", "id": "t1"}, headers={"X-Webhook-Secret": secret}
    )
    assert ok.status_code == 202, ok.text
    assert jobs.get_job(ok.json()["job_id"])["payload"]["workspace_id"] == WS
    _drain()
    again = api.post(url, json={"text": "checkout broke", "id": "t1"}, headers={"X-Webhook-Secret": secret})  # fmt: skip
    assert again.json() == {"status": "duplicate"}


def test_customer_webhook_uses_the_dashboard_secret_with_supabase(api, monkeypatch):
    monkeypatch.setattr(supabase_sync, "configured", lambda: True)
    monkeypatch.setattr(supabase_sync, "get_webhook_secret", lambda ws: "whsec_abc" if ws == WS else None)  # fmt: skip
    url = f"/webhook/feedback/{WS}"
    assert api.post(url, json={"text": "x"}, headers={"X-Webhook-Secret": "whsec_abc"}).status_code == 202  # fmt: skip
    assert api.post(url, json={"text": "x"}, headers={"X-Webhook-Secret": "wrong"}).status_code == 401  # fmt: skip
    other = "99999999-2222-3333-4444-555555555555"  # no secret set: never open
    assert api.post(f"/webhook/feedback/{other}", json={"text": "x"}).status_code == 401
    assert api.post("/webhook/feedback/not-a-uuid", json={"text": "x"}).status_code == 401


def test_me_endpoints_for_webhook_taxonomy_and_fixes(api, monkeypatch):
    headers = _as_customer(monkeypatch)
    monkeypatch.setattr(security, "require_workspace", security.require_workspace)
    hook = api.get("/me/webhook", headers=headers)
    assert hook.status_code == 200, hook.text

    monkeypatch.setattr(fp, "is_safe_public_url", lambda url: url.startswith("https://"))
    assert api.post("/me/taxonomy", json={"url": "file:///etc/passwd"}, headers=headers).status_code == 400  # fmt: skip
    queued = api.post("/me/taxonomy", json={"url": "https://help.acme.com"}, headers=headers)
    assert queued.status_code == 202
    status = api.get("/me/taxonomy", headers=headers).json()
    assert status["labels"] == [] and status["last_build"]["status"] == "pending"

    assert api.get("/me/fixes", headers=headers).json()["fixes"] == []


def test_generate_fix_cannot_be_queued_twice(api, monkeypatch):
    headers = _as_customer(monkeypatch, configured=True)
    monkeypatch.setattr(
        supabase_sync, "get_workspace",
        lambda ws: {"id": ws, "github_installation_id": "9", "github_repo": "acme/app"},
    )  # fmt: skip
    fp._upsert_bucket(security.get_settings().default_workspace_id, "Checkout > Pay", 3, 0.0)
    ws = api.get("/me/fixes", headers=headers).json()["workspace_id"]
    fp._upsert_bucket(ws, "Checkout > Pay", 3, 0.0)
    first = api.post("/me/repo-analysis", json={"label": "Checkout > Pay"}, headers=headers)
    assert first.status_code == 202, first.text
    second = api.post("/me/repo-analysis", json={"label": "Checkout > Pay"}, headers=headers)
    assert second.status_code == 409


def test_github_installation_must_belong_to_the_signed_in_github_user(api, tmp_db, monkeypatch):
    import github_app

    headers = _as_customer(monkeypatch, configured=True)
    tmp_db.github_app_client_id, tmp_db.github_app_client_secret = "Iv1.abc", "s3cret"
    linked = {}
    monkeypatch.setattr(supabase_sync, "set_workspace_github_installation", lambda ws, i: linked.update({ws: i}))  # fmt: skip
    monkeypatch.setattr(supabase_sync, "workspace_for_installation", lambda i: None)
    monkeypatch.setattr(github_app, "user_installation_ids", lambda code: {"111"} if code == "good" else set())  # fmt: skip

    url = "/me/github/install-callback"
    assert (
        api.post(url, json={"installation_id": "111"}, headers=headers).status_code == 400
    )  # no code
    denied = api.post(url, json={"installation_id": "222", "code": "good"}, headers=headers)
    assert denied.status_code == 403 and linked == {}
    ok = api.post(url, json={"installation_id": "111", "code": "good"}, headers=headers)
    assert ok.status_code == 200 and list(linked.values()) == ["111"]

    monkeypatch.setattr(supabase_sync, "workspace_for_installation", lambda i: "someone-else")
    taken = api.post(url, json={"installation_id": "111", "code": "good"}, headers=headers)
    assert taken.status_code == 409


def test_production_requires_github_oauth_credentials_with_the_app():
    from config import Settings

    base = {
        "_env_file": None, "environment": "production", "llm_provider": "gemini",
        "gemini_api_key": "k", "api_auth_token": "t", "webhook_secret": "w",
        "supabase_db_url": "postgresql://x", "github_app_id": "123",
    }  # fmt: skip
    with pytest.raises(ValueError, match="GITHUB_APP_CLIENT_ID"):
        Settings(**base)
    assert Settings(**base, github_app_client_id="c", github_app_client_secret="s")
