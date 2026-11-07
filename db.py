"""SQLite connection + schema layer, shared by the API, the worker, and jobs.

Kept separate from feedback_platform so the job queue (jobs.py) and the worker
process can open the database without importing the whole FastAPI app.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from config import settings

log = logging.getLogger("prodrion.db")


def utc_now_iso() -> str:
    """Timezone-aware UTC timestamp in ISO-8601 (e.g. 2026-01-01T00:00:00+00:00)."""
    return datetime.now(timezone.utc).isoformat()


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    text         TEXT NOT NULL,
    user_id      TEXT,
    user_email   TEXT,
    source       TEXT NOT NULL,
    external_id  TEXT,                      -- stable id from the source, for dedup
    timestamp    TEXT NOT NULL,
    raw_meta     TEXT,
    labels       TEXT,                      -- JSON array (kept for raw views)
    sentiment    TEXT,                      -- 'negative' | 'neutral' | 'positive'
    arr          REAL DEFAULT 0,
    plan_type    TEXT,
    jira_ticket  TEXT,
    jira_status  TEXT,
    embedding    BLOB,
    embed_model  TEXT,                      -- provider/model that produced `embedding`
    created_at   TEXT DEFAULT (datetime('now'))
);

-- Idempotency: one row per (workspace, source, external_id) when the source gives us an id.
CREATE UNIQUE INDEX IF NOT EXISTS ux_feedback_ws_source_ext
    ON feedback(workspace_id, source, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_feedback_ws ON feedback(workspace_id);

-- Normalised labels: one row per (feedback, label) for correct per-node rollups.
CREATE TABLE IF NOT EXISTS feedback_labels (
    feedback_id TEXT NOT NULL,
    label       TEXT NOT NULL,
    PRIMARY KEY (feedback_id, label),
    FOREIGN KEY (feedback_id) REFERENCES feedback(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_feedback_labels_label ON feedback_labels(label);

CREATE TABLE IF NOT EXISTS taxonomy (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    level1       TEXT NOT NULL,
    level2       TEXT,
    level3       TEXT,
    description  TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_taxonomy_ws ON taxonomy(workspace_id);

CREATE TABLE IF NOT EXISTS jira_tickets (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    key          TEXT NOT NULL,
    summary      TEXT NOT NULL,
    status       TEXT,
    embedding    BLOB,
    embed_model  TEXT,
    updated_at   TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jira_tickets_ws_key ON jira_tickets(workspace_id, key);

CREATE TABLE IF NOT EXISTS users (
    email        TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    user_id      TEXT,
    arr          REAL DEFAULT 0,
    plan_type    TEXT,
    company      TEXT,
    PRIMARY KEY (workspace_id, email)
);

-- Per-(workspace, source) ingestion high-water mark (e.g. last seen created_at / ts).
CREATE TABLE IF NOT EXISTS sync_state (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    source       TEXT NOT NULL,
    cursor       TEXT,
    updated_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (workspace_id, source)
);

-- Clustered buckets of negative feedback, surfaced to the dashboard as
-- suggested features and eligible for a user-triggered "Generate fix".
CREATE TABLE IF NOT EXISTS buckets (
    id             TEXT PRIMARY KEY,
    workspace_id   TEXT NOT NULL,
    label          TEXT NOT NULL,
    feedback_count INTEGER NOT NULL DEFAULT 0,
    arr_impacted   REAL NOT NULL DEFAULT 0,
    suggested_goal TEXT,
    status         TEXT NOT NULL DEFAULT 'new',  -- new|queued|resolved|dismissed
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_buckets_ws_label ON buckets(workspace_id, label);

-- Server-authoritative credit balance per workspace.
CREATE TABLE IF NOT EXISTS credit_accounts (
    workspace_id TEXT PRIMARY KEY,
    balance      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only credit movements (grants and charges).
CREATE TABLE IF NOT EXISTS credit_ledger (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    ref           TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_credit_ledger_ws
    ON credit_ledger(workspace_id, created_at DESC);

-- Durable background job queue (drained by worker.py).
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|dead
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error   TEXT,
    run_after    TEXT NOT NULL DEFAULT (datetime('now')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, run_after);

-- Superseded by workspace-scoped unique indexes (see _ADDED_COLUMNS /
-- _rebuild_workspace_scoped_tables below) — drop so two workspaces can
-- legitimately share a (source, external_id) or ticket key.
DROP INDEX IF EXISTS ux_feedback_source_ext;
DROP INDEX IF EXISTS ux_jira_tickets_key;
"""

# Columns added after the original prototype schema — applied to pre-existing
# databases by _migrate().
_ADDED_COLUMNS = {
    "feedback": [
        ("external_id", "TEXT"),
        ("embed_model", "TEXT"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("sentiment", "TEXT"),
    ],
    "jira_tickets": [
        ("embed_model", "TEXT"),
        ("workspace_id", "TEXT NOT NULL DEFAULT 'default'"),
    ],
    "taxonomy": [("workspace_id", "TEXT NOT NULL DEFAULT 'default'")],
}

# Tables whose primary key changed to include workspace_id. SQLite can't
# ALTER a primary key, so pre-existing tables are rebuilt in place, with
# every existing row defaulted into the "default" workspace.
_REBUILD_WORKSPACE_PK = {
    "users": (
        """
        CREATE TABLE users_new (
            email        TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            user_id      TEXT,
            arr          REAL DEFAULT 0,
            plan_type    TEXT,
            company      TEXT,
            PRIMARY KEY (workspace_id, email)
        )
        """,
        "INSERT INTO users_new (email, workspace_id, user_id, arr, plan_type, company) "
        "SELECT email, 'default', user_id, arr, plan_type, company FROM users",
    ),
    "sync_state": (
        """
        CREATE TABLE sync_state_new (
            workspace_id TEXT NOT NULL DEFAULT 'default',
            source       TEXT NOT NULL,
            cursor       TEXT,
            updated_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (workspace_id, source)
        )
        """,
        "INSERT INTO sync_state_new (workspace_id, source, cursor, updated_at) "
        "SELECT 'default', source, cursor, updated_at FROM sync_state",
    ),
}


def get_conn(*, autocommit: bool = False) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    Pass ``autocommit=True`` for code that manages transactions explicitly
    (e.g. ``BEGIN IMMEDIATE`` in the job-claim path).
    """
    timeout = settings.sqlite_busy_timeout_seconds
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if autocommit:
        conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the prototype schema to pre-existing tables.

    Runs before the schema script so CREATE INDEX statements that reference the
    new columns succeed on an upgraded database.
    """
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("Migrated: added %s.%s", table, name)

    for table, (create_sql, copy_sql) in _REBUILD_WORKSPACE_PK.items():
        if table not in tables:
            continue
        pk_cols = [
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})") if row["pk"] > 0
        ]
        if pk_cols == ["workspace_id", "source"] or pk_cols == ["workspace_id", "email"]:
            continue  # already rebuilt
        conn.execute(f"DROP TABLE IF EXISTS {table}_new")
        conn.execute(create_sql)
        conn.execute(copy_sql)
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        log.info("Migrated: rebuilt %s with workspace_id in its primary key", table)


def init_db() -> None:
    with get_conn() as conn:
        _migrate(conn)
        conn.executescript(DB_SCHEMA)
    log.info("Database initialized.")


def get_sync_cursor(source: str, workspace_id: str = "default") -> str | None:
    """Return the stored high-water mark for a (workspace, source), or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cursor FROM sync_state WHERE workspace_id = ? AND source = ?",
            (workspace_id, source),
        ).fetchone()
    return row["cursor"] if row else None


def set_sync_cursor(source: str, cursor: str, workspace_id: str = "default") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (workspace_id, source, cursor, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(workspace_id, source) DO UPDATE SET
              cursor = excluded.cursor, updated_at = datetime('now')
            """,
            (workspace_id, source, cursor),
        )
