"""Local Ollama provider — kept for offline development."""

from __future__ import annotations

import requests

from .base import Embedder, LLMProvider


class OllamaLLM(LLMProvider):
    def __init__(self, generate_url: str, model: str):
        self._url = generate_url
        self._model = model

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.1) -> str:
        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        resp = requests.post(self._url, json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()


class OllamaEmbedder(Embedder):
    def __init__(self, generate_url: str, model: str):
        # Derive the API base from the configured .../api/generate URL.
        self._base = generate_url.rsplit("/api/", 1)[0] or "http://localhost:11434"
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            resp = requests.post(
                f"{self._base}/api/embeddings",
                json={"model": self._model, "prompt": text},
                timeout=120,
            )
            resp.raise_for_status()
            vectors.append(resp.json()["embedding"])
        return vectors
