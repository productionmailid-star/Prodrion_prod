"""Provider-agnostic LLM interfaces.

Two capabilities the rest of the codebase needs:

* :class:`LLMProvider` — text generation (taxonomy bootstrap, classification,
  drift labelling, text-to-SQL, and the repo-agent's reasoning steps).
* :class:`Embedder` — vector embeddings (feedback + Jira ticket indexing).

Concrete providers live alongside this module (``gemini``, ``ollama``,
``fake``) and are selected by ``PRODRION_LLM_PROVIDER``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Text generation."""

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.1) -> str:
        """Return the model's completion for ``prompt`` (already stripped)."""

    def chat(self, system: str, user: str, prefill: str = "", *, temperature: float = 0.1) -> str:
        """Single-turn helper used by the repo-agent.

        ``prefill`` seeds the start of the answer to lock an output format.
        The default implementation folds it into the prompt and re-attaches it
        to the result; providers with native prefill support may override.
        """
        prompt = user if not prefill else f"{user}\n\n{prefill}"
        out = self.generate(prompt, system=system, temperature=temperature)
        if prefill and not out.startswith(prefill):
            return prefill + out
        return out


class Embedder(ABC):
    """Vector embeddings."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, order preserved."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
