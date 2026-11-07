-- ============================================================
--  ProdMind — Supabase Database Schema
--  Version: 1.0  |  Cloud-portable (works local + Supabase cloud)
-- ============================================================
-- HOW TO RUN:
--   1. Open your Supabase project → SQL Editor → New query
--   2. Paste this entire file and click "Run"
--   3. All tables, RLS policies, triggers & seed logic will be created
-- ============================================================

-- ── Extensions ──────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Helper: auto-update updated_at column ───────────────────
-- SET search_path pins name resolution (Supabase linter: function_search_path_mutable).
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;
REVOKE EXECUTE ON FUNCTION public.set_updated_at() FROM anon, authenticated;

-- ============================================================
-- TABLE: profiles
-- One row per auth user. Created automatically by trigger.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.profiles (
  id            UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  full_name     TEXT,
  first_name    TEXT,
  last_name     TEXT,
  role_title    TEXT    DEFAULT 'Product Manager',
  timezone      TEXT    DEFAULT 'UTC+0 (GMT)',
  avatar_url    TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "profiles_select_own"  ON public.profiles;
DROP POLICY IF EXISTS "profiles_insert_own"  ON public.profiles;
DROP POLICY IF EXISTS "profiles_update_own"  ON public.profiles;
CREATE POLICY "profiles_select_own" ON public.profiles FOR SELECT USING ((select auth.uid()) = id);
CREATE POLICY "profiles_insert_own" ON public.profiles FOR INSERT WITH CHECK ((select auth.uid()) = id);
CREATE POLICY "profiles_update_own" ON public.profiles FOR UPDATE USING ((select auth.uid()) = id);

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON public.profiles;
CREATE TRIGGER trg_profiles_updated_at
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: workspaces
-- One workspace per user (expandable to multi-tenant later).
-- ============================================================
CREATE TABLE IF NOT EXISTS public.workspaces (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL DEFAULT 'My Workspace',
  website     TEXT,
  industry    TEXT    DEFAULT 'SaaS / Software',
  github_repo TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "workspaces_owner"        ON public.workspaces;
DROP POLICY IF EXISTS "workspaces_owner_insert"  ON public.workspaces;
CREATE POLICY "workspaces_owner"       ON public.workspaces FOR SELECT USING ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_update" ON public.workspaces FOR UPDATE USING ((select auth.uid()) = owner_id) WITH CHECK ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_insert" ON public.workspaces FOR INSERT WITH CHECK ((select auth.uid()) = owner_id);
CREATE POLICY "workspaces_owner_delete" ON public.workspaces FOR DELETE USING ((select auth.uid()) = owner_id);

DROP TRIGGER IF EXISTS trg_workspaces_updated_at ON public.workspaces;
CREATE TRIGGER trg_workspaces_updated_at
  BEFORE UPDATE ON public.workspaces
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON public.workspaces(owner_id);

-- ============================================================
-- TABLE: notification_preferences
-- One row per user. Drives email/push notification settings.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.notification_preferences (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                 UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE UNIQUE,
  weekly_digest           BOOLEAN DEFAULT TRUE,
  new_feature_suggestion  BOOLEAN DEFAULT TRUE,
  sync_failures           BOOLEAN DEFAULT TRUE,
  usage_limit_alerts      BOOLEAN DEFAULT FALSE,
  created_at              TIMESTAMPTZ DEFAULT NOW(),
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.notification_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "notif_prefs_own" ON public.notification_preferences;
DROP POLICY IF EXISTS "notif_prefs_own_insert" ON public.notification_preferences;
CREATE POLICY "notif_prefs_own"        ON public.notification_preferences FOR SELECT USING ((select auth.uid()) = user_id);
CREATE POLICY "notif_prefs_own_insert" ON public.notification_preferences FOR INSERT WITH CHECK ((select auth.uid()) = user_id);
CREATE POLICY "notif_prefs_own_update" ON public.notification_preferences FOR UPDATE USING ((select auth.uid()) = user_id);

DROP TRIGGER IF EXISTS trg_notif_updated_at ON public.notification_preferences;
CREATE TRIGGER trg_notif_updated_at
  BEFORE UPDATE ON public.notification_preferences
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: ai_config
-- AI model + parameter settings per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.ai_config (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id          UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE UNIQUE,
  selected_model        TEXT    DEFAULT 'gpt4o',
  temperature           FLOAT   DEFAULT 0.7,
  max_tokens            INTEGER DEFAULT 4096,
  response_language     TEXT    DEFAULT 'English',
  monthly_request_cap   INTEGER DEFAULT 10000,
  rate_limit            INTEGER DEFAULT 60,
  alert_at_80           BOOLEAN DEFAULT TRUE,
  conversation_logging  BOOLEAN DEFAULT TRUE,
  log_retention         TEXT    DEFAULT '90 Days',
  created_at            TIMESTAMPTZ DEFAULT NOW(),
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.ai_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "ai_config_owner" ON public.ai_config;
DROP POLICY IF EXISTS "ai_config_owner_insert" ON public.ai_config;
CREATE POLICY "ai_config_owner" ON public.ai_config
  FOR SELECT USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "ai_config_owner_insert" ON public.ai_config
  FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "ai_config_owner_update" ON public.ai_config
  FOR UPDATE USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP TRIGGER IF EXISTS trg_ai_config_updated_at ON public.ai_config;
CREATE TRIGGER trg_ai_config_updated_at
  BEFORE UPDATE ON public.ai_config
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: hooks
-- External integrations (App Store, Zendesk, Intercom, etc.)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.hooks (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  type            TEXT NOT NULL,                  -- appstore | playstore | zendesk | intercom | slack | hubspot | …
  name            TEXT NOT NULL,
  status          TEXT    DEFAULT 'disconnected', -- live | syncing | disconnected | error
  config          JSONB   DEFAULT '{}',           -- type-specific: app_id, subdomain, api_key, etc.
  last_sync_at    TIMESTAMPTZ,
  reviews_synced  INTEGER DEFAULT 0,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.hooks ENABLE ROW LEVEL SECURITY;
-- A single FOR ALL policy (USING doubles as WITH CHECK when omitted) covers
-- every command without the per-command duplicates the Supabase linter flags
-- as "multiple permissive policies" (each one otherwise re-evaluated per row).
DROP POLICY IF EXISTS "hooks_owner" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_insert" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_update" ON public.hooks;
DROP POLICY IF EXISTS "hooks_owner_delete" ON public.hooks;
CREATE POLICY "hooks_owner" ON public.hooks
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP TRIGGER IF EXISTS trg_hooks_updated_at ON public.hooks;
CREATE TRIGGER trg_hooks_updated_at
  BEFORE UPDATE ON public.hooks
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_hooks_workspace ON public.hooks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_hooks_status    ON public.hooks(workspace_id, status);

-- ============================================================
-- TABLE: sync_settings
-- Global hook/sync preferences per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.sync_settings (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE UNIQUE,
  sync_frequency    TEXT    DEFAULT 'Every hour',
  min_star_rating   TEXT    DEFAULT '2★ and above',
  auto_tag          BOOLEAN DEFAULT TRUE,
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.sync_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sync_settings_owner" ON public.sync_settings;
DROP POLICY IF EXISTS "sync_settings_owner_insert" ON public.sync_settings;
DROP POLICY IF EXISTS "sync_settings_owner_update" ON public.sync_settings;
CREATE POLICY "sync_settings_owner" ON public.sync_settings
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP TRIGGER IF EXISTS trg_sync_settings_updated_at ON public.sync_settings;
CREATE TRIGGER trg_sync_settings_updated_at
  BEFORE UPDATE ON public.sync_settings
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: api_keys
-- User-generated API keys (hashed). Full key shown ONCE.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.api_keys (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  key_prefix    TEXT NOT NULL,               -- e.g. "pm_live_"
  key_masked    TEXT NOT NULL,               -- e.g. "pm_live_••••••••••3f9a"
  key_hash      TEXT NOT NULL,               -- SHA-256 of the full key — NEVER store plaintext
  environment   TEXT DEFAULT 'Production',   -- Production | Development | Staging
  expiry        TEXT DEFAULT 'Never',
  status        TEXT DEFAULT 'active',       -- active | revoked
  last_used_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "api_keys_owner" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_insert" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_update" ON public.api_keys;
DROP POLICY IF EXISTS "api_keys_owner_delete" ON public.api_keys;
CREATE POLICY "api_keys_owner" ON public.api_keys
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP TRIGGER IF EXISTS trg_api_keys_updated_at ON public.api_keys;
CREATE TRIGGER trg_api_keys_updated_at
  BEFORE UPDATE ON public.api_keys
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_api_keys_workspace ON public.api_keys(workspace_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_status    ON public.api_keys(workspace_id, status);

-- ============================================================
-- TABLE: webhook_secrets
-- One secret per workspace. Reveal/rotate from Keys page.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.webhook_secrets (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE UNIQUE,
  secret        TEXT NOT NULL,               -- "whsec_<hex>" — protected by RLS
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.webhook_secrets ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "webhook_secrets_owner" ON public.webhook_secrets;
DROP POLICY IF EXISTS "webhook_secrets_owner_insert" ON public.webhook_secrets;
DROP POLICY IF EXISTS "webhook_secrets_owner_update" ON public.webhook_secrets;
CREATE POLICY "webhook_secrets_owner" ON public.webhook_secrets
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

DROP TRIGGER IF EXISTS trg_webhook_secrets_updated_at ON public.webhook_secrets;
CREATE TRIGGER trg_webhook_secrets_updated_at
  BEFORE UPDATE ON public.webhook_secrets
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: activity_log
-- Append-only timeline. No UPDATE/DELETE on this table.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.activity_log (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  user_id       UUID REFERENCES auth.users(id),
  type          TEXT NOT NULL,  -- sync | integration | feature | warning | error | info
  icon          TEXT DEFAULT 'info',
  icon_color    TEXT DEFAULT 'primary',  -- primary | blue | amber | red
  title         TEXT NOT NULL,
  description   TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.activity_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "activity_owner_select" ON public.activity_log;
DROP POLICY IF EXISTS "activity_owner_insert" ON public.activity_log;
CREATE POLICY "activity_owner_select" ON public.activity_log FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "activity_owner_insert" ON public.activity_log FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

CREATE INDEX IF NOT EXISTS idx_activity_workspace ON public.activity_log(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_user ON public.activity_log(user_id);

-- ============================================================
-- TABLE: billing
-- One billing record per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.billing (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id        UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE UNIQUE,
  plan                TEXT    DEFAULT 'pro',        -- starter | pro | enterprise
  price               NUMERIC DEFAULT 79.00,
  requests_used       INTEGER DEFAULT 0,
  requests_limit      INTEGER DEFAULT 10000,
  seats_used          INTEGER DEFAULT 1,
  seats_limit         INTEGER DEFAULT 5,
  renews_at           DATE,
  payment_last4       TEXT,
  payment_brand       TEXT    DEFAULT 'Visa',
  payment_expiry      TEXT,
  payment_is_default  BOOLEAN DEFAULT TRUE,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.billing ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "billing_owner" ON public.billing;
CREATE POLICY "billing_owner" ON public.billing
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
CREATE POLICY "billing_owner_insert" ON public.billing
  FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
CREATE POLICY "billing_owner_update" ON public.billing
  FOR UPDATE USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));

DROP TRIGGER IF EXISTS trg_billing_updated_at ON public.billing;
CREATE TRIGGER trg_billing_updated_at
  BEFORE UPDATE ON public.billing
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- TABLE: invoices
-- Historical billing records per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.invoices (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  period_label  TEXT NOT NULL,               -- e.g. "February 2026"
  plan_name     TEXT NOT NULL,               -- e.g. "Pro Plan"
  amount        NUMERIC NOT NULL,
  status        TEXT DEFAULT 'paid',         -- paid | pending | failed
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "invoices_owner" ON public.invoices;
DROP POLICY IF EXISTS "invoices_owner_insert" ON public.invoices;
CREATE POLICY "invoices_owner" ON public.invoices FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));
CREATE POLICY "invoices_owner_insert" ON public.invoices FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

CREATE INDEX IF NOT EXISTS idx_invoices_workspace ON public.invoices(workspace_id, created_at DESC);

-- ============================================================
-- TABLE: suggested_features
-- AI-generated feature recommendations per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.suggested_features (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  description   TEXT,
  confidence    INTEGER DEFAULT 80,           -- 0–100
  impact_level  TEXT    DEFAULT 'medium',     -- high | medium | low
  sources_count INTEGER DEFAULT 0,
  icon          TEXT    DEFAULT 'auto_awesome',
  is_dismissed  BOOLEAN DEFAULT FALSE,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.suggested_features ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "features_owner" ON public.suggested_features;
DROP POLICY IF EXISTS "features_owner_insert" ON public.suggested_features;
DROP POLICY IF EXISTS "features_owner_update" ON public.suggested_features;
CREATE POLICY "features_owner" ON public.suggested_features
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

CREATE INDEX IF NOT EXISTS idx_features_workspace ON public.suggested_features(workspace_id, created_at DESC);

-- ============================================================
-- TABLE: feedback_synthesis
-- AI-generated synthesized insights per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.feedback_synthesis (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  type          TEXT NOT NULL,   -- pain_point | expansion | churn_alert | insight
  label         TEXT,
  content       TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.feedback_synthesis ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "feedback_owner" ON public.feedback_synthesis;
DROP POLICY IF EXISTS "feedback_owner_insert" ON public.feedback_synthesis;
CREATE POLICY "feedback_owner" ON public.feedback_synthesis
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = (select auth.uid())));

CREATE INDEX IF NOT EXISTS idx_feedback_workspace ON public.feedback_synthesis(workspace_id);

-- ============================================================
-- TABLE: dashboard_stats
-- Cached signal/metric counters per workspace.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.dashboard_stats (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id          UUID NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE UNIQUE,
  signals_processed     INTEGER DEFAULT 0,
  signals_change_pct    FLOAT   DEFAULT 0,
  feature_ops           INTEGER DEFAULT 0,
  feature_ops_change    FLOAT   DEFAULT 0,
  active_experiments    INTEGER DEFAULT 0,
  ai_recs               INTEGER DEFAULT 0,
  ai_recs_new_today     INTEGER DEFAULT 0,
  updated_at            TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.dashboard_stats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "stats_owner" ON public.dashboard_stats;
CREATE POLICY "stats_owner" ON public.dashboard_stats
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
CREATE POLICY "stats_owner_insert" ON public.dashboard_stats FOR INSERT
  WITH CHECK (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));
CREATE POLICY "stats_owner_update" ON public.dashboard_stats FOR UPDATE
  USING (EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace_id AND w.owner_id = auth.uid()));

-- ============================================================
-- FUNCTION + TRIGGER: on_auth_user_created
-- Bootstraps all default rows whenever a user signs up.
-- ============================================================
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  ws_id UUID;
  umeta JSONB;
  uname TEXT;
BEGIN
  umeta := COALESCE(NEW.raw_user_meta_data, '{}'::jsonb);
  uname := COALESCE(umeta->>'full_name', umeta->>'name', split_part(NEW.email, '@', 1));

  -- 1. Profile
  INSERT INTO public.profiles (id, full_name, first_name, last_name, avatar_url)
  VALUES (
    NEW.id,
    uname,
    COALESCE(umeta->>'first_name', split_part(uname, ' ', 1)),
    COALESCE(umeta->>'last_name',  CASE WHEN strpos(uname, ' ') > 0 THEN split_part(uname, ' ', 2) ELSE '' END),
    umeta->>'avatar_url'
  )
  ON CONFLICT (id) DO NOTHING;

  -- 2. Workspace
  INSERT INTO public.workspaces (owner_id, name)
  VALUES (NEW.id, COALESCE(NULLIF(umeta->>'company',''), 'My Workspace'))
  RETURNING id INTO ws_id;

  -- 3. Notification preferences (all defaults)
  INSERT INTO public.notification_preferences (user_id) VALUES (NEW.id);

  -- 4. AI config defaults
  INSERT INTO public.ai_config (workspace_id) VALUES (ws_id);

  -- 5. Sync settings defaults
  INSERT INTO public.sync_settings (workspace_id) VALUES (ws_id);

  -- 6. Webhook secret (random 32-byte hex)
  INSERT INTO public.webhook_secrets (workspace_id, secret)
  VALUES (ws_id, 'whsec_' || encode(gen_random_bytes(32), 'hex'));

  -- 7. Billing (Pro plan trial defaults)
  INSERT INTO public.billing (workspace_id, plan, price, requests_limit, seats_limit, renews_at)
  VALUES (ws_id, 'pro', 79.00, 10000, 5, (NOW() + INTERVAL '30 days')::DATE);

  -- 8. Dashboard stats — start at zero; the backend fills these in.
  INSERT INTO public.dashboard_stats (workspace_id)
  VALUES (ws_id);

  -- 9. Suggested features + 10. feedback synthesis: generated by the backend
  --    from real data, not seeded here.

  -- 11. Welcome activity entry
  INSERT INTO public.activity_log (workspace_id, user_id, type, icon, icon_color, title, description)
  VALUES (ws_id, NEW.id, 'info', 'celebration', 'primary', 'Welcome to ProdMind!', 'Your workspace is ready. Connect a data source to start generating insights.');

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Trigger invocation does not require EXECUTE privilege on the function (it
-- runs under the trigger mechanism as the function owner via SECURITY
-- DEFINER); revoking PUBLIC/anon/authenticated EXECUTE only blocks calling it
-- directly via /rest/v1/rpc/handle_new_user (Supabase linter:
-- anon/authenticated_security_definer_function_executable).
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;

-- ============================================================
-- END OF SCHEMA
-- ============================================================
-- Summary of tables created:
--   profiles               — user profile info (name, timezone, role)
--   workspaces             — workspace settings (name, website, industry)
--   notification_preferences — email/push toggles per user
--   ai_config              — model selection + parameters per workspace
--   hooks                  — external integrations (App Store, Zendesk…)
--   sync_settings          — global sync frequency + filter settings
--   api_keys               — developer API keys (hashed, never plaintext)
--   webhook_secrets        — per-workspace webhook signing secret
--   activity_log           — immutable event timeline
--   billing                — plan + payment method + usage counters
--   invoices               — historical billing records
--   suggested_features     — AI-generated feature recommendations
--   feedback_synthesis     — AI-generated user feedback insights
--   dashboard_stats        — cached analytics counters
-- ============================================================
