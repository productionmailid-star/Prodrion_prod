"""Milestones 10-14: multi-tenant data model, sentiment-gated buckets,
per-workspace connector dispatch, and the self-rescheduling job chains that
replaced the in-process APScheduler.
"""

from __future__ import annotations

import pytest

import feedback_platform as fp
import jobs
import security


@pytest.fixture
def db(tmp_db):
    return fp


def _fb(**kw):
    base = {"text": "login broken", "source": "webhook", "labels": ["Auth > Login"]}
    base.update(kw)
    return fp.StandardFeedbackObject(**base)


# --- workspace isolation ------------------------------------------------


def test_feedback_is_isolated_per_workspace(db):
    db.save_feedback(_fb(workspace_id="ws-a", external_id="a1", user_email="u@a.com", arr=100.0))
    db.save_feedback(_fb(workspace_id="ws-b", external_id="b1", user_email="u@b.com", arr=999.0))

    report_a = db.get_revenue_impact_report(workspace_id="ws-a")
    report_b = db.get_revenue_impact_report(workspace_id="ws-b")

    assert report_a[0]["total_arr"] == 100.0
    assert report_b[0]["total_arr"] == 999.0


def test_same_external_id_allowed_across_workspaces(db):
    db.save_feedback(_fb(workspace_id="ws-a", source="zendesk", external_id="123"))
    db.save_feedback(_fb(workspace_id="ws-b", source="zendesk", external_id="123"))
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE source='zendesk' AND external_id='123'"
        ).fetchone()[0]
    assert n == 2  # not deduped across workspaces


def test_chat_with_data_is_scoped_to_one_workspace(db, monkeypatch):
    from llm.fake import FakeLLM

    db.save_feedback(_fb(workspace_id="ws-a", external_id="a1"))
    db.save_feedback(_fb(workspace_id="ws-b", external_id="b1"))

    monkeypatch.setattr(fp, "get_llm", lambda: FakeLLM(canned="SELECT * FROM feedback"))
    result = fp.chat_with_data("anything", workspace_id="ws-a")
    assert result["count"] == 1
    assert result["rows"][0]["external_id"] == "a1"


def test_chat_with_data_rejects_malformed_workspace_id(db):
    with pytest.raises(ValueError):
        fp._open_readonly_conn("not valid; DROP TABLE feedback")


# --- sentiment-gated classification -------------------------------------


def test_classify_feedback_returns_labels_and_sentiment(db, monkeypatch):
    from llm.fake import FakeLLM

    monkeypatch.setattr(
        fp,
        "get_llm",
        lambda: FakeLLM(canned='{"labels": ["Bug > Crash"], "sentiment": "negative"}'),
    )
    labels, sentiment = fp.classify_feedback(_fb())
    assert labels == ["Bug > Crash"]
    assert sentiment == "negative"


def test_classify_feedback_defaults_sentiment_on_bad_json(db, monkeypatch):
    from llm.fake import FakeLLM

    monkeypatch.setattr(fp, "get_llm", lambda: FakeLLM(canned="not json"))
    labels, sentiment = fp.classify_feedback(_fb())
    assert labels == ["General > Feedback > Uncategorized"]
    assert sentiment == "neutral"


def test_classify_feedback_rejects_invalid_sentiment_value(db, monkeypatch):
    from llm.fake import FakeLLM

    monkeypatch.setattr(
        fp, "get_llm", lambda: FakeLLM(canned='{"labels": ["X"], "sentiment": "furious"}')
    )
    _labels, sentiment = fp.classify_feedback(_fb())
    assert sentiment == "neutral"  # invalid value falls back, doesn't propagate raw


# --- bucket builder -------------------------------------------------------


def _negative_fb(label, **kw):
    fb = _fb(**kw)
    fb.labels = [label]
    fb.sentiment = "negative"
    return fb


def test_rebuild_buckets_only_clusters_negative_feedback_above_threshold(db, monkeypatch):
    monkeypatch.setattr(fp.settings, "goal_min_cluster_size", 2)

    db.save_feedback(_negative_fb("Billing > Invoice", external_id="n1", arr=500.0))
    db.save_feedback(_negative_fb("Billing > Invoice", external_id="n2", arr=500.0))
    positive = _fb(external_id="p1", labels=["Billing > Invoice"])
    positive.sentiment = "positive"
    db.save_feedback(positive)
    db.save_feedback(_negative_fb("Rare > Issue", external_id="n3"))  # below threshold alone

    touched = db.rebuild_buckets()

    labels = {b["label"] for b in touched}
    assert "Billing > Invoice" in labels
    assert "Rare > Issue" not in labels  # only 1 negative report, below threshold=2

    bucket = db.get_bucket_by_label(fp.settings.default_workspace_id, "Billing > Invoice")
    assert bucket["feedback_count"] == 2
    assert bucket["arr_impacted"] == 1000.0
    assert bucket["status"] == "new"


def test_rebuild_buckets_excludes_uncategorized(db, monkeypatch):
    monkeypatch.setattr(fp.settings, "goal_min_cluster_size", 1)
    db.save_feedback(_negative_fb(fp.UNCATEGORIZED_LABEL, external_id="u1"))
    touched = db.rebuild_buckets()
    assert touched == []


def test_set_bucket_status_updates_without_resetting_count(db, monkeypatch):
    monkeypatch.setattr(fp.settings, "goal_min_cluster_size", 1)
    db.save_feedback(_negative_fb("Auth > SSO", external_id="s1"))
    [bucket] = db.rebuild_buckets()
    db.set_bucket_status(fp.settings.default_workspace_id, bucket["id"], "queued")
    row = db.get_bucket(fp.settings.default_workspace_id, bucket["id"])
    assert row["status"] == "queued"
    assert row["feedback_count"] == 1


# --- per-workspace connector dispatch --------------------------------------


def test_poll_hook_dispatches_by_type(db, monkeypatch):
    calls = []
    monkeypatch.setattr(fp, "poll_zendesk", lambda ws, **kw: calls.append(("zendesk", ws, kw)) or 1)
    monkeypatch.setattr(
        fp, "poll_app_store", lambda ws, **kw: calls.append(("app_store", ws, kw)) or 2
    )
    monkeypatch.setattr(
        fp, "poll_slack_channel", lambda ws, **kw: calls.append(("slack", ws, kw)) or 3
    )

    n1 = fp.poll_hook(
        {
            "workspace_id": "ws-a",
            "type": "zendesk",
            "config": {"url": "u", "email": "e", "token": "t"},
        }
    )
    n2 = fp.poll_hook({"workspace_id": "ws-a", "type": "appstore", "config": {"app_id": "123"}})
    n3 = fp.poll_hook(
        {"workspace_id": "ws-a", "type": "slack", "config": {"bot_token": "x", "channel_id": "C1"}}
    )
    n4 = fp.poll_hook({"workspace_id": "ws-a", "type": "unknown", "config": {}})

    assert (n1, n2, n3, n4) == (1, 2, 3, 0)
    assert calls[0] == ("zendesk", "ws-a", {"url": "u", "email": "e", "token": "t"})


@pytest.mark.parametrize(
    "label,expected",
    [(None, 1), ("1★ and above", 1), ("3★ and above", 3), ("no digits here", 1)],
)
def test_parse_min_star_rating(label, expected):
    assert fp._parse_min_star_rating(label) == expected


@pytest.mark.parametrize(
    "label,expected",
    [
        (None, 3600.0),
        ("Every 15 minutes", 900.0),
        ("Every hour", 3600.0),
        ("Every 6 hours", 21600.0),
        ("Daily", 86400.0),
        ("garbage", 3600.0),
    ],
)
def test_parse_sync_frequency(label, expected):
    assert fp._parse_sync_frequency(label) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("acme/app", "acme/app"),
        ("https://github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app/", "acme/app"),
        ("https://github.com/acme/app.git", "acme/app"),
        ("github.com/acme/app", "acme/app"),
    ],
)
def test_normalize_repo_full_name(value, expected):
    assert fp._normalize_repo_full_name(value) == expected


# --- require_workspace ------------------------------------------------


async def test_require_workspace_dev_fallback_without_supabase(db):
    ws = await security.require_workspace(user_id="dash-user")
    assert ws == fp.settings.default_workspace_id


async def test_require_workspace_resolves_via_supabase_sync(db, monkeypatch):
    import supabase_sync

    monkeypatch.setattr(supabase_sync, "configured", lambda: True)
    monkeypatch.setattr(
        supabase_sync,
        "get_workspace_by_owner",
        lambda uid: {"id": "ws-xyz"} if uid == "dash-user" else None,
    )
    security.clear_workspace_cache()
    ws = await security.require_workspace(user_id="dash-user")
    assert ws == "ws-xyz"


async def test_require_workspace_404_when_no_workspace_found(db, monkeypatch):
    from fastapi import HTTPException

    import supabase_sync

    monkeypatch.setattr(supabase_sync, "configured", lambda: True)
    monkeypatch.setattr(supabase_sync, "get_workspace_by_owner", lambda uid: None)
    security.clear_workspace_cache()
    with pytest.raises(HTTPException) as exc:
        await security.require_workspace(user_id="ghost-user")
    assert exc.value.status_code == 404


# --- durable self-rescheduling jobs (replacing APScheduler) ---------------


def test_jobs_enqueue_respects_delay(db):
    soon = jobs.enqueue("_test_tick", {}, delay_seconds=0)
    jobs.enqueue("_test_tick", {}, delay_seconds=3600)
    assert jobs.claim_next("w1")["id"] == soon
    assert jobs.claim_next("w1") is None  # the delayed job isn't runnable yet


def test_jobs_has_active_matches_payload_subset(db):
    assert jobs.has_active("poll_tick") is False
    jobs.enqueue("poll_tick", {"hook_id": "h1", "workspace_id": "ws-a"})
    assert jobs.has_active("poll_tick") is True
    assert jobs.has_active("poll_tick", {"hook_id": "h1"}) is True
    assert jobs.has_active("poll_tick", {"hook_id": "h2"}) is False


# --- db.py migration path (upgrading a pre-multi-tenant database) --------


def test_migration_adds_workspace_id_and_rebuilds_pk_tables(tmp_path, monkeypatch):
    import config as _config
    import db as _db

    db_path = str(tmp_path / "legacy.db")
    s = _config.Settings(
        _env_file=None, environment="development", llm_provider="fake", db_path=db_path
    )
    monkeypatch.setattr(_db, "settings", s)

    # Build the pre-milestone-10 schema by hand: no workspace_id anywhere,
    # users keyed by bare email, sync_state keyed by bare source.
    conn = _db.get_conn()
    conn.executescript("""
        CREATE TABLE feedback (
            id TEXT PRIMARY KEY, text TEXT NOT NULL, user_id TEXT, user_email TEXT,
            source TEXT NOT NULL, external_id TEXT, timestamp TEXT NOT NULL,
            raw_meta TEXT, labels TEXT, arr REAL DEFAULT 0, plan_type TEXT,
            jira_ticket TEXT, jira_status TEXT, embedding BLOB, embed_model TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX ux_feedback_source_ext ON feedback(source, external_id)
            WHERE external_id IS NOT NULL;
        CREATE TABLE taxonomy (
            id TEXT PRIMARY KEY, level1 TEXT NOT NULL, level2 TEXT, level3 TEXT,
            description TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE jira_tickets (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, summary TEXT NOT NULL,
            status TEXT, embedding BLOB, embed_model TEXT, updated_at TEXT
        );
        CREATE UNIQUE INDEX ux_jira_tickets_key ON jira_tickets(key);
        CREATE TABLE users (
            email TEXT PRIMARY KEY, user_id TEXT, arr REAL DEFAULT 0,
            plan_type TEXT, company TEXT
        );
        CREATE TABLE sync_state (
            source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT
        );
        """)
    conn.execute(
        "INSERT INTO feedback (id, text, source, external_id, timestamp) "
        "VALUES ('f1', 'old row', 'zendesk', 'z1', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO users (email, arr) VALUES ('legacy@co.com', 42.0)")
    conn.execute("INSERT INTO sync_state (source, cursor) VALUES ('zendesk', 'cursor-1')")
    conn.commit()
    conn.close()

    _db.init_db()  # runs _migrate() then the current DB_SCHEMA

    with _db.get_conn() as conn:
        feedback_cols = {row["name"] for row in conn.execute("PRAGMA table_info(feedback)")}
        assert {"workspace_id", "sentiment"} <= feedback_cols

        row = conn.execute("SELECT workspace_id FROM feedback WHERE id='f1'").fetchone()
        assert row["workspace_id"] == "default"

        users_pk = [r["name"] for r in conn.execute("PRAGMA table_info(users)") if r["pk"] > 0]
        assert set(users_pk) == {"workspace_id", "email"}
        user_row = conn.execute(
            "SELECT arr FROM users WHERE workspace_id='default' AND email='legacy@co.com'"
        ).fetchone()
        assert user_row["arr"] == 42.0

        sync_pk = [r["name"] for r in conn.execute("PRAGMA table_info(sync_state)") if r["pk"] > 0]
        assert set(sync_pk) == {"workspace_id", "source"}
        cursor_row = conn.execute(
            "SELECT cursor FROM sync_state WHERE workspace_id='default' AND source='zendesk'"
        ).fetchone()
        assert cursor_row["cursor"] == "cursor-1"

        # old global unique index is gone -> a second workspace can reuse (zendesk, z1)
        conn.execute(
            "INSERT INTO feedback (id, workspace_id, text, source, external_id, timestamp) "
            "VALUES ('f2', 'ws-b', 'new row', 'zendesk', 'z1', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


def test_poll_tick_ends_its_chain_when_hook_disconnected(db, monkeypatch):
    import supabase_sync
    import worker

    monkeypatch.setattr(supabase_sync, "get_hook", lambda hook_id: None)
    before = jobs.list_jobs()
    worker._poll_tick({"hook_id": "gone"})
    after = jobs.list_jobs()
    assert len(after) == len(before)  # did not re-enqueue itself
