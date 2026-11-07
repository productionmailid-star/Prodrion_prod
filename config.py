"""Central runtime configuration for Prodrion.

Every tunable value and secret is defined here as a field on :class:`Settings`
and read from the environment (or a local ``.env`` file) with the prefix
``PRODRION_``. See ``.env.example`` for the full list.

Import the shared instance:

    from config import settings

In production (``PRODRION_ENVIRONMENT=production``) the configuration is
validated on load and the process refuses to start on an incoherent setup
(demo data enabled, or an integration URL set without its credentials).
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["gemini", "ollama", "fake"]


class Environment(str, Enum):
    development = "development"
    production = "production"
    test = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRODRION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.development

    # ---- Observability ----
    service_name: str = "prodrion"
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"  # production should use "json"
    sentry_dsn: str = ""  # when set, errors are reported to Sentry

    # ---- Core API service ----
    api_host: str = "127.0.0.1"  # set to 0.0.0.0 in production, behind a proxy
    api_port: int = 8000
    db_path: str = "feedback_platform.db"
    # How long a writer waits for a competing lock before "database is locked".
    sqlite_busy_timeout_seconds: float = Field(5.0, gt=0.0)

    # ---- API security ----
    # Bearer token for the admin/query endpoints. Required in production.
    api_auth_token: str = ""
    # Comma-separated browser origins allowed to call the API (CORS).
    cors_allow_origins: str = ""
    # Supabase project JWT secret (HS256) — verifies dashboard user tokens.
    supabase_jwt_secret: str = ""
    # Direct Postgres connection string for the Supabase project (server-only;
    # read per-workspace integration config, write computed dashboard results).
    # See supabase_sync.py. Blank disables all dashboard sync (dev/tests).
    supabase_db_url: str = ""
    # Shared secret for POST /webhook/feedback (X-Webhook-Secret header).
    webhook_secret: str = ""
    # Slack signing secret for POST /webhook/slack signature verification.
    slack_signing_secret: str = ""
    # Reject request bodies larger than this (bytes).
    max_request_bytes: int = Field(1_048_576, ge=1024)
    # Hard row cap and wall-clock budget for the natural-language /query endpoint.
    query_row_cap: int = Field(100, ge=1, le=10_000)
    query_timeout_seconds: float = Field(5.0, gt=0.0)
    # repo-agent may only analyse paths under this directory (blank -> the
    # bundled repo-agent/ directory, resolved at runtime).
    repo_agent_allowed_base: str = ""

    # ---- GitHub App (repo read + ticket/PR creation) ----
    github_api_url: str = "https://api.github.com"
    github_app_id: str = ""
    # PEM contents, or a path to a .pem file.
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    github_clone_max_bytes: int = Field(50_000_000, ge=1_000_000)
    github_clone_timeout_seconds: float = Field(120.0, gt=0.0)
    # Scratch directory that user-repo clones are confined to.
    github_clone_dir: str = "var/gh-clones"

    # ---- Credits / billing ----
    # When false, charge() is a no-op (dev / single-tenant without billing).
    credits_enabled: bool = False
    default_workspace_id: str = "default"
    credits_per_feedback: int = Field(1, ge=0)
    credits_per_repo_analysis: int = Field(25, ge=0)

    # ---- Background worker / job queue ----
    worker_poll_interval_seconds: float = Field(1.0, gt=0.0)
    job_max_attempts: int = Field(5, ge=1)
    job_backoff_base_seconds: float = Field(5.0, gt=0.0)
    job_backoff_max_seconds: float = Field(3600.0, gt=0.0)
    # Run the worker loop + scheduler in a thread inside the API process.
    # Handy for local dev; in production run worker.py as its own service.
    inline_worker: bool = False

    # ---- LLM / embeddings ----
    # Provider for both generation and embeddings. "fake" is test-only.
    llm_provider: LLMProviderName = "gemini"

    # Gemini (free-tier API) — used when llm_provider == "gemini".
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_embed_model: str = "text-embedding-004"

    # Ollama (local) — used when llm_provider == "ollama".
    ollama_url: str = "http://localhost:11434/api/generate"
    ollama_model: str = "llama3.2:latest"
    ollama_embed_model: str = "nomic-embed-text"

    # ---- Tuning knobs ----
    similarity_threshold: float = Field(0.85, ge=0.0, le=1.0)
    velocity_spike_mult: float = Field(2.0, gt=0.0)
    drift_min_cluster_size: int = Field(3, ge=2)

    # ---- Demo / bootstrap ----
    load_demo_data: bool = False
    # Product docs / help-centre URL used to auto-build the taxonomy when empty.
    taxonomy_bootstrap_url: str = ""

    # ---- Slack ----
    # slack_bot_token is used for admin alerts (send_slack_alert) only. The
    # per-workspace feedback connector (poll_slack_channel) takes its own bot
    # token + channel from that workspace's `hooks.config` instead.
    slack_bot_token: str = ""
    slack_alert_channel: str = "#alerts"

    # ---- Jira / HubSpot ----
    # Global (admin's own workspace) — no per-customer connection UI exists
    # yet for these two, unlike Zendesk/App Store/Play Store/Slack which are
    # configured per-workspace via the dashboard's `hooks` table.
    jira_url: str = ""
    jira_email: str = ""
    jira_token: str = ""
    jira_project: str = "PROJ"
    hubspot_api_key: str = ""

    # ---- Bucket builder ----
    # Minimum feedback_count in a taxonomy label before rebuild_buckets()
    # surfaces it as a suggested fix on the dashboard.
    goal_min_cluster_size: int = Field(2, ge=1)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.production

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL")
        return value

    @field_validator("taxonomy_bootstrap_url", "jira_url")
    @classmethod
    def _blank_or_http_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("must be blank or an http(s) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate_production_coherence(self) -> Settings:
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.load_demo_data:
            problems.append("PRODRION_LOAD_DEMO_DATA must be false in production")
        if self.llm_provider == "fake":
            problems.append("PRODRION_LLM_PROVIDER=fake is not allowed in production")
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            problems.append("PRODRION_LLM_PROVIDER=gemini requires PRODRION_GEMINI_API_KEY")
        if not self.api_auth_token:
            problems.append("PRODRION_API_AUTH_TOKEN is required in production")
        if not self.webhook_secret:
            problems.append("PRODRION_WEBHOOK_SECRET is required in production")
        if not self.supabase_db_url:
            problems.append("PRODRION_SUPABASE_DB_URL is required in production")
        if self.jira_url and not (self.jira_email and self.jira_token):
            problems.append("PRODRION_JIRA_URL is set but JIRA_EMAIL/JIRA_TOKEN are missing")

        if problems:
            raise ValueError("Incoherent production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached)."""
    return Settings()


settings = get_settings()
