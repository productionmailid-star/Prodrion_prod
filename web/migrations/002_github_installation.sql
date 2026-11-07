-- ============================================================
--  Migration 002 — track each workspace's GitHub App installation
-- ============================================================
-- Set once the workspace owner completes the GitHub App install flow
-- (POST /me/github/install-callback). Nullable: a workspace with no
-- installation yet simply can't trigger "Generate fix".
--
-- Run once in the Supabase SQL editor, after 001_billing_stats_readonly.sql.
-- ============================================================

ALTER TABLE public.workspaces
  ADD COLUMN IF NOT EXISTS github_installation_id TEXT;

-- The existing "workspaces_owner_update" RLS policy lets the owner UPDATE
-- their own row (fine — they can rename it, change github_repo, etc). This
-- column-level privilege additionally stops the browser (the `authenticated`
-- role PostgREST runs as) from writing github_installation_id specifically;
-- only the backend's direct Postgres connection (supabase_sync.py, which
-- connects as a role with full table privileges) can set it, after verifying
-- the GitHub App install callback belongs to the signed-in owner.
REVOKE UPDATE (github_installation_id) ON public.workspaces FROM authenticated;
