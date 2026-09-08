# Credits & billing

Billable actions (feedback ingestion, repo analysis / code generation) consume
credits. Balances are **server-authoritative**: they live in the backend
database and are only mutated by the backend.

## Model

| Table (backend SQLite) | Purpose |
| --- | --- |
| `credit_accounts(workspace_id, balance, updated_at)` | current balance per workspace |
| `credit_ledger(id, workspace_id, delta, balance_after, reason, ref, created_at)` | append-only history of every grant and charge |

`credits.charge()` runs inside `BEGIN IMMEDIATE`, refuses to take the balance
negative (`InsufficientCredits`), and is called **before** the work is queued.
When `PRODRION_CREDITS_ENABLED=false` (the default) `charge()` is a no-op, so
single-tenant / dev deployments run without billing.

Prices: `PRODRION_CREDITS_PER_FEEDBACK`, `PRODRION_CREDITS_PER_REPO_ANALYSIS`.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/credits?workspace_id=` | balance + recent ledger (admin) |
| `POST` | `/admin/credits/grant` | `{workspace_id?, amount, reason?}` — manual / promo top-up (admin) |
| `POST` | `/webhook/stripe` | **stub** — bearer-authed, grants `{workspace_id, credits}`. TODO: real Stripe signature verification + `checkout.session.completed` mapping. |

`POST /webhook/feedback` returns **402** when the workspace is out of credits.

## Supabase lockdown (run once)

The prototype dashboard writes `billing` / `dashboard_stats` directly from the
browser under owner-write RLS. Those must become read-only to clients; only the
backend (service role) writes them.

```sql
-- billing: clients may read, not write
DROP POLICY IF EXISTS "billing_owner"        ON public.billing;
DROP POLICY IF EXISTS "billing_owner_insert" ON public.billing;
DROP POLICY IF EXISTS "billing_owner_update" ON public.billing;
CREATE POLICY "billing_read_own" ON public.billing FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w
                 WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
-- (no INSERT/UPDATE/DELETE policy -> writes require the service role)

-- dashboard_stats: same treatment
DROP POLICY IF EXISTS "stats_owner"        ON public.dashboard_stats;
DROP POLICY IF EXISTS "stats_owner_insert" ON public.dashboard_stats;
DROP POLICY IF EXISTS "stats_owner_update" ON public.dashboard_stats;
CREATE POLICY "stats_read_own" ON public.dashboard_stats FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w
                 WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
```

Credit balance is exposed to the dashboard through the backend `GET /credits`
endpoint, not a Supabase table.
