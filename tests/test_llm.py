"""Tests for the llm provider abstraction (fake provider + selection)."""

from __future__ import annotations

import pytest

import llm
from llm.base import Embedder, LLMProvider
from llm.fake import FakeEmbedder, FakeLLM


@pytest.fixture(autouse=True)
def _reset_llm_cache():
    llm.reset_cache()
    yield
    llm.reset_cache()


# --- fake provider ---------------------------------------------------------


def test_fake_llm_records_calls_and_returns_canned():
    provider = FakeLLM(canned="hello")
    assert provider.generate("prompt", system="sys", temperature=0.4) == "hello"
    assert provider.calls == [{"prompt": "prompt", "system": "sys", "temperature": 0.4}]


def test_chat_reattaches_prefill():
    provider = FakeLLM(canned="42")
    assert provider.chat("sys", "how many?", prefill="LINE: ") == "LINE: 42"


def test_chat_does_not_double_prefill():
    provider = FakeLLM(canned="LINE: 42")
    assert provider.chat("sys", "how many?", prefill="LINE: ") == "LINE: 42"


def test_fake_embedder_is_deterministic_and_sized():
    emb = FakeEmbedder(dim=16)
    v1 = emb.embed_one("login is broken")
    v2 = emb.embed_one("login is broken")
    v3 = emb.embed_one("export to csv fails")
    assert len(v1) == 16
    assert v1 == v2
    assert v1 != v3
    assert all(-1.0 <= x <= 1.0 for x in v1)


# --- provider selection --------------------------------------------------


def test_get_llm_and_embedder_use_configured_provider(monkeypatch):
    monkeypatch.setenv("PRODRION_LLM_PROVIDER", "fake")
    from config import get_settings

    get_settings.cache_clear()
    llm.reset_cache()

    assert isinstance(llm.get_llm(), LLMProvider)
    assert isinstance(llm.get_embedder(), Embedder)
    # singletons
    assert llm.get_llm() is llm.get_llm()


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("PRODRION_LLM_PROVIDER", "fake")
    from config import get_settings

    get_settings.cache_clear()
    llm.reset_cache()
    with pytest.raises(ValueError):
        llm._build_llm("nope")


# --- feedback_platform wiring -------------------------------------------


def test_call_llm_strips_json_fences(monkeypatch):
    import feedback_platform as fp

    monkeypatch.setattr(fp, "get_llm", lambda: FakeLLM(canned='```json\n{"labels": ["A"]}\n```'))
    assert fp.call_llm("classify this", expect_json=True) == '{"labels": ["A"]}'


def test_call_llm_returns_empty_on_error(monkeypatch):
    import feedback_platform as fp

    class Boom(FakeLLM):
        def generate(self, *a, **k):
            raise RuntimeError("provider down")

    monkeypatch.setattr(fp, "get_llm", lambda: Boom())
    assert fp.call_llm("anything") == ""
