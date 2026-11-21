"""Workspace confinement, change tracking, and diff round-tripping, plus the
tools built on top of it (repo-agent/repo/workspace.py, agents/tools.py)."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from agents.tools import Toolbox
from repo.repomap import build_repo_map
from repo.workspace import Workspace, WorkspaceError


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "core.py").write_text(
        "import os\n\n\ndef load(path):\n    return open(path).read()\n\n\n"
        "class Store:\n    def get(self, key):\n        return key\n"
    )
    (root / "app" / "view.js").write_text(
        "export function render(items) {\n  return items.length;\n}\n"
    )
    (root / "README.md").write_text("# demo\n")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("module.exports = 1;\n")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    return root


# ── Confinement ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["../outside.txt", "/etc/passwd", "app/../../outside.txt", "C:\\Windows\\x", ".git/config",
     "node_modules/dep/index.js", ""],
)  # fmt: skip
def test_paths_outside_the_repo_are_refused(repo, bad):
    ws = Workspace(repo)
    with pytest.raises(WorkspaceError):
        ws.resolve(bad)


def test_symlink_pointing_outside_the_repo_cannot_be_read_or_written(repo, tmp_path):
    secret = tmp_path / "secret.env"
    secret.write_text("API_KEY=hunter2\n")
    (repo / "config.env").symlink_to(secret)
    ws = Workspace(repo)

    with pytest.raises(WorkspaceError):
        ws.read_text("config.env")
    with pytest.raises(WorkspaceError):
        ws.write_text("config.env", "overwritten")
    assert "config.env" not in ws.files()
    assert secret.read_text() == "API_KEY=hunter2\n"


def test_listing_skips_ignored_dirs_and_binary_files(repo):
    assert Workspace(repo).files() == ["README.md", "app/core.py", "app/view.js"]


# ── Diff round trip ──────────────────────────────────────────────────────


def _apply_and_compare(pristine, ws):
    """`git apply` the workspace diff onto an untouched copy; trees must match."""
    patch = pristine.parent / "fix.patch"
    patch.write_text(ws.diff())
    subprocess.run(["git", "apply", str(patch)], cwd=pristine, check=True, capture_output=True)
    for rel in ws.files():
        assert (pristine / rel).read_bytes() == (ws.root / rel).read_bytes(), rel
    for rel in ws.changed_files():
        assert (pristine / rel).exists() == (ws.root / rel).exists(), rel


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_diff_applies_cleanly_with_git(repo, tmp_path):
    pristine = tmp_path / "pristine"
    (repo / "notes.txt").write_text("first\nlast line without newline")
    shutil.copytree(repo, pristine)
    ws = Workspace(repo)

    ws.write_text(
        "app/core.py", ws.read_text("app/core.py").replace("return key", "return key.lower()")
    )
    ws.write_text("app/new_module.py", "VALUE = 1\n")  # new file
    ws.write_text("app/empty.py", "")  # new empty file
    ws.write_text("notes.txt", "first\nchanged last line without newline")
    ws.delete("README.md")  # deleted file

    assert ws.changed_files() == [
        "README.md", "app/core.py", "app/empty.py", "app/new_module.py", "notes.txt",
    ]  # fmt: skip
    diff = ws.diff()
    assert "--- a/app/core.py" in diff and "+++ b/app/core.py" in diff
    assert "\\ No newline at end of file" in diff
    _apply_and_compare(pristine, ws)


def test_reverting_a_change_removes_it_from_the_diff(repo):
    ws = Workspace(repo)
    original = ws.read_text("README.md")
    ws.write_text("README.md", "changed\n")
    ws.write_text("README.md", original)
    ws.write_text("scratch.py", "x = 1\n")
    ws.delete("scratch.py")
    assert ws.changed_files() == []
    assert ws.diff() == ""


# ── Tools ────────────────────────────────────────────────────────────────


def test_search_and_read_file(repo):
    tools = Toolbox(Workspace(repo))
    assert tools.call("search", {"pattern": r"def \w+\("}) == (
        "app/core.py:4: def load(path):\napp/core.py:9: def get(self, key):"
    )
    assert tools.call("search", {"pattern": "render", "glob": "*.py"}) == "(no matches)"
    # an invalid regex is searched for literally instead of failing
    assert "app/core.py:4" in tools.call("search", {"pattern": "load(path"})

    out = tools.call("read_file", {"path": "app/core.py", "start": 4, "end": 5})
    assert out.splitlines() == [
        "app/core.py (lines 4-5 of 10) — continue with start=6",
        "    4| def load(path):",
        "    5|     return open(path).read()",
    ]


def test_runaway_regex_is_cut_off_instead_of_hanging_the_worker(repo, monkeypatch):
    from agents import tools as tools_module

    monkeypatch.setattr(tools_module, "SEARCH_TIME_LIMIT_SECONDS", 0.5)
    (repo / "trap.txt").write_text("a" * 60 + "b\n")
    out = Toolbox(Workspace(repo)).call("search", {"pattern": r"(a+)+$"})
    assert out.startswith("ERROR: the search took too long")


def test_edit_file_requires_an_exact_unique_match(repo):
    ws = Workspace(repo)
    tools = Toolbox(ws)
    (repo / "dup.py").write_text("x = 1\ny = 2\nx = 1\n")

    assert tools.call("edit_file", {"path": "dup.py", "old": "x = 1", "new": "x = 3"}).startswith(
        'ERROR: "old" matches 2 places'
    )
    assert ws.changed_files() == []

    missing = tools.call("edit_file", {"path": "app/core.py", "old": "return  key", "new": "x"})
    assert missing.startswith('ERROR: "old" was not found')

    ok = tools.call(
        "edit_file", {"path": "dup.py", "old": "x = 1", "new": "x = 3", "replace_all": True}
    )
    assert ok == (
        "Edited dup.py (2 replacement(s)). The first one now reads:\n"
        "    1| x = 3\n    2| y = 2\n    3| x = 3"
    )
    assert (repo / "dup.py").read_text() == "x = 3\ny = 2\nx = 3\n"


def test_edit_file_warns_when_the_file_stops_parsing(repo):
    tools = Toolbox(Workspace(repo))
    out = tools.call(
        "edit_file", {"path": "app/core.py", "old": "def load(path):", "new": "def load(path:"}
    )
    assert "WARNING: app/core.py no longer parses" in out


def test_edit_file_preserves_crlf_line_endings(repo):
    (repo / "win.py").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
    tools = Toolbox(Workspace(repo))
    # the model writes plain "\n"; the file must stay CRLF throughout
    out = tools.call("edit_file", {"path": "win.py", "old": "a = 1\nb = 2", "new": "a = 1\nb = 20"})
    assert out.startswith("Edited win.py")
    assert (repo / "win.py").read_bytes() == b"a = 1\r\nb = 20\r\nc = 3\r\n"


def test_tool_errors_are_reported_not_raised(repo):
    tools = Toolbox(Workspace(repo))
    assert tools.call("read_file", {"path": "../x"}).startswith("ERROR:")
    assert tools.call("read_file", {}).startswith("ERROR:")
    assert tools.call("launch_missiles", {}).startswith("ERROR: unknown tool")
    assert tools.call("run", {"command": "id"}).startswith("ERROR: unknown tool")  # no sandbox


# ── Repo map ─────────────────────────────────────────────────────────────


def test_repo_map_lists_every_file_with_its_symbols(repo):
    repo_map = build_repo_map(Workspace(repo))
    assert "app/core.py (10 lines)\n    def load; class Store: get" in repo_map
    assert "app/view.js (3 lines)\n    function render" in repo_map
    assert "README.md" in repo_map
    assert "node_modules" not in repo_map


def test_repo_map_degrades_to_fit_its_budget(repo):
    for i in range(60):
        (repo / "app" / f"module_{i:02}.py").write_text(f"def handler_{i}():\n    return {i}\n")
    ws = Workspace(repo)

    full = build_repo_map(ws)
    assert "def handler_59" in full

    paths_only = build_repo_map(ws, budget_chars=len(full) - 1)
    assert "symbols omitted" in paths_only
    assert "app/module_59.py" in paths_only and "def handler_59" not in paths_only

    dirs_only = build_repo_map(ws, budget_chars=300)
    assert "showing directories only" in dirs_only
    assert "app/  (62 files)" in dirs_only and "module_59" not in dirs_only
    assert len(dirs_only) <= 300
