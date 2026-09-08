-- ============================================================
--  Migration 001 — make billing / dashboard_stats read-only to clients
-- ============================================================
-- The prototype let the browser write these tables directly (a user could set
-- their own plan / limits / usage). After this migration clients may only
-- SELECT them; all writes require the service role (the backend).
--
-- Run once in the Supabase SQL editor, after supabase_schema.sql.
-- ============================================================

-- ---- billing ----------------------------------------------
DROP POLICY IF EXISTS "billing_owner"        ON public.billing;
DROP POLICY IF EXISTS "billing_owner_insert" ON public.billing;
DROP POLICY IF EXISTS "billing_owner_update" ON public.billing;
CREATE POLICY "billing_read_own" ON public.billing FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w
                 WHERE w.id = workspace_id AND w.owner_id = auth.uid()));

-- ---- dashboard_stats -------------------------------------
DROP POLICY IF EXISTS "stats_owner"        ON public.dashboard_stats;
DROP POLICY IF EXISTS "stats_owner_insert" ON public.dashboard_stats;
DROP POLICY IF EXISTS "stats_owner_update" ON public.dashboard_stats;
CREATE POLICY "stats_read_own" ON public.dashboard_stats FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w
                 WHERE w.id = workspace_id AND w.owner_id = auth.uid()));

-- Credit balance is served by the backend (GET /me/credits), not a table here.
-- The dashboard's ensureWorkspace() fallback must stop upserting billing /
-- dashboard_stats — those rows now come from the on_auth_user_created trigger
-- and the backend only.
