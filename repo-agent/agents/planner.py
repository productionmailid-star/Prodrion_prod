import re
from pathlib import Path

from agents.llm import LocalLLM


# ═══════════════════════════════════════════════════════════════════════
# SCOPE ANALYSER — pure regex, zero LLM, zero hallucination
# ═══════════════════════════════════════════════════════════════════════

class ScopeAnalyser:
    """
    Extracts every concrete fact needed to write correct replacement code
    directly from the source text using regex.  No LLM is involved.

    Facts extracted
    ───────────────
    local_vars   : every name assigned in the function  (name = ...)
    func_params  : parameter names from the def() line
    commit_calls : exact .commit() / .save() / .flush() expressions found
    target_obj   : the object variable acted on in the target line
    db_var       : the db/session/conn handle name
    orm_style    : 'sqlalchemy' | 'django' | 'peewee' | 'mongoengine' | 'raw' | 'unknown'
    all_calls    : every method call expression in the function (for context)
    """

    # ── ORM / DB commit patterns (ordered most-specific first) ──────────
    _COMMIT_RE = re.compile(
        r"""
        \b(
            \w+\.session\.commit\s*\(\)     |   # db.session.commit()
            \w+\.session\.flush\s*\(\)      |   # db.session.flush()
            session\.commit\s*\(\)          |   # session.commit()
            session\.flush\s*\(\)           |   # session.flush()
            \w+\.commit\s*\(\)              |   # conn.commit()  /  db.commit()
            \w+\.save\s*\(                  |   # obj.save( ...
            \w+\.save\s*\(\)                    # obj.save()
        )
        """,
        re.VERBOSE,
    )

    # ── Delete / remove operation on the target line ─────────────────────
    _DELETE_ARG_RE   = re.compile(r"\.delete\(\s*(\w+)\s*\)")   # .delete(user)
    _DELETE_SELF_RE  = re.compile(r"(\w+)\.delete\s*\(\s*\)")   # user.delete()
    _REMOVE_ARG_RE   = re.compile(r"\.remove\(\s*(\w+)\s*\)")   # .remove(item)
    _DESTROY_SELF_RE = re.compile(r"(\w+)\.destroy\s*\(\s*\)")  # user.destroy()

    # ── Variable assignment ───────────────────────────────────────────────
    _ASSIGN_RE = re.compile(r"^\s*(\w+)\s*=(?!=)")              # name = ...

    # ── Function def + params ────────────────────────────────────────────
    _DEF_RE    = re.compile(r"def\s+\w+\s*\(([^)]*)\)")

    # ── DB / session handle ───────────────────────────────────────────────
    _DB_RE = re.compile(
        r"\b(db|session|conn|connection|g\.db|current_app\.db)\b"
    )

    # ── ORM detection: check imports AND function body ────────────────────
    # Tuple of (import_keywords, body_keywords) — either match wins.
    # Body keywords cover frameworks that import db from an app extensions
    # module rather than directly from sqlalchemy/django.
    _ORM_HINTS = {
        "sqlalchemy": (
            ["sqlalchemy", "SQLAlchemy", "scoped_session"],
            ["db.session", "session.commit", "session.delete",
             "session.add", ".query(", "session.query"],
        ),
        "django": (
            ["django.db", "models.Model", "django.contrib"],
            ["objects.filter", "objects.get", "objects.create", ".save("],
        ),
        "peewee": (
            ["peewee"],
            [".delete_instance(", "Model.delete"],
        ),
        "mongoengine": (
            ["mongoengine", "Document"],
            [".delete()", ".save()"],
        ),
    }

    # ────────────────────────────────────────────────────────────────────

    def analyse(self, func_lines: list[str], target_line_0: int,
                import_lines: list[str]) -> dict:
        """
        func_lines   : raw lines of the enclosing function (0-indexed in the list)
        target_line_0: 0-indexed position of the target line WITHIN func_lines
        import_lines : all import lines from the file
        """
        target_content = func_lines[target_line_0] if target_line_0 < len(func_lines) else ""

        # ── Params from def line ──────────────────────────────────────────
        func_params: list[str] = []
        if func_lines:
            m = self._DEF_RE.search(func_lines[0])
            if m:
                raw_params = m.group(1)
                for p in raw_params.split(","):
                    p = p.strip().split(":")[0].split("=")[0].strip()
                    if p and p not in ("self", "cls", "*", "**"):
                        func_params.append(p)

        # ── Local variable assignments ────────────────────────────────────
        local_vars: list[str] = []
        seen: set[str] = set()
        for line in func_lines:
            m = self._ASSIGN_RE.match(line)
            if m:
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    local_vars.append(name)

        # ── Commit / save calls ───────────────────────────────────────────
        commit_calls: list[str] = []
        seen_commits: set[str] = set()
        for line in func_lines:
            for m in self._COMMIT_RE.finditer(line):
                call = m.group(1).strip()
                if call not in seen_commits:
                    seen_commits.add(call)
                    commit_calls.append(call)

        # ── Object being acted on in target line ─────────────────────────
        target_obj: str | None = None

        # db.session.delete(user)  → user
        m = self._DELETE_ARG_RE.search(target_content)
        if m:
            target_obj = m.group(1)

        # user.delete()  → user
        if not target_obj:
            m = self._DELETE_SELF_RE.search(target_content)
            if m:
                target_obj = m.group(1)

        # collection.remove(item)  → item
        if not target_obj:
            m = self._REMOVE_ARG_RE.search(target_content)
            if m:
                target_obj = m.group(1)

        # record.destroy()  → record
        if not target_obj:
            m = self._DESTROY_SELF_RE.search(target_content)
            if m:
                target_obj = m.group(1)

        # ── DB / session handle ───────────────────────────────────────────
        db_var: str | None = None
        for line in func_lines:
            m = self._DB_RE.search(line)
            if m:
                db_var = m.group(1)
                break

        # ── ORM style: check imports AND function body ────────────────────
        import_text = "\n".join(import_lines)
        body_text   = "\n".join(func_lines)
        orm_style   = "unknown"
        for style, (import_hints, body_hints) in self._ORM_HINTS.items():
            if (any(h in import_text for h in import_hints) or
                    any(h in body_text for h in body_hints)):
                orm_style = style
                break

        # ── All method calls in the function (for context) ────────────────
        all_calls = re.findall(r"\b\w+\.\w+\s*\(", "\n".join(func_lines))

        return {
            "local_vars":   local_vars,
            "func_params":  func_params,
            "commit_calls": commit_calls,
            "target_obj":   target_obj,
            "db_var":       db_var,
            "orm_style":    orm_style,
            "all_calls":    list(dict.fromkeys(all_calls)),  # deduplicated
        }

    def build_prompt_facts(self, scope: dict) -> str:
        """
        Build a tight, factual constraint block to inject into the LLM prompt.
        Every piece of information comes from the real file — nothing invented.
        """
        lines: list[str] = []

        if scope["target_obj"]:
            lines.append(f"TARGET OBJECT   : {scope['target_obj']}")

        all_vars = scope["func_params"] + scope["local_vars"]
        if all_vars:
            lines.append(f"VARIABLES IN SCOPE: {', '.join(dict.fromkeys(all_vars))}")

        if scope["commit_calls"]:
            lines.append(f"COMMIT/SAVE CALLS : {', '.join(scope['commit_calls'])}")
        else:
            lines.append("COMMIT/SAVE CALLS : none found — infer from ORM style")

        if scope["db_var"]:
            lines.append(f"DB/SESSION HANDLE : {scope['db_var']}")

        lines.append(f"ORM STYLE         : {scope['orm_style']}")

        if scope["all_calls"]:
            sample = scope["all_calls"][:8]
            lines.append(f"METHOD CALLS SEEN : {', '.join(sample)}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# CHANGE PLANNER
# ═══════════════════════════════════════════════════════════════════════

class ChangePlanner:
    """
    Per located hit:

    Step 1  (no LLM) — extract enclosing function + imports
    Step 2  (no LLM) — ScopeAnalyser reads all variable names, commit
                        calls, ORM style, db handle directly from the file
    Step 3  (LLM)    — generate replacement code
                        The LLM is given ONLY:
                          • the goal
                          • the full enclosing function (~20-60 lines)
                          • a block of VERIFIED facts from Step 2
                          • the exact target line content
                        It cannot invent variable names because every
                        relevant name is provided as a hard constraint.
    """

    def __init__(self, repo_path: str):
        self.llm      = LocalLLM()
        self.analyser = ScopeAnalyser()
        self.repo_path = Path(repo_path)

    # ── File I/O ─────────────────────────────────────────────────────────

    def _read_lines(self, file_path: str) -> list[str]:
        try:
            with open(self.repo_path / file_path, "r", encoding="utf-8") as f:
                return f.read().splitlines()
        except Exception:
            return []

    # ── Enclosing function extractor ──────────────────────────────────────

    @staticmethod
    def _extract_enclosing_function(lines: list[str],
                                    target_line: int) -> tuple[list[str], int, int]:
        """
        Returns (func_lines, func_start_1based, target_offset_0based).
        func_lines is the raw list of lines for the enclosing function.
        target_offset_0based is the position of the target inside func_lines.
        """
        target_0     = target_line - 1
        func_start_0 = target_0

        for i in range(target_0, -1, -1):
            s = lines[i].lstrip()
            if s.startswith("def ") or s.startswith("async def "):
                func_start_0 = i
                break

        def_indent = len(lines[func_start_0]) - len(lines[func_start_0].lstrip())
        func_end_0 = len(lines)

        for i in range(func_start_0 + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= def_indent:
                func_end_0 = i
                break

        func_lines     = lines[func_start_0:func_end_0]
        target_offset  = target_0 - func_start_0
        return func_lines, func_start_0 + 1, target_offset

    @staticmethod
    def _numbered_func(func_lines: list[str], func_start: int,
                       target_line: int) -> str:
        return "\n".join(
            f"{'>>>' if (func_start + i) == target_line else '   '} "
            f"{func_start + i:>4}: {l}"
            for i, l in enumerate(func_lines)
        )

    @staticmethod
    def _extract_imports(lines: list[str]) -> list[str]:
        out = []
        for line in lines:
            s = line.strip()
            if s.startswith(("import ", "from ")):
                out.append(line)
            elif out and not s:
                continue
            elif out:
                break
        return out

    @staticmethod
    def _extract_indent(line: str) -> str:
        return line[: len(line) - len(line.lstrip())]

    # ── LLM: replacement generation only ─────────────────────────────────

    def _generate_replacement(self, goal: str, file: str,
                               func_snippet: str,
                               target_line: int, target_content: str,
                               facts: str) -> str:
        """
        One tightly-constrained LLM call.
        facts = verified string from ScopeAnalyser.build_prompt_facts()
        """
        indent_str = self._extract_indent(target_content)

        system = """\
You are a code editor making one precise change.

You will be given:
  1. A developer goal
  2. The function containing the line to change
  3. VERIFIED FACTS extracted from the actual source file
  4. The exact line to replace

STRICT RULES:
- Use ONLY the variable names listed in VARIABLES IN SCOPE.
- Use ONLY the commit/save expression listed in COMMIT/SAVE CALLS (if any).
- Do NOT invent method names, variable names, or imports.
- Output ONLY the replacement lines — no explanation, no markdown, no diff.
- Preserve the exact indentation of the original line.
- Do NOT repeat the original line.
"""

        user = f"""\
GOAL: {goal}

FILE: {file}
FUNCTION (>>> = line to replace):
{func_snippet}

VERIFIED FACTS FROM SOURCE FILE:
{facts}

TARGET LINE {target_line}: {target_content.strip()!r}

Write ONLY the replacement lines (same indentation):"""

        raw    = self.llm.chat(system, user, prefill=indent_str)
        result = raw.strip("\n")

        # Strip accidental code fences
        result = re.sub(r"^```[a-z]*\n?", "", result, flags=re.IGNORECASE)
        result = re.sub(r"\n?```$",        "", result)
        result = result.strip("\n")

        # Enforce indentation on every line
        fixed: list[str] = []
        for line in result.splitlines():
            if line.strip() and not line.startswith(indent_str):
                fixed.append(indent_str + line.lstrip())
            else:
                fixed.append(line)
        return "\n".join(fixed)

    # ── Patch assembly ────────────────────────────────────────────────────

    @staticmethod
    def _make_patch(file: str, target_line: int,
                    original: str, replacement: str, scope: dict) -> str:
        plus_lines = "\n".join(f"+ {l}" for l in replacement.splitlines())
        obj    = scope.get("target_obj") or "unknown"
        commit = ", ".join(scope.get("commit_calls") or ["none found"])
        reason = (
            f"Line {target_line} performs the operation the goal targets.\n"
            f"Object: `{obj}` — Commit method: `{commit}`"
        )
        return (
            f"FILE: {file}\n"
            f"REASON:\n{reason}\n"
            f"PATCH:\n"
            f"```diff\n"
            f"@@ line {target_line}\n"
            f"- {original}\n"
            f"{plus_lines}\n"
            f"```\n"
            f"----------------------------------------"
        )

    # ── Public API ────────────────────────────────────────────────────────

    def plan(self, goal: str, hits: list[dict]) -> list[dict]:
        """
        hits    : [{ "file": str, "line": int, "content": str }, ...]
        returns : [{ "file": str, "patch": str, "warnings": list[str] }, ...]
        """
        results: list[dict] = []

        for hit in hits:
            file     = hit["file"]
            lineno   = hit["line"]      # 1-based, verified
            original = hit["content"]  # verbatim real line

            lines = self._read_lines(file)
            if not lines:
                results.append({
                    "file": file, "patch": f"(could not read {file})",
                    "warnings": []
                })
                continue

            # ── Step 1: extract function + imports ────────────
            func_lines, func_start, target_offset = \
                self._extract_enclosing_function(lines, lineno)
            import_lines = self._extract_imports(lines)

            # ── Step 2: deterministic scope analysis ──────────
            scope = self.analyser.analyse(func_lines, target_offset, import_lines)
            facts = self.analyser.build_prompt_facts(scope)

            print(f"  Scope [{file}:{lineno}]")
            print(f"    obj={scope['target_obj']}  "
                  f"commit={scope['commit_calls']}  "
                  f"orm={scope['orm_style']}")
            print(f"    vars={scope['local_vars'] + scope['func_params']}")

            # ── Step 3: LLM generates logic only ─────────────
            func_snippet = self._numbered_func(func_lines, func_start, lineno)
            replacement  = self._generate_replacement(
                goal, file, func_snippet, lineno, original, facts
            )

            patch = self._make_patch(file, lineno, original, replacement, scope)

            results.append({
                "file":     file,
                "patch":    patch,
                "warnings": [],
            })

        return results
