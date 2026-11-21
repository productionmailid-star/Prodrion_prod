"""A compact map of the repository: every file, with the symbols it defines.

The agent gets this up front so it starts with a picture of the whole codebase
and can jump straight to the relevant area with ``search`` / ``read_file``,
instead of depending on an embedding index that must be built per job.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import PurePosixPath

DEFAULT_BUDGET_CHARS = 24_000
_MAX_SYMBOLS_PER_FILE = 40

# Declarations in brace/`end` languages, matched line by line. Group "name".
_DECLARATION = re.compile(
    r"""^\s{0,8}
    (?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|internal\s+)?
    (?:static\s+)?(?:abstract\s+)?(?:final\s+)?(?:async\s+)?(?:pub(?:\([^)]*\))?\s+)?
    (?:
        (?P<kind>function\*?|class|interface|enum|struct|trait|impl|module|def|fn|type)
        \s+(?P<name>[A-Za-z_$][\w$.:]*)
      |
        func\s+(?:\([^)]*\)\s*)?(?P<goname>[A-Za-z_]\w*)
      |
        (?:const|let|var)\s+(?P<arrow>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?
        (?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)
    )
    """,
    re.VERBOSE,
)

_SOURCE_SUFFIXES = {
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".h",
    ".cpp", ".cc", ".hpp", ".rb", ".php", ".cs", ".kt", ".swift", ".scala",
}  # fmt: skip


def _python_symbols(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            symbols.append(f"class {node.name}" + (f": {', '.join(methods)}" if methods else ""))
    return symbols


def _generic_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for line in text.splitlines():
        match = _DECLARATION.match(line)
        if not match:
            continue
        if match.group("name"):
            symbols.append(f"{match.group('kind')} {match.group('name')}")
        elif match.group("goname"):
            symbols.append(f"func {match.group('goname')}")
        else:
            symbols.append(f"function {match.group('arrow')}")
    return list(dict.fromkeys(symbols))


def file_symbols(path: str, text: str) -> list[str]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in (".py", ".pyi"):
        symbols = _python_symbols(text)
    elif suffix in _SOURCE_SUFFIXES:
        symbols = _generic_symbols(text)
    else:
        return []
    if len(symbols) > _MAX_SYMBOLS_PER_FILE:
        extra = len(symbols) - _MAX_SYMBOLS_PER_FILE
        symbols = symbols[:_MAX_SYMBOLS_PER_FILE] + [f"... {extra} more"]
    return symbols


def build_repo_map(workspace, budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """Render the map, degrading in detail until it fits ``budget_chars``."""
    files = workspace.files()
    if not files:
        return "(the repository contains no readable text files)"

    # Tier 1: every file with its line count and symbols.
    detailed: list[str] = []
    for rel in files:
        try:
            text = workspace.read_text(rel)
        except Exception:
            detailed.append(rel)
            continue
        detailed.append(f"{rel} ({len(text.splitlines())} lines)")
        symbols = file_symbols(rel, text)
        if symbols:
            detailed.append("    " + "; ".join(symbols))
    rendered = "\n".join(detailed)
    if len(rendered) <= budget_chars:
        return rendered

    # Tier 2: paths only.
    rendered = "\n".join(files)
    note = "(symbols omitted to fit: use search / read_file to look inside files)\n"
    if len(rendered) + len(note) <= budget_chars:
        return note + rendered

    # Tier 3: directories with file counts.
    counts = Counter(str(PurePosixPath(rel).parent) for rel in files)
    lines = [f"{directory}/  ({count} files)" for directory, count in sorted(counts.items())]
    rendered = "\n".join(lines)
    note = (
        f"(large repository: {len(files)} files, showing directories only. "
        "Use list_files and search to explore.)\n"
    )
    return note + rendered[: max(0, budget_chars - len(note))]
