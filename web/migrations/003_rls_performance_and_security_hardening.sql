-- ============================================================
--  Migration 003 — RLS performance + function security hardening
-- ============================================================
-- Fixes flagged by Supabase's own advisors after the base schema +
-- 001 + 002 were first applied:
--   security: function_search_path_mutable (set_updated_at, handle_new_user),
--             anon/authenticated_security_definer_function_executable (handle_new_user)
--   performance: auth_rls_initplan (auth.uid() re-evaluated per row — wrap in
--             a subquery so the planner caches it once per statement),
--             multiple_permissive_policies (hooks/sync_settings/api_keys/
--             webhook_secrets/suggested_features/feedback_synthesis each had
--             a redundant FOR INSERT/UPDATE/DELETE policy on top of an
--             already-covering FOR ALL one)
--
-- Safe to run on a project that deployed supabase_schema.sql + 001 + 002
-- before this fix landed. A fresh deploy from the current supabase_schema.sql
-- (+ 001, 002) already includes all of this — running 003 on top is a no-op.
--
-- Run once in the Supabase SQL editor, after 001 and 002.
-- ============================================================

-- ---- functions ---------------------------------------------
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;
REVOKE EXECUTE ON FUNCTION public.set_updated_at() FROM anon, authenticated;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  ws_id UUID;
  umeta JSONB;
  uname TEXT;
BEGIN
  umeta := COALESCE(NEW.raw_user_meta_data, '{}'::jsonb);
  uname := COALESCE(umeta->>'full_name', umeta->>'name', split_part(NEW.email, '@', 1));

  INSERT INTO public.profiles (id, full_name, first_name, last_name, avatar_url)
  VALUES (
    NEW.id, uname,
    COALESCE(umeta->>'first_name', split_part(uname, ' ', 1)),
    COALESCE(umeta->>'last_name',  CASE WHEN strpos(uname, ' ') > 0 THEN split_part(uname, ' ', 2) ELSE '' END),
    umeta->>'avatar_url'
  )
  ON CONFLICT (id) DO NOTHING;

  INSERT INTO public.workspaces (owner_id, name)
  VALUES (NEW.id, COALESCE(NULLIF(umeta->>'company',''), 'My Workspace'))
  RETURNING id INTO ws_id;

  INSERT INTO public.notification_preferences (user_id) VALUES (NEW.id);
  INSERT INTO public.ai_config (workspace_id) VALUES (ws_id);
  INSERT INTO public.sync_settings (workspace_id) VALUES (ws_id);

  INSERT INTO public.webhook_secrets (workspace_id, secret)
  VALUES (ws_id, 'whsec_' || encode(gen_random_bytes(32), 'hex'));

  INSERT INTO public.billing (workspace_id, plan, price, requests_limit, seats_limit, renews_at)
  VALUES (ws_id, 'pro', 79.00, 10000, 5, (NOW() + INTERVAL '30 days')::DATE);

  INSERT INTO public.dashboard_stats (workspace_id) VALUES (ws_id);

  INSERT INTO public.activity_log (workspace_id, user_id, type, icon, icon_color, title, description)
  VALUES (ws_id, NEW.id, 'info', 'celebration', 'primary', 'Welcome to ProdMind!', 'Your workspace is ready. Connect a data source to start generating insights.');

  RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;

-- ---- profiles / workspaces / notification_preferences / ai_config -------
DROP POLICY IF EXISTS "profiles_select_own" ON public.profiles;
DROP POLICY IF EXISTS "profiles_insert_own" ON public.profiles;
DROP POLICY IF EXISTS "profiles_update_own" ON public.profiles;
CREATE POLICY "profiles_select_own" ON public.profiles FOR SELECT USING ((select auth.uid()) = id);
CREATE POLICY "profiles_insert_own" ON public.profiles FOR INSERT WITH CHECK ((select auth.uid()) = id);
CREATE POLICY "profiles_update_own" ON public.profiles FOR UPDATE USING ((select auth.uid()) = id);

DROP POLICY IF EXISTS "workspaces_owner" ON public.workspaces;
DROP POLICY IF EXISTS "workspaces_owner_update" ON public.workspaces;
DROP POLICY IF EXISTS "workspaces_owner_insert" ON public.workspaces;
DROP POLICY IF EXISTS "workspaces_owner_delete" ON public.workspaces;
CREATE POLICY "workspaces_owner" ON public.workspaces FOR SELECT USING ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_update" ON public.workspaces FOR UPDATE USING ((select auth.uid()) = owner_id) WITH CHECK ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_insert" ON public.workspaces FOR INSERT WITH CHECK ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_delete" ON public.workspaces FOR DELETE USING ((select auth.uid()) = owner_id);

DROP POLICY IF EXISTS "notif_prefs_own" ON public.notification_preferences;
DROP POLICY IF EXISTS "notif_prefs_own_insert" ON public.notification_preferences;
DROP POLICY IF EXISTS "notif_prefs_own_update" ON public.notification_preferences;
CREATE POLICY "notif_prefs_own" ON public.notification_preferences FOR SELECT USING ((select auth.uid()) = user_id);
CREATE POLICY "notif_prefs_own_insert" ON public.notification_preferences FOR INSERT WITH CHECK ((select auth.uid()) = user_id);
CREATE POLICY "notif_prefs_own_update" ON public.notification_preferences FOR UPDATE USING ((select auth.uid()) = user_id);

DROP POLICY IF EXISTS "ai_config_owner" ON public.ai_config;
DROP POLICY IF EXISTS "ai_config_owner_insert" ON public.ai_config;
DROP POLICY IF EXISTS "ai_config_owner_update" ON public.ai_config;
CREATE POLICY "ai_config_owner" ON public.ai_config FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "ai_config_owner_insert" ON public.ai_config FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "ai_config_owner_update" ON public.ai_config FOR UPDATE
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

-- ---- hooks / sync_settings / api_keys / webhook_secrets: consolidate ----
DROP POLICY IF EXISTS "hooks_owner" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_insert" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_update" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_delete" ON public.hooks;
CREATE POLICY "hooks_owner" ON public.hooks
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP POLICY IF EXISTS "sync_settings_owner" ON public.sync_settings;
DROP POLICY IF EXISTS "sync_settings_owner_insert" ON public.sync_settings;
DROP POLICY IF EXISTS "sync_settings_owner_update" ON public.sync_settings;
CREATE POLICY "sync_settings_owner" ON public.sync_settings
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP POLICY IF EXISTS "api_keys_owner" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_insert" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_update" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_delete" ON public.api_keys;
CREATE POLICY "api_keys_owner" ON public.api_keys
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP POLICY IF EXISTS "webhook_secrets_owner" ON public.webhook_secrets;
DROP POLICY IF EXISTS "webhook_secrets_owner_insert" ON public.webhook_secrets;
DROP POLICY IF EXISTS "webhook_secrets_owner_update" ON public.webhook_secrets;
CREATE POLICY "webhook_secrets_owner" ON public.webhook_secrets
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

-- ---- activity_log / billing / invoices -----------------------------------
DROP POLICY IF EXISTS "activity_owner_select" ON public.activity_log;
DROP POLICY IF EXISTS "activity_owner_insert" ON public.activity_log;
CREATE POLICY "activity_owner_select" ON public.activity_log FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "activity_owner_insert" ON public.activity_log FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE INDEX IF NOT EXISTS idx_activity_log_user ON public.activity_log(user_id);

DROP POLICY IF EXISTS "billing_read_own" ON public.billing;
CREATE POLICY "billing_read_own" ON public.billing FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP POLICY IF EXISTS "invoices_owner" ON public.invoices;
DROP POLICY IF EXISTS "invoices_owner_insert" ON public.invoices;
CREATE POLICY "invoices_owner" ON public.invoices FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "invoices_owner_insert" ON public.invoices FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

-- ---- suggested_features / feedback_synthesis: consolidate ---------------
-- (kept as FOR ALL, not narrowed to SELECT — the dashboard's "clear insights"
-- action deletes rows from both directly from the browser.)
DROP POLICY IF EXISTS "features_owner" ON public.suggested_features;
DROP POLICY IF EXISTS "features_owner_insert" ON public.suggested_features;
DROP POLICY IF EXISTS "features_owner_update" ON public.suggested_features;
CREATE POLICY "features_owner" ON public.suggested_features
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP POLICY IF EXISTS "feedback_owner" ON public.feedback_synthesis;
DROP POLICY IF EXISTS "feedback_owner_insert" ON public.feedback_synthesis;
CREATE POLICY "feedback_owner" ON public.feedback_synthesis
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

-- ---- dashboard_stats ------------------------------------------------------
DROP POLICY IF EXISTS "stats_read_own" ON public.dashboard_stats;
CREATE POLICY "stats_read_own" ON public.dashboard_stats FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
