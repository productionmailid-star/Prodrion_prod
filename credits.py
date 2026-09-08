"""Server-authoritative credit ledger.

Billable actions call :func:`charge` *before* doing the work. Balances and the
append-only ledger live in the backend database, never in client-writable
Supabase tables. When ``PRODRION_CREDITS_ENABLED`` is false, :func:`charge` is a
no-op so single-tenant / dev setups run without billing.
"""

from __future__ import annotations

import logging
import uuid

from config import settings
from db import get_conn

log = logging.getLogger("prodrion.credits")


class InsufficientCredits(Exception):
    """Raised by :func:`charge` when the workspace cannot cover the cost."""

    def __init__(self, workspace_id: str, needed: int, balance: int):
        self.workspace_id = workspace_id
        self.needed = needed
        self.balance = balance
        super().__init__(f"workspace {workspace_id!r} has {balance} credits, needs {needed}")


def get_balance(workspace_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT balance FROM credit_accounts WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
    return row["balance"] if row else 0


def ledger(workspace_id: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, delta, balance_after, reason, ref, created_at "
            "FROM credit_ledger WHERE workspace_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _apply(
    workspace_id: str,
    delta: int,
    reason: str,
    ref: str | None,
    *,
    allow_negative: bool,
) -> int:
    """Atomically move the balance by ``delta`` and append a ledger row."""
    conn = get_conn(autocommit=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT balance FROM credit_accounts WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        current = row["balance"] if row else 0
        new_balance = current + delta
        if new_balance < 0 and not allow_negative:
            conn.execute("ROLLBACK")
            raise InsufficientCredits(workspace_id, -delta, current)

        conn.execute(
            """
            INSERT INTO credit_accounts (workspace_id, balance, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(workspace_id) DO UPDATE SET
              balance = excluded.balance, updated_at = datetime('now')
            """,
            (workspace_id, new_balance),
        )
        conn.execute(
            "INSERT INTO credit_ledger "
            "(id, workspace_id, delta, balance_after, reason, ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), workspace_id, delta, new_balance, reason, ref),
        )
        conn.execute("COMMIT")
        return new_balance
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def grant(workspace_id: str, amount: int, reason: str, ref: str | None = None) -> int:
    """Add credits (top-up / promo / admin). Returns the new balance."""
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    new_balance = _apply(workspace_id, amount, reason, ref, allow_negative=True)
    log.info("Granted %d credits to %s (%s) -> %d", amount, workspace_id, reason, new_balance)
    return new_balance


def charge(workspace_id: str, amount: int, reason: str, ref: str | None = None) -> int:
    """Deduct credits for a billable action.

    No-op (returns the current balance) when credits are disabled or the amount
    is zero. Raises :class:`InsufficientCredits` when the balance is too low.
    """
    if not settings.credits_enabled or amount <= 0:
        return get_balance(workspace_id)
    new_balance = _apply(workspace_id, -amount, reason, ref, allow_negative=False)
    log.info("Charged %d credits to %s (%s) -> %d", amount, workspace_id, reason, new_balance)
    return new_balance
