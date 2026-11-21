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


# --- multi-turn (repo fix agent) -------------------------------------------

TURNS = [
    {"role": "user", "content": "find the bug"},
    {"role": "assistant", "content": '{"actions": []}'},
    {"role": "user", "content": "tool output"},
]


def test_fake_llm_script_is_replayed_then_falls_back_to_canned():
    provider = FakeLLM(canned="done", script=["one", "two"])
    assert [provider.generate("p") for _ in range(3)] == ["one", "two", "done"]


def test_converse_default_flattens_the_transcript_in_order():
    provider = FakeLLM(canned="reply")
    assert provider.converse("sys", TURNS, temperature=0.3, json_mode=True) == "reply"
    (call,) = provider.calls
    assert call["system"] == "sys" and call["temperature"] == 0.3
    assert call["prompt"] == (
        "### USER\nfind the bug\n\n"
        '### ASSISTANT\n{"actions": []}\n\n'
        "### USER\ntool output\n\n"
        "### ASSISTANT"
    )


def test_gemini_converse_sends_native_turns_and_json_mode(monkeypatch):
    """Against a stand-in for the google-genai SDK surface that llm/gemini.py uses."""
    import sys
    import types as pytypes

    sent = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            sent.update(model=model, contents=contents, config=config)
            return pytypes.SimpleNamespace(text='  {"actions": []}  ')

    class _Part:
        @staticmethod
        def from_text(*, text):
            return {"text": text}

    sdk_types = pytypes.SimpleNamespace(
        GenerateContentConfig=lambda **kw: pytypes.SimpleNamespace(**kw),
        Content=lambda *, role, parts: {"role": role, "parts": parts},
        Part=_Part,
    )
    genai = pytypes.ModuleType("google.genai")
    genai.Client = lambda api_key: pytypes.SimpleNamespace(models=_Models())
    genai.types = sdk_types
    google = pytypes.ModuleType("google")
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)

    from llm.gemini import GeminiLLM

    out = GeminiLLM("key", "gemini-test").converse("sys", TURNS, json_mode=True)

    assert out == '{"actions": []}'
    assert sent["model"] == "gemini-test"
    assert [c["role"] for c in sent["contents"]] == ["user", "model", "user"]
    assert sent["contents"][2]["parts"] == [{"text": "tool output"}]
    assert sent["config"].system_instruction == "sys"
    assert sent["config"].response_mime_type == "application/json"


def test_gemini_retry_hint_is_parsed_from_quota_errors():
    from llm.gemini import parse_retry_after

    daily = (
        "429 RESOURCE_EXHAUSTED. {'error': {'message': 'Quota exceeded ... per day limit "
        "exceeded on Free Tier.\\nPlease retry in 10h50m41.841524158s.', 'details': "
        "[{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '39041s'}]}}"
    )
    assert parse_retry_after(daily) == 39041.0
    assert parse_retry_after("Please retry in 10h50m41.5s.") == 10 * 3600 + 50 * 60 + 41.5
    assert parse_retry_after("Please retry in 23.4s.") == 23.4
    assert parse_retry_after("503 UNAVAILABLE. high demand, try again later") is None
    assert FakeLLM().retry_after_seconds(RuntimeError("x")) is None  # default: unknown


# --- Z.ai --------------------------------------------------------------


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text, self.reason = str(payload), "x"

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses):
        self.responses, self.sent = list(responses), []

    def post(self, url, *, json, headers, timeout):
        self.sent.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _zai_ok(content, finish="stop", usage=None):
    return _Resp(200, {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20,
                           "prompt_tokens_details": {"cached_tokens": 60}},
    })  # fmt: skip


def test_zai_converse_sends_openai_style_turns_json_mode_and_reasoning_effort():
    from llm.zai import ZaiLLM

    http = _Session(_zai_ok('  {"actions": []} '), _zai_ok("plain"))
    zai = ZaiLLM("secret", "glm-5.3", reasoning_effort="low", session=http)

    assert zai.converse("sys", TURNS, json_mode=True) == '{"actions": []}'
    sent = http.sent[0]
    assert sent["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer secret"
    body = sent["json"]
    assert body["model"] == "glm-5.3"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert body["messages"][3]["content"] == "tool output"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "low"

    assert zai.generate("hi") == "plain"
    assert "response_format" not in http.sent[1]["json"]
    assert zai.usage == {"requests": 2, "prompt_tokens": 200, "cached_tokens": 120,
                         "completion_tokens": 40}  # fmt: skip


def test_zai_errors_tell_a_short_retry_from_an_empty_balance():
    from llm.zai import ZaiError, ZaiLLM

    http = _Session(
        _Resp(429, {"error": {"code": "1113", "message": "Insufficient balance"}}),
        _Resp(429, {"error": {"code": "1302", "message": "Rate limit reached"}}),
        _Resp(500, {"error": {"code": "1234", "message": "Network error"}}),
        _zai_ok("", finish="sensitive"),
    )
    zai = ZaiLLM("k", "glm-5.3", session=http)
    errors = []
    for _ in range(4):
        with pytest.raises(ZaiError) as caught:
            zai.converse("sys", TURNS)
        errors.append(caught.value)

    assert [e.code for e in errors] == [1113, 1302, 1234, 1301]
    # empty balance: long wait -> the fix agent stops now and the job is postponed
    assert zai.retry_after_seconds(errors[0]) > 120
    # rate limit / server error: unknown -> the agent's normal short retries
    assert [zai.retry_after_seconds(e) for e in errors[1:]] == [None, None, None]


def test_zai_requires_a_key_and_is_not_an_embedder(monkeypatch):
    from llm.zai import ZaiLLM

    with pytest.raises(ValueError, match="ZAI_API_KEY"):
        ZaiLLM("", "glm-5.3")

    from config import get_settings

    monkeypatch.setenv("PRODRION_LLM_PROVIDER", "zai")
    monkeypatch.setenv("PRODRION_ZAI_API_KEY", "k")
    monkeypatch.setenv("PRODRION_EMBED_PROVIDER", "fake")
    get_settings.cache_clear()
    llm.reset_cache()
    try:
        assert type(llm.get_llm()).__name__ == "ZaiLLM"
        assert isinstance(llm.get_embedder(), FakeEmbedder)
        monkeypatch.delenv("PRODRION_EMBED_PROVIDER")
        get_settings.cache_clear()
        llm.reset_cache()
        with pytest.raises(ValueError, match="EMBED_PROVIDER"):
            llm.get_embedder()
    finally:
        get_settings.cache_clear()
        llm.reset_cache()


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
