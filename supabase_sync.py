"""Narrow, server-only bridge to the dashboard's Supabase/Postgres database.

The Python backend is the source of truth for pipeline data (feedback,
buckets, credits — all in SQLite via db.py). The dashboard reads its own
Supabase tables (workspaces, hooks, billing, suggested_features, ...). This
module is the one place the backend is allowed to read per-workspace
integration config from Supabase and write computed results back into it.

Uses a direct Postgres connection (the Supabase project's connection string,
PRODRION_SUPABASE_DB_URL) rather than the REST/PostgREST API, so this stays a
handful of typed SQL helpers instead of pulling in the full supabase-py SDK.
The service-role-equivalent DB credential never leaves this process; nothing
here is reachable from the browser.

Every function no-ops (reads return empty, writes are skipped) when
PRODRION_SUPABASE_DB_URL is not configured, so local dev / tests that don't
care about the dashboard sync don't need a real Supabase project.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from config import settings

log = logging.getLogger("prodrion.supabase_sync")

_pool = None
_pool_lock = threading.Lock()


class SupabaseSyncError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.supabase_db_url)


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool  # pip install psycopg[binary] psycopg-pool

            _pool = ConnectionPool(settings.supabase_db_url, min_size=1, max_size=5, open=True)
    return _pool


@contextmanager
def _conn() -> Iterator[Any]:
    if not configured():
        raise SupabaseSyncError("PRODRION_SUPABASE_DB_URL is not configured")
    with _get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Release pooled connections (tests / graceful shutdown)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ---------------------------------------------------------------------------
# Reads: per-workspace integration config
# ---------------------------------------------------------------------------


def get_workspace(workspace_id: str) -> dict | None:
    """Return {id, owner_id, name, github_repo, github_installation_id} or None."""
    if not configured():
        return None
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, owner_id, name, github_repo, github_installation_id "
            "FROM workspaces WHERE id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = ("id", "owner_id", "name", "github_repo", "github_installation_id")
    return dict(zip(cols, row, strict=True))


def get_workspace_by_owner(user_id: str) -> dict | None:
    """Resolve a signed-in Supabase user's own workspace (one per user today)."""
    if not configured():
        return None
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, owner_id, name, github_repo, github_installation_id "
            "FROM workspaces WHERE owner_id = %s LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = ("id", "owner_id", "name", "github_repo", "github_installation_id")
    return dict(zip(cols, row, strict=True))


def list_connected_hooks(hook_type: str | None = None) -> list[dict]:
    """Return every `hooks` row with status='connected', optionally filtered by type.

    Shape: [{id, workspace_id, type, config}], config already json-decoded.
    """
    if not configured():
        return []
    with _conn() as conn, conn.cursor() as cur:
        if hook_type:
            cur.execute(
                "SELECT id, workspace_id, type, config FROM hooks "
                "WHERE status = 'connected' AND type = %s",
                (hook_type,),
            )
        else:
            cur.execute(
                "SELECT id, workspace_id, type, config FROM hooks WHERE status = 'connected'"
            )
        rows = cur.fetchall()
    out = []
    for hook_id, workspace_id, htype, config in rows:
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        out.append(
            {"id": hook_id, "workspace_id": workspace_id, "type": htype, "config": config or {}}
        )
    return out


def get_hook(hook_id: str) -> dict | None:
    """Return one `hooks` row by id, or None (e.g. the hook was disconnected
    or deleted since it was last enqueued) — the caller should treat that as
    "stop this poll_tick chain", not an error."""
    if not configured():
        return None
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, workspace_id, type, config, status FROM hooks WHERE id = %s",
            (hook_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    hook_id_, workspace_id, htype, config, status = row
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = {}
    return {
        "id": hook_id_,
        "workspace_id": workspace_id,
        "type": htype,
        "config": config or {},
        "status": status,
    }


def get_sync_settings(workspace_id: str) -> dict:
    """Return {sync_frequency, min_star_rating, auto_tag} with sane defaults."""
    defaults = {"sync_frequency": "Every hour", "min_star_rating": "1★ and above", "auto_tag": True}
    if not configured():
        return defaults
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sync_frequency, min_star_rating, auto_tag FROM sync_settings "
            "WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
    if row is None:
        return defaults
    return {"sync_frequency": row[0], "min_star_rating": row[1], "auto_tag": row[2]}


# ---------------------------------------------------------------------------
# Writes: computed results surfaced on the dashboard
# ---------------------------------------------------------------------------


def set_workspace_github_installation(workspace_id: str, installation_id: str) -> None:
    if not configured():
        log.info("Supabase sync not configured; skipping install-callback write.")
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE workspaces SET github_installation_id = %s, updated_at = now() WHERE id = %s",
            (installation_id, workspace_id),
        )
        conn.commit()


def update_hook_sync_status(
    hook_id: str, *, status: str | None = None, reviews_synced_delta: int = 0
) -> None:
    if not configured():
        return
    with _conn() as conn, conn.cursor() as cur:
        if status is not None:
            cur.execute(
                "UPDATE hooks SET status = %s, last_sync_at = now(), "
                "reviews_synced = reviews_synced + %s, updated_at = now() WHERE id = %s",
                (status, reviews_synced_delta, hook_id),
            )
        else:
            cur.execute(
                "UPDATE hooks SET last_sync_at = now(), "
                "reviews_synced = reviews_synced + %s, updated_at = now() WHERE id = %s",
                (reviews_synced_delta, hook_id),
            )
        conn.commit()


def insert_activity_log(
    workspace_id: str,
    *,
    type: str,
    title: str,
    description: str = "",
    icon: str = "info",
    icon_color: str = "primary",
) -> None:
    if not configured():
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO activity_log (workspace_id, type, icon, icon_color, title, description) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (workspace_id, type, icon, icon_color, title, description),
        )
        conn.commit()


def upsert_suggested_feature(
    workspace_id: str,
    *,
    key: str,
    title: str,
    description: str,
    confidence: int,
    impact_level: str,
    sources_count: int,
) -> None:
    """One row per (workspace, bucket label). `key` is the bucket's label,
    used only to find-and-replace an existing suggestion on rebuild — the
    suggested_features table itself has no unique constraint on it, so this
    does a manual select-then-update/insert.
    """
    if not configured():
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM suggested_features WHERE workspace_id = %s AND title = %s",
            (workspace_id, key),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE suggested_features SET description = %s, confidence = %s, "
                "impact_level = %s, sources_count = %s WHERE id = %s",
                (description, confidence, impact_level, sources_count, row[0]),
            )
        else:
            cur.execute(
                "INSERT INTO suggested_features "
                "(workspace_id, title, description, confidence, impact_level, sources_count) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace_id, title, description, confidence, impact_level, sources_count),
            )
        conn.commit()


def upsert_dashboard_stats(workspace_id: str, **deltas: int) -> None:
    """Increment numeric columns on dashboard_stats (e.g. signals_processed=5)."""
    if not configured() or not deltas:
        return
    set_clause = ", ".join(f"{col} = dashboard_stats.{col} + %s" for col in deltas)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE dashboard_stats SET {set_clause}, updated_at = now() WHERE workspace_id = %s",
            (*deltas.values(), workspace_id),
        )
        conn.commit()


def bump_billing_usage(workspace_id: str, requests_delta: int) -> None:
    """Increment billing.requests_used only — never plan/price/payment columns."""
    if not configured() or requests_delta == 0:
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE billing SET requests_used = requests_used + %s, updated_at = now() "
            "WHERE workspace_id = %s",
            (requests_delta, workspace_id),
        )
        conn.commit()
