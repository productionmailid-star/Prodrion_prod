"""The tools the fix agent can call, and the text it sees back.

Each tool takes a dict of arguments and returns a string for the model. A
:class:`ToolError` (or ``WorkspaceError``) is reported back to the model as an
error message it can act on; it never aborts the run.
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
import signal
import threading

from repo import verify
from repo.workspace import Workspace, WorkspaceError

MAX_READ_LINES = 500
MAX_SEARCH_MATCHES = 60
MAX_LIST_ENTRIES = 300
MAX_LINE_CHARS = 240
MAX_COMMAND_OUTPUT_CHARS = 8000
SEARCH_TIME_LIMIT_SECONDS = 20.0


class ToolError(Exception):
    """Bad arguments or a request that cannot be carried out."""


TOOL_DOCS = """\
list_files   {"path"?: "dir/", "glob"?: "*.py"}
    List repository files, optionally under a directory and/or matching a glob.
search       {"pattern": "regex", "path"?: "dir/ or file", "glob"?: "*.ts", "ignore_case"?: false}
    Search file contents with a regular expression. Returns file:line: text.
read_file    {"path": "file", "start"?: 1, "end"?: 200}
    Read a file with line numbers (at most 500 lines per call; ask for ranges).
edit_file    {"path": "file", "old": "exact text", "new": "replacement", "replace_all"?: false}
    Replace `old` with `new`. `old` must match the file exactly (whitespace
    included, without the line-number prefix) and be unique unless replace_all.
write_file   {"path": "file", "content": "full file content"}
    Create a new file, or overwrite an existing one completely.
delete_file  {"path": "file"}
    Delete a file.
finish       {"summary": "what you changed and why it fixes the problem", "root_cause": "why the problem happened"}
    Submit the fix. It is verified first; if verification fails you get the
    failures back and must repair them.
give_up      {"reason": "..."}
    Stop without a fix: only when the complaint cannot be addressed by changing
    this repository's code, or you could not find a safe fix."""

SANDBOX_TOOL_DOCS = """\
run          {"command": "shell command", "timeout"?: 120}
    Run a shell command in the repository root inside an isolated sandbox with
    no network. Use it to reproduce the problem and to check your fix.
run_tests    {}
    Run the repository's test suite and compare against the results recorded
    before you changed anything."""


@contextlib.contextmanager
def _time_limit(seconds: float, message: str):
    """Abort the block with ToolError after ``seconds``.

    Guards against a regular expression that backtracks forever. It relies on
    an interval timer, so it is only armed on the main thread of a POSIX
    process — which is where the worker runs jobs. Elsewhere it is a no-op.
    """
    armed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not armed:
        yield
        return

    def _expired(_signum, _frame):
        raise ToolError(message)

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …"


def _require_str(args: dict, key: str, *, allow_empty: bool = False) -> str:
    value = args.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ToolError(f'"{key}" is required and must be a string')
    return value


class Toolbox:
    def __init__(self, workspace: Workspace, sandbox=None, harness=None, command_timeout=120.0):
        self.workspace = workspace
        self.sandbox = sandbox
        self.harness = harness
        self.command_timeout = command_timeout
        self._tools = {
            "list_files": self.list_files,
            "search": self.search,
            "read_file": self.read_file,
            "edit_file": self.edit_file,
            "write_file": self.write_file,
            "delete_file": self.delete_file,
        }
        if sandbox is not None:
            self._tools["run"] = self.run
            self._tools["run_tests"] = self.run_tests

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def docs(self) -> str:
        return TOOL_DOCS + ("\n" + SANDBOX_TOOL_DOCS if self.sandbox is not None else "")

    def call(self, name: str, args: dict) -> str:
        """Run one tool; errors come back as text the model can act on."""
        tool = self._tools.get(name)
        if tool is None:
            return (
                f"ERROR: unknown tool {name!r}. Available: {', '.join(self.names)}, finish, give_up"
            )
        if not isinstance(args, dict):
            return 'ERROR: "args" must be a JSON object'
        try:
            return tool(args)
        except (ToolError, WorkspaceError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # a tool bug must not kill the run
            return f"ERROR: {type(exc).__name__}: {exc}"

    # ── Exploration ──────────────────────────────────────────────────────

    def _select(self, path: str | None, glob: str | None) -> list[str]:
        files = self.workspace.files()
        if path and path.strip() not in (".", "./", ""):
            target = self.workspace.rel(self.workspace.resolve(path))
            prefix = target.rstrip("/") + "/"
            files = [f for f in files if f == target or f.startswith(prefix)]
        if glob:
            files = [
                f
                for f in files
                if fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], glob)
            ]
        return files

    def list_files(self, args: dict) -> str:
        files = self._select(args.get("path"), args.get("glob"))
        if not files:
            return "(no matching files)"
        shown = files[:MAX_LIST_ENTRIES]
        out = "\n".join(shown)
        if len(files) > len(shown):
            out += f"\n... {len(files) - len(shown)} more files; narrow with path or glob"
        return out

    def search(self, args: dict) -> str:
        pattern = _require_str(args, "pattern")
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            regex = re.compile(re.escape(pattern), flags)  # treat as literal text

        matches: list[str] = []
        total = 0
        too_slow = "the search took too long; use a simpler pattern or narrow it with path/glob"
        with _time_limit(SEARCH_TIME_LIMIT_SECONDS, too_slow):
            for rel in self._select(args.get("path"), args.get("glob")):
                try:
                    text = self.workspace.read_text(rel)
                except WorkspaceError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        total += 1
                        if len(matches) < MAX_SEARCH_MATCHES:
                            matches.append(f"{rel}:{number}: {_clip(line.strip())}")
        if not matches:
            return "(no matches)"
        out = "\n".join(matches)
        if total > len(matches):
            out += f"\n... {total - len(matches)} more matches; narrow the pattern, path or glob"
        return out

    def read_file(self, args: dict) -> str:
        rel = _require_str(args, "path")
        lines = self.workspace.read_text(rel).splitlines()
        total = len(lines)
        try:
            start = max(1, int(args.get("start") or 1))
            end = int(args.get("end") or (start + MAX_READ_LINES - 1))
        except (TypeError, ValueError):
            raise ToolError('"start" and "end" must be integers') from None
        end = min(end, start + MAX_READ_LINES - 1, total)
        if total == 0:
            return f"{rel} is empty"
        if start > total:
            raise ToolError(f"{rel} has only {total} lines")
        body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))
        header = f"{rel} (lines {start}-{end} of {total})"
        if end < total:
            header += f" — continue with start={end + 1}"
        return f"{header}\n{body}"

    # ── Editing ──────────────────────────────────────────────────────────

    def _syntax_note(self, rel: str, text: str) -> str:
        message = verify.syntax_error(rel, text)
        if message:
            return f"\nWARNING: {rel} no longer parses ({message}). Fix this before finishing."
        return ""

    def edit_file(self, args: dict) -> str:
        rel = _require_str(args, "path")
        old = _require_str(args, "old")
        new = _require_str(args, "new", allow_empty=True)
        if old == new:
            raise ToolError('"old" and "new" are identical')

        raw = self.workspace.read_text(rel)
        # Work in "\n" space so the model need not reproduce CRLF endings.
        crlf = "\r\n" in raw and "\n" not in raw.replace("\r\n", "")
        text = raw.replace("\r\n", "\n") if crlf else raw
        if crlf:
            old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")

        count = text.count(old)
        if count == 0:
            raise ToolError(self._not_found_hint(rel, text, old))
        if count > 1 and not args.get("replace_all"):
            first_line = old.split("\n", 1)[0]
            where = [str(n) for n, line in enumerate(text.split("\n"), 1) if first_line in line]
            raise ToolError(
                f'"old" matches {count} places in {rel} (around lines {", ".join(where[:10])}). '
                "Include more surrounding lines to make it unique, or set replace_all."
            )

        position = text.index(old)
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        self.workspace.write_text(rel, updated.replace("\n", "\r\n") if crlf else updated)
        first = text.count("\n", 0, position) + 1
        region = self._region(updated, first, first + new.count("\n"))
        done = count if args.get("replace_all") else 1
        where = "The first one now reads" if done > 1 else "It now reads"
        return f"Edited {rel} ({done} replacement(s)). {where}:\n{region}" + self._syntax_note(
            rel, updated
        )

    @staticmethod
    def _region(text: str, first: int, last: int, context: int = 3, limit: int = 40) -> str:
        """Numbered lines ``first..last`` of ``text`` with a little context."""
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # the file's final newline, not an extra line
        start = max(1, first - context)
        end = min(len(lines), last + context, start + limit - 1)
        return "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, end + 1))

    @staticmethod
    def _not_found_hint(rel: str, text: str, old: str) -> str:
        first = next((line.strip() for line in old.split("\n") if line.strip()), "")
        near = [n for n, line in enumerate(text.split("\n"), 1) if first and first in line]
        if near:
            return (
                f'"old" was not found in {rel}. Its first line appears at line(s) '
                f'{", ".join(map(str, near[:5]))}, so whitespace or nearby lines differ: '
                "read_file that range and copy the text exactly."
            )
        return f'"old" was not found in {rel}. read_file it again and copy the exact text.'

    def write_file(self, args: dict) -> str:
        rel = _require_str(args, "path")
        content = _require_str(args, "content", allow_empty=True)
        existed = self.workspace.exists(rel)
        key = self.workspace.write_text(rel, content)
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {key} ({len(content.splitlines())} lines)." + self._syntax_note(
            key, content
        )

    def delete_file(self, args: dict) -> str:
        return f"Deleted {self.workspace.delete(_require_str(args, 'path'))}."

    # ── Execution (sandbox only) ─────────────────────────────────────────

    def run(self, args: dict) -> str:
        command = _require_str(args, "command")
        try:
            timeout = float(args.get("timeout") or self.command_timeout)
        except (TypeError, ValueError):
            raise ToolError('"timeout" must be a number of seconds') from None
        timeout = max(1.0, min(timeout, self.command_timeout))
        result = self.sandbox.run(command, timeout=timeout, network=False)
        output = result.output
        if len(output) > MAX_COMMAND_OUTPUT_CHARS:
            output = "[... earlier output dropped ...]\n" + output[-MAX_COMMAND_OUTPUT_CHARS:]
        status = (
            f"timed out after {timeout:.0f}s"
            if result.timed_out
            else f"exit code {result.exit_code}"
        )
        return f"{status}\n{output}".rstrip()

    def run_tests(self, args: dict) -> str:
        if self.harness is None or not self.harness.available:
            reason = self.harness.reason if self.harness is not None else "no test harness"
            return f"Tests are unavailable: {reason}. Use `run` for your own checks."
        outcome = self.harness.check()
        lines = list(outcome["lines"])
        if outcome["regressions"]:
            lines.append("REGRESSIONS (must be fixed):")
            lines.extend(f"  - {item}" for item in outcome["regressions"])
        else:
            lines.append("No regressions against the baseline.")
        return "\n".join(lines) + "\n\n" + outcome["output"]
