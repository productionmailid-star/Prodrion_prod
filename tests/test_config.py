"""Tests for config.Settings: defaults, env parsing, and production fail-fast."""

from __future__ import annotations

import pytest

from config import Environment, Settings


def _settings(**overrides):
    # _env_file=None keeps a developer's local .env from leaking into the test.
    return Settings(_env_file=None, **overrides)


def test_defaults():
    s = _settings()
    assert s.environment is Environment.development
    assert s.is_production is False
    assert s.api_host == "127.0.0.1"
    assert s.api_port == 8000
    assert s.similarity_threshold == 0.85
    assert s.load_demo_data is False
    assert s.taxonomy_bootstrap_url == ""


def test_env_prefix_is_read(monkeypatch):
    monkeypatch.setenv("PRODRION_API_PORT", "9001")
    monkeypatch.setenv("PRODRION_OLLAMA_MODEL", "custom-model")
    monkeypatch.setenv("PRODRION_LOAD_DEMO_DATA", "true")
    s = _settings()
    assert s.api_port == 9001
    assert s.ollama_model == "custom-model"
    assert s.load_demo_data is True


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
        _settings(environment="production", load_demo_data=True)


def test_production_rejects_partial_zendesk_credentials():
    with pytest.raises(ValueError, match="ZENDESK"):
        _settings(environment="production", zendesk_url="https://co.zendesk.com")


def test_production_rejects_partial_jira_credentials():
    with pytest.raises(ValueError, match="JIRA"):
        _settings(environment="production", jira_url="https://co.atlassian.net")


def test_production_accepts_coherent_config():
    s = _settings(
        environment="production",
        api_host="0.0.0.0",
        zendesk_url="https://co.zendesk.com",
        zendesk_email="ops@co.com",
        zendesk_token="tok",
    )
    assert s.is_production is True
    assert s.zendesk_url == "https://co.zendesk.com"
