"""LocalLLM — the repo-agent's text-generation entry point.

Historically this wrapped Ollama/CodeLlama directly. It now delegates to the
shared provider abstraction (``llm.get_llm``) so the whole product uses one
configured model. The class name and ``chat(system, user, prefill)`` signature
are kept so the agent modules need no changes.
"""

from __future__ import annotations

from llm import get_llm


class LocalLLM:
    def __init__(self, model: str | None = None, temperature: float = 0.1):
        # `model` is retained for call-site compatibility; the actual model is
        # chosen by the configured provider.
        self.model = model
        self.temperature = temperature

    def chat(self, system: str, user: str, prefill: str = "") -> str:
        return get_llm().chat(system, user, prefill, temperature=self.temperature)
