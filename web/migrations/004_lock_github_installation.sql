-- ============================================================
--  Migration 004 — make github_installation_id backend-only, for real
-- ============================================================
-- Migration 002 tried to stop the browser from writing
-- workspaces.github_installation_id with a column-level REVOKE. In Postgres a
-- column REVOKE has no effect while the role still holds table-level UPDATE,
-- which Supabase grants to `authenticated` by default, so a signed-in user
-- could set ANY installation id on their own workspace (and have "Generate
-- fix" clone another organisation's repositories with our GitHub App).
--
-- This trigger rejects any change to the column made through the API roles
-- (anon / authenticated). The backend connects to Postgres directly as a
-- privileged role and sets it only after POST /me/github/install-callback
-- has verified, via GitHub sign-in, that the user can access the installation.
--
-- Also: one installation can belong to one workspace only.
--
-- Run once in the Supabase SQL editor, after 003.
-- ============================================================

CREATE OR REPLACE FUNCTION public.protect_github_installation_id()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
  IF current_user IN ('anon', 'authenticated') THEN
    IF TG_OP = 'INSERT' AND NEW.github_installation_id IS NOT NULL THEN
      RAISE EXCEPTION 'github_installation_id can only be set by the Prodrion backend'
        USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.github_installation_id IS DISTINCT FROM OLD.github_installation_id THEN
      RAISE EXCEPTION 'github_installation_id can only be set by the Prodrion backend'
        USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.protect_github_installation_id() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_protect_github_installation_id ON public.workspaces;
CREATE TRIGGER trg_protect_github_installation_id
  BEFORE INSERT OR UPDATE ON public.workspaces
  FOR EACH ROW EXECUTE FUNCTION public.protect_github_installation_id();

CREATE UNIQUE INDEX IF NOT EXISTS ux_workspaces_github_installation_id
  ON public.workspaces (github_installation_id)
  WHERE github_installation_id IS NOT NULL;
