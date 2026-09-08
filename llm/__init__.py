"""LLM provider selection.

    from llm import get_llm, get_embedder

Both return process-wide singletons for the provider named by
``PRODRION_LLM_PROVIDER`` (``gemini`` | ``ollama`` | ``fake``). Call
:func:`reset_cache` in tests after changing the setting.
"""

from __future__ import annotations

from config import get_settings

from .base import Embedder, LLMProvider
from .fake import FakeEmbedder, FakeLLM

__all__ = ["Embedder", "LLMProvider", "get_llm", "get_embedder", "reset_cache"]

_llm: LLMProvider | None = None
_embedder: Embedder | None = None


def _build_llm(provider: str) -> LLMProvider:
    if provider == "fake":
        return FakeLLM()
    if provider == "gemini":
        from .gemini import GeminiLLM

        s = get_settings()
        return GeminiLLM(s.gemini_api_key, s.gemini_model)
    if provider == "ollama":
        from .ollama import OllamaLLM

        s = get_settings()
        return OllamaLLM(s.ollama_url, s.ollama_model)
    raise ValueError(f"unknown PRODRION_LLM_PROVIDER: {provider!r}")


def _build_embedder(provider: str) -> Embedder:
    if provider == "fake":
        return FakeEmbedder()
    if provider == "gemini":
        from .gemini import GeminiEmbedder

        s = get_settings()
        return GeminiEmbedder(s.gemini_api_key, s.gemini_embed_model)
    if provider == "ollama":
        from .ollama import OllamaEmbedder

        s = get_settings()
        return OllamaEmbedder(s.ollama_url, s.ollama_embed_model)
    raise ValueError(f"unknown PRODRION_LLM_PROVIDER: {provider!r}")


def get_llm() -> LLMProvider:
    global _llm
    if _llm is None:
        _llm = _build_llm(get_settings().llm_provider)
    return _llm


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder(get_settings().llm_provider)
    return _embedder


def reset_cache() -> None:
    """Drop the cached provider instances (test helper)."""
    global _llm, _embedder
    _llm = None
    _embedder = None
