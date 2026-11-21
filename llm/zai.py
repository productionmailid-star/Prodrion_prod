"""Z.ai (GLM) provider — paid, pay-per-token chat completions API.

Generation only: Z.ai is not used for embeddings, so pair it with
``PRODRION_EMBED_PROVIDER=gemini`` (or ``ollama``).

GLM-5.3 always reasons before answering; ``reasoning_effort`` (low | high |
max) sets how much, and the reasoning tokens are billed as output. The API
reports token usage on every reply; :attr:`ZaiLLM.usage` accumulates it so
callers can see what a job cost.
"""

from __future__ import annotations

import requests

from .base import LLMProvider

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

# Business error codes (docs.z.ai/api-reference/api-code) after which retrying
# in a minute cannot help: the account is out of balance, or a plan / spend
# window is used up. The fix agent stops at once instead of burning retries,
# and the worker postpones the job.
_OUT_OF_QUOTA = {1113, 1308, 1309, 1310, 1311, 1313, 1314, 1316, 1317, 1318, 1319, 1320, 1321}
_OUT_OF_QUOTA_WAIT_SECONDS = 3600.0

# Retryable: rate limit (1302), overloaded (1305), server-side errors.
_TRANSIENT = {1302, 1305, 1200, 1230, 1234}


class ZaiError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"Z.ai HTTP {status} (code {code}): {message}")
        self.status = status
        self.code = code


class ZaiLLM(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        reasoning_effort: str = "low",
        max_tokens: int = 32768,
        timeout: float = 600.0,
        session: requests.Session | None = None,
    ):
        if not api_key:
            raise ValueError("PRODRION_ZAI_API_KEY is required for the zai provider")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._http = session or requests.Session()
        #: Running token totals across every call on this instance.
        self.usage = {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}

    # ── LLMProvider ──────────────────────────────────────────────────────

    def generate(self, prompt: str, *, system: str | None = None, temperature: float = 0.1) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._complete(system, messages, temperature=temperature, json_mode=False)

    def converse(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        return self._complete(system, messages, temperature=temperature, json_mode=json_mode)

    def retry_after_seconds(self, exc: Exception) -> float | None:
        if isinstance(exc, ZaiError) and exc.code in _OUT_OF_QUOTA:
            return _OUT_OF_QUOTA_WAIT_SECONDS
        return None

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _complete(
        self,
        system: str | None,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_mode: bool,
    ) -> str:
        turns = [{"role": "system", "content": system}] if system else []
        turns += [
            {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
            for m in messages
        ]
        body: dict = {
            "model": self._model,
            "messages": turns,
            "temperature": min(max(temperature, 0.0), 1.0),
            "max_tokens": self._max_tokens,
            "stream": False,
            "thinking": {"type": "enabled"},
            "reasoning_effort": self._reasoning_effort,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        resp = self._http.post(self._url, json=body, headers=self._headers, timeout=self._timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or "error" in data:
            error = data.get("error") or {}
            try:
                code = int(error.get("code")) if error.get("code") is not None else None
            except (TypeError, ValueError):
                code = None
            message = error.get("message") or resp.text[:300] or resp.reason
            raise ZaiError(resp.status_code, code, message)

        self._count(data.get("usage") or {})
        choices = data.get("choices") or []
        if not choices:
            raise ZaiError(resp.status_code, None, "reply had no choices")
        choice = choices[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        reason = choice.get("finish_reason")
        if reason == "sensitive":
            raise ZaiError(resp.status_code, 1301, "reply withheld by the content filter")
        if not content and reason == "length":
            raise ZaiError(
                resp.status_code,
                None,
                f"reply cut off at max_tokens={self._max_tokens} before any answer",
            )
        return content

    def _count(self, usage: dict) -> None:
        self.usage["requests"] += 1
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        self.usage["cached_tokens"] += int(details.get("cached_tokens") or 0)
