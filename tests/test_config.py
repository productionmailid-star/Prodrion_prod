"""Tests for config.Settings: defaults, env parsing, and production fail-fast."""

from __future__ import annotations

import pytest

from config import Environment, Settings

# conftest sets PRODRION_LLM_PROVIDER=fake for the run; production checks below
# pass an explicit coherent baseline so they exercise one rule at a time.
_PROD_LLM = {
    "llm_provider": "gemini",
    "gemini_api_key": "test-key",
    "api_auth_token": "admin-token",
    "webhook_secret": "hook-secret",
}


def _settings(**overrides):
    # _env_file=None keeps a developer's local .env from leaking into the test.
    return Settings(_env_file=None, **overrides)


def test_defaults(monkeypatch):
    monkeypatch.delenv("PRODRION_LLM_PROVIDER", raising=False)
    s = _settings()
    assert s.environment is Environment.development
    assert s.is_production is False
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 8000
    assert s.similarity_threshold == 0.85
    assert s.load_demo_data is False
    assert s.taxonomy_bootstrap_url == ""
    assert s.llm_provider == "gemini"


def test_env_prefix_is_read(monkeypatch):
    monkeypatch.setenv("PRODRION_API_PORT", "9001")
    monkeypatch.setenv("PRODRION_GEMINI_MODEL", "custom-model")
    monkeypatch.setenv("PRODRION_LOAD_DEMO_DATA", "true")
    s = _settings()
    assert s.api_port == 9001
    assert s.gemini_model == "custom-model"
    assert s.load_demo_data is True


def test_unknown_llm_provider_rejected():
    with pytest.raises(ValueError):
        _settings(llm_provider="anthropic")


def test_similarity_threshold_bounds():
    with pytest.raises(ValueError):
        _settings(similarity_threshold=1.5)
    with pytest.raises(ValueError):
        _settings(similarity_threshold=-0.1)


def test_url_fields_reject_non_http():
    with pytest.raises(ValueError):
        _settings(zendesk_url="ftp://example.com")


def test_url_fields_strip_trailing_slash():
    assert _settings(jira_url="https://x.atlassian.net/").jira_url == "https://x.atlassian.net"


# --- production fail-fast -----------------------------------------------------


def test_production_rejects_demo_data():
    with pytest.raises(ValueError, match="LOAD_DEMO_DATA"):
        _settings(environment="production", load_demo_data=True, **_PROD_LLM)


def test_production_rejects_fake_provider():
    with pytest.raises(ValueError, match="fake"):
        _settings(environment="production", llm_provider="fake")


def test_production_requires_gemini_key():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        _settings(environment="production", llm_provider="gemini", gemini_api_key="")


def test_production_rejects_partial_zendesk_credentials():
    with pytest.raises(ValueError, match="ZENDESK"):
        _settings(environment="production", zendesk_url="https://co.zendesk.com", **_PROD_LLM)


def test_production_rejects_partial_jira_credentials():
    with pytest.raises(ValueError, match="JIRA"):
        _settings(environment="production", jira_url="https://co.atlassian.net", **_PROD_LLM)


def test_production_accepts_coherent_config():
    s = _settings(
        environment="production",
        api_host="0.0.0.0",
        zendesk_url="https://co.zendesk.com",
        zendesk_email="ops@co.com",
        zendesk_token="tok",
        **_PROD_LLM,
    )
    assert s.is_production is True
    assert s.zendesk_url == "https://co.zendesk.com"
