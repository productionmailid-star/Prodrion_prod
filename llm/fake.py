"""Deterministic, offline provider used by the test suite."""

from __future__ import annotations

import hashlib

from .base import Embedder, LLMProvider


class FakeLLM(LLMProvider):
    """Records every call and returns a canned response."""

    def __init__(self, canned: str = ""):
        self.canned = canned
        self.calls: list[dict] = []

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.1) -> str:
        self.calls.append({"prompt": prompt, "system": system, "temperature": temperature})
        return self.canned


class FakeEmbedder(Embedder):
    """Hash-derived pseudo-vectors: deterministic, cheap, no network."""

    def __init__(self, dim: int = 16):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([(digest[i % len(digest)] / 255.0) * 2.0 - 1.0 for i in range(self.dim)])
        return vectors
