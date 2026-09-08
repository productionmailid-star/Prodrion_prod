"""Google Gemini provider (free-tier API).

The ``google-genai`` SDK is imported lazily so the package as a whole stays
importable without it (tests use the fake provider).
"""

from __future__ import annotations

from .base import Embedder, LLMProvider


class GeminiLLM(LLMProvider):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("PRODRION_GEMINI_API_KEY is required for the gemini provider")
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.1) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(temperature=temperature)
        if system:
            config.system_instruction = system
        resp = self._client.models.generate_content(
            model=self._model, contents=prompt, config=config
        )
        return (resp.text or "").strip()


class GeminiEmbedder(Embedder):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("PRODRION_GEMINI_API_KEY is required for the gemini provider")
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self.model_name = f"gemini:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.models.embed_content(model=self._model, contents=texts)
        return [list(item.values) for item in resp.embeddings]
