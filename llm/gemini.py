"""Google Gemini provider (free-tier API).

The ``google-genai`` SDK is imported lazily so the package as a whole stays
importable without it (tests use the fake provider).
"""

from __future__ import annotations

import re

from .base import Embedder, LLMProvider

_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?([\d.]+)s")
_RETRY_IN = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


def parse_retry_after(message: str) -> float | None:
    """Seconds to wait, from a Gemini quota error's RetryInfo / "retry in 10h50m41s"."""
    match = _RETRY_DELAY.search(message)
    if match:
        return float(match.group(1))
    match = _RETRY_IN.search(message)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


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

    def retry_after_seconds(self, exc: Exception) -> float | None:
        return parse_retry_after(str(exc))

    def converse(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(temperature=temperature)
        if system:
            config.system_instruction = system
        if json_mode:
            config.response_mime_type = "application/json"
        contents = [
            types.Content(
                role="model" if message["role"] == "assistant" else "user",
                parts=[types.Part.from_text(text=message["content"])],
            )
            for message in messages
        ]
        resp = self._client.models.generate_content(
            model=self._model, contents=contents, config=config
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
