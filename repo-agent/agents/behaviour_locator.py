import re
from pathlib import Path

from agents.llm import LocalLLM


class BehaviorLocator:
    """
    For each discovered file, asks one tightly-scoped question:
    "What line number in THIS file implements the behavior?"

    Returns a list of verified hits:
        [{ "file": str, "line": int, "content": str }, ...]

    Every returned line has been verified to exist in the real file.
    The LLM only needs to output a single integer — the smallest possible
    task, which CodeLlama handles reliably.
    """

    # Window of lines shown to the LLM around a candidate section
    _CONTEXT_WINDOW = 40

    def __init__(self, repo_path: str):
        self.llm       = LocalLLM()
        self.repo_root = Path(repo_path)

    # ── File I/O ─────────────────────────────────────────────────────────

    def _read_lines(self, file: str) -> list[str]:
        """Return 0-indexed list of line strings (no newline chars)."""
        try:
            with open(self.repo_root / file, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().splitlines()
        except Exception:
            return []

    @staticmethod
    def _numbered(lines: list[str], start: int = 1) -> str:
        return "\n".join(f"{start + i:>4}: {l}" for i, l in enumerate(lines))

    # ── LLM: ask for a single line number ────────────────────────────────

    def _ask_line_number(self, goal: str, file: str, numbered_code: str) -> int | None:
        """
        Ask the LLM for the single most relevant line number.
        Uses prefill 'LINE: ' so the model is forced to start with the number.
        Returns an int, or None if the answer can't be parsed.
        """
        system = """\
You are a code analyst. Answer with ONE integer and nothing else.

Read the numbered source code below.
Find the single line number that DIRECTLY implements the behavior
described in the goal (e.g. the delete call, the status update, the DB write).

Respond with ONLY the line number. No words. No punctuation. Just the number.
"""
        user = f"""\
GOAL: {goal}

FILE: {file}
{numbered_code}

Which single line number implements this behavior? Reply with the number only:"""

        # Prefill 'LINE: ' to lock the model into outputting a number immediately
        raw = self.llm.chat(system, user, prefill="LINE: ")

        # Parse: grab the first integer we find in the response
        m = re.search(r"\d+", raw)
        if m:
            return int(m.group())
        return None

    # ── Public API ───────────────────────────────────────────────────────

    def locate(self, goal: str, files: list) -> list[dict]:
        """
        Returns verified hits:
            [{ "file": str, "line": int, "content": str }, ...]

        "line" is 1-based.  "content" is the verbatim text of that line.
        """
        hits: list[dict] = []

        for file in files:
            lines = self._read_lines(file)
            if not lines:
                continue

            total = len(lines)
            numbered = self._numbered(lines)

            lineno = self._ask_line_number(goal, file, numbered)

            # Validate
            if lineno is None or lineno < 1 or lineno > total:
                print(f"  ⚠  Locator returned invalid line for {file} "
                      f"(got {lineno}, file has {total} lines) — skipping")
                continue

            actual_content = lines[lineno - 1]   # 0-indexed

            hits.append({
                "file":    file,
                "line":    lineno,
                "content": actual_content,
            })

            print(f"  Located: {file}:{lineno}  →  {actual_content.strip()[:60]}")

        return hits
