"""Static checks that a changed file still parses.

This is the floor of the "do not break the codebase" guarantee: it needs no
execution of the target repo, so it runs on every job. Running the repo's own
tests (see ``repo.sandbox``) is the stronger, opt-in layer on top.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

# extension -> tree-sitter grammar name
_TREE_SITTER_GRAMMARS = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
}

_parsers: dict[str, object] = {}


def _tree_sitter_parser(grammar: str):
    """Return a cached parser, or None when tree-sitter is not installed."""
    if grammar in _parsers:
        return _parsers[grammar]
    parser = None
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language

        parser = Parser()
        parser.language = get_language(grammar)
    except Exception:  # missing package or grammar: degrade to "not checked"
        parser = None
    _parsers[grammar] = parser
    return parser


def _first_error(node) -> tuple[int, str] | None:
    """Locate the first ERROR / MISSING node: (1-based line, description)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.is_missing:
            return current.start_point[0] + 1, f"missing {current.type!r}"
        if current.type == "ERROR":
            return current.start_point[0] + 1, "unexpected or incomplete syntax"
        if current.has_error:
            stack.extend(reversed(current.children))
    return None


def syntax_error(path: str, text: str) -> str | None:
    """Return a one-line description of a syntax error in ``text``, or None.

    None also covers "no checker for this file type" — use
    :func:`can_check` to tell the two apart.
    """
    suffix = PurePosixPath(path).suffix.lower()

    if suffix in (".py", ".pyi"):
        try:
            compile(text, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            return f"line {exc.lineno}: {exc.msg}"
        except ValueError as exc:  # e.g. source contains null bytes
            return str(exc)
        return None

    if suffix == ".json":
        try:
            json.loads(text)
        except ValueError as exc:
            return str(exc)
        return None

    if suffix == ".toml":
        try:
            import tomllib
        except ImportError:  # Python 3.10
            return None
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return str(exc)
        return None

    grammar = _TREE_SITTER_GRAMMARS.get(suffix)
    if grammar:
        parser = _tree_sitter_parser(grammar)
        if parser is None:
            return None
        tree = parser.parse(text.encode("utf-8"))
        if tree.root_node.has_error:
            found = _first_error(tree.root_node)
            if found:
                return f"line {found[0]}: {found[1]}"
            return "syntax error"
    return None


def can_check(path: str) -> bool:
    """True if :func:`syntax_error` actually validates this file type here."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in (".py", ".pyi", ".json"):
        return True
    if suffix == ".toml":
        try:
            import tomllib  # noqa: F401
        except ImportError:
            return False
        return True
    grammar = _TREE_SITTER_GRAMMARS.get(suffix)
    return bool(grammar) and _tree_sitter_parser(grammar) is not None


def check_changes(workspace) -> dict:
    """Syntax-check every changed file in a :class:`repo.workspace.Workspace`.

    A file that already failed to parse before the agent touched it is not
    held against the change. Returns::

        {"ok": bool, "checked": [paths], "unchecked": [paths], "errors": {path: message}}
    """
    checked: list[str] = []
    unchecked: list[str] = []
    errors: dict[str, str] = {}

    for rel in workspace.changed_files():
        if not workspace.exists(rel):
            continue  # deleted
        if not can_check(rel):
            unchecked.append(rel)
            continue
        checked.append(rel)
        message = syntax_error(rel, workspace.read_text(rel))
        if message is None:
            continue
        before = workspace.original(rel)
        if before is not None and syntax_error(rel, before) is not None:
            continue  # was already broken; not this change's fault
        errors[rel] = message

    return {"ok": not errors, "checked": checked, "unchecked": unchecked, "errors": errors}
