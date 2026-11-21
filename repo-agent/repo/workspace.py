"""A confined, change-tracking view of the repository being fixed.

Every path the agent names is resolved against the workspace root and refused
if it would land outside it (``..``, absolute paths, or a symlink planted in
the cloned repo that points at the host). The first write to any file records
its original content, so :meth:`Workspace.diff` can always produce a real
unified diff that ``git apply`` accepts.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from repo.filters import IGNORE_DIRS

# Files larger than this are never read, searched or edited by the agent.
MAX_FILE_BYTES = 1_000_000
_BINARY_SNIFF_BYTES = 8192


class WorkspaceError(Exception):
    """A tool request that cannot be honoured (bad path, binary file, ...)."""


class Workspace:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"{root} is not a directory")
        # rel path -> original text, or None if the file did not exist.
        self._originals: dict[str, str | None] = {}

    # ── Paths ────────────────────────────────────────────────────────────

    def resolve(self, rel: str) -> Path:
        """Map a repo-relative path to a real path inside the workspace."""
        if not isinstance(rel, str) or not rel.strip():
            raise WorkspaceError("path must be a non-empty string")
        rel = rel.strip().replace("\\", "/")
        if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
            raise WorkspaceError(f"{rel}: use a path relative to the repository root")
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError(f"{rel}: path is outside the repository")
        parts = candidate.relative_to(self.root).parts
        if any(part in IGNORE_DIRS for part in parts):
            raise WorkspaceError(f"{rel}: path is inside an ignored directory")
        return candidate

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ── Listing ──────────────────────────────────────────────────────────

    def files(self) -> list[str]:
        """Every readable text file, as sorted repo-relative posix paths."""
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in IGNORE_DIRS and not os.path.islink(os.path.join(dirpath, d))
            )
            for name in filenames:
                full = Path(dirpath) / name
                if full.is_symlink() or not self._is_text(full):
                    continue
                found.append(full.relative_to(self.root).as_posix())
        return sorted(found)

    @staticmethod
    def _is_text(path: Path) -> bool:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return False
            with open(path, "rb") as handle:
                return b"\0" not in handle.read(_BINARY_SNIFF_BYTES)
        except OSError:
            return False

    # ── Reading ──────────────────────────────────────────────────────────

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).is_file()
        except WorkspaceError:
            return False

    def read_text(self, rel: str) -> str:
        """Return the file's text with its original line endings intact."""
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError(f"{rel}: no such file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"{rel}: file is too large to open ({path.stat().st_size} bytes)")
        data = path.read_bytes()
        if b"\0" in data[:_BINARY_SNIFF_BYTES]:
            raise WorkspaceError(f"{rel}: binary file")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError(f"{rel}: not valid UTF-8 text") from None

    # ── Writing (all changes are tracked) ────────────────────────────────

    def _remember(self, rel: str, path: Path) -> None:
        if rel in self._originals:
            return
        self._originals[rel] = self.read_text(rel) if path.is_file() else None

    def write_text(self, rel: str, content: str) -> str:
        path = self.resolve(rel)
        if path.is_dir():
            raise WorkspaceError(f"{rel}: is a directory")
        key = self.rel(path)
        self._remember(key, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return key

    def delete(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError(f"{rel}: no such file")
        key = self.rel(path)
        self._remember(key, path)
        path.unlink()
        return key

    # ── Change report ────────────────────────────────────────────────────

    def original(self, rel: str) -> str | None:
        """Content before the agent touched ``rel`` (None if it did not exist)."""
        return self._originals.get(rel)

    def _current(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_bytes().decode("utf-8") if path.is_file() else None

    def changed_files(self) -> list[str]:
        """Files whose content now differs from the original, sorted."""
        return sorted(
            rel for rel, before in self._originals.items() if self._current(rel) != before
        )

    def diff(self) -> str:
        """All changes as one unified diff in ``git apply`` format."""
        chunks = [
            _file_diff(rel, self._originals[rel], self._current(rel))
            for rel in self.changed_files()
        ]
        return "".join(chunks)


def _lines(text: str) -> list[str]:
    """Split on "\n" only (as git does), keeping the terminator on each line.

    ``str.splitlines`` would also break on form feeds and other Unicode
    separators, producing hunks git cannot apply.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _file_diff(rel: str, before: str | None, after: str | None) -> str:
    header = [f"diff --git a/{rel} b/{rel}\n"]
    if before is None:
        header.append("new file mode 100644\n")
    elif after is None:
        header.append("deleted file mode 100644\n")

    body = difflib.unified_diff(
        _lines(before or ""),
        _lines(after or ""),
        fromfile="/dev/null" if before is None else f"a/{rel}",
        tofile="/dev/null" if after is None else f"b/{rel}",
    )
    out = list(header)
    for line in body:
        if line.endswith("\n"):
            out.append(line)
        else:  # last line of a file that has no trailing newline
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)
