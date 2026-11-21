"""Static verification (repo/verify.py) and sandboxed test execution
(repo/sandbox.py): the two layers that keep a bad fix from being shipped."""

from __future__ import annotations

import json
import sys

import pytest
from repo import verify
from repo.sandbox import (
    CommandResult,
    DockerSandbox,
    LocalSandbox,
    Sandbox,
    TestHarness,
    TestPlan,
    detect_test_plan,
    make_sandbox,
)
from repo.workspace import Workspace

# ── Syntax verification ──────────────────────────────────────────────────


def test_python_and_json_syntax_errors_are_reported():
    assert verify.syntax_error("a.py", "def f():\n    return 1\n") is None
    assert verify.syntax_error("a.py", "def f(:\n    return 1\n").startswith("line 1")
    assert verify.syntax_error("a.json", '{"a": 1}') is None
    assert verify.syntax_error("a.json", '{"a": 1,}') is not None
    assert verify.syntax_error("notes.txt", "anything (goes") is None
    assert verify.can_check("a.py") and not verify.can_check("notes.txt")


def test_javascript_and_typescript_syntax_errors_are_reported():
    pytest.importorskip("tree_sitter_language_pack")
    assert verify.can_check("a.ts")
    assert verify.syntax_error("a.js", "function f(a) {\n  return a + 1;\n}\n") is None
    assert verify.syntax_error("a.js", "function f(a) {\n  return a + ;\n}\n") is not None
    assert (
        verify.syntax_error("a.tsx", "const x: number = 1;\nexport const C = () => <div/>;\n")
        is None
    )
    assert verify.syntax_error("a.ts", "const x: number = ;\n") is not None


def test_check_changes_blames_only_new_breakage(tmp_path):
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "already_broken.py").write_text("def f(:\n")
    ws = Workspace(tmp_path)

    ws.write_text("good.py", "x = (1\n")
    ws.write_text("already_broken.py", "def f(:\n    pass\n")
    ws.write_text("notes.txt", "free text\n")
    report = verify.check_changes(ws)

    assert report["ok"] is False
    assert list(report["errors"]) == ["good.py"]
    assert report["unchecked"] == ["notes.txt"]

    ws.write_text("good.py", "x = (1)\n")
    assert verify.check_changes(ws)["ok"] is True


# ── Test plan detection ──────────────────────────────────────────────────


def test_detect_python_test_plan(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "requirements.txt").write_text("requests\n")
    plan = detect_test_plan(tmp_path)
    assert plan.setup == [
        "python -m pip install -q -r requirements.txt",
        "python -m pip install -q pytest",
    ]
    assert plan.commands == ["python -m pytest -q -rfE --no-header -p no:cacheprovider"]


def test_detect_node_test_plan_and_overrides(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    plan = detect_test_plan(tmp_path)
    assert plan.setup == ["npm ci --ignore-scripts --no-audit --no-fund"]
    assert plan.commands == ["npm test"]

    plan = detect_test_plan(tmp_path, setup_override="make deps", test_override="make test")
    assert (plan.setup, plan.commands) == (["make deps"], ["make test"])


def test_no_tests_detected(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
    )
    assert detect_test_plan(tmp_path).commands == []


# ── Baseline / regression logic ──────────────────────────────────────────


class ScriptedSandbox(Sandbox):
    """Returns queued results for test commands; records everything it ran."""

    can_install = True

    def __init__(self, results):
        self.results = list(results)
        self.ran: list[tuple[str, bool]] = []

    def run(self, command, *, timeout, network=False):
        self.ran.append((command, network))
        if command.startswith("install"):
            return CommandResult(0, "")
        return self.results.pop(0)


PASS = CommandResult(0, "....\n4 passed in 0.10s\n")
ONE_FAIL = CommandResult(
    1, "FAILED tests/test_a.py::test_old - assert 1 == 2\n1 failed, 3 passed in 0.1s\n"
)
TWO_FAIL = CommandResult(
    1,
    "FAILED tests/test_a.py::test_old - assert 1 == 2\n"
    "FAILED tests/test_a.py::test_new - assert 3 == 4\n2 failed, 2 passed in 0.1s\n",
)
BROKEN = CommandResult(2, "ERROR tests/test_a.py - SyntaxError\n1 error in 0.1s\n")


def _harness(*results):
    sandbox = ScriptedSandbox(results)
    harness = TestHarness(sandbox, TestPlan(setup=["install deps"], commands=["pytest"]))
    harness.prepare()
    return harness, sandbox


def test_setup_gets_network_and_tests_do_not():
    _, sandbox = _harness(PASS)
    assert sandbox.ran == [("install deps", True), ("pytest", False)]


def test_passing_suite_that_still_passes_is_ok():
    harness, _ = _harness(PASS, PASS)
    outcome = harness.check()
    assert outcome["ran"] and outcome["ok"]
    assert outcome["lines"] == ["`pytest`: 4 passed in 0.10s before -> 4 passed in 0.10s after"]


def test_new_failure_is_a_regression():
    harness, _ = _harness(PASS, ONE_FAIL)
    outcome = harness.check()
    assert not outcome["ok"]
    assert "tests/test_a.py::test_old" in outcome["regressions"][0]


def test_pre_existing_failures_are_not_blamed_on_the_fix():
    harness, _ = _harness(ONE_FAIL, ONE_FAIL)
    assert harness.check()["ok"]

    harness, _ = _harness(ONE_FAIL, TWO_FAIL)
    outcome = harness.check()
    assert not outcome["ok"]
    assert "test_new" in outcome["regressions"][0] and "test_old" not in outcome["regressions"][0]


def test_suite_that_stops_running_is_a_regression():
    harness, _ = _harness(PASS, BROKEN)
    outcome = harness.check()
    assert not outcome["ok"] and "no longer runs" in outcome["regressions"][0]


def test_unrunnable_baseline_disables_test_verification():
    harness, _ = _harness(BROKEN)
    assert harness.available is False
    assert "could not be run before the change" in harness.reason
    assert harness.check() == {
        "ran": False,
        "ok": True,
        "regressions": [],
        "lines": [],
        "output": "",
    }

    empty = TestHarness(ScriptedSandbox([]), TestPlan())
    empty.prepare()
    assert empty.available is False and "no test suite" in empty.reason


# ── Sandboxes ────────────────────────────────────────────────────────────


def test_local_sandbox_scrubs_the_environment_and_enforces_timeouts(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODRION_GEMINI_API_KEY", "super-secret")
    sandbox = LocalSandbox(tmp_path)

    result = sandbox.run("env; pwd", timeout=30)
    assert result.ok
    assert "super-secret" not in result.output and "PRODRION_" not in result.output
    assert str(tmp_path) in result.output

    assert sandbox.run("exit 3", timeout=30).exit_code == 3

    slow = sandbox.run(f"{sys.executable} -c 'import time; time.sleep(30)'", timeout=0.5)
    assert slow.timed_out and slow.exit_code is None


def test_docker_sandbox_is_locked_down(tmp_path):
    sandbox = DockerSandbox(tmp_path / "repo", tmp_path / "deps", "img:1", memory="1g", cpus=1.5)
    argv = sandbox.command_line("pytest -q", network=False, name="job1")

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert argv[:3] == ["docker", "run", "--rm"]
    assert value("--network") == "none"
    assert value("--cap-drop") == "ALL"
    assert value("--security-opt") == "no-new-privileges"
    assert value("--memory") == "1g" and value("--cpus") == "1.5"
    assert "--read-only" in argv
    assert f"{tmp_path / 'repo'}:/work:rw" in argv
    assert not any(arg.startswith("PRODRION_") for arg in argv)
    assert argv[-3:-1] == ["sh", "-c"] and argv[-1].endswith("pytest -q")
    assert argv[argv.index("img:1") + 1] == "sh"

    with_network = sandbox.command_line("pip install x", network=True, name="job2")
    assert with_network[with_network.index("--network") + 1] == "bridge"


def test_make_sandbox_modes(tmp_path):
    assert make_sandbox("off", tmp_path) is None
    assert isinstance(make_sandbox("local", tmp_path), LocalSandbox)
    docker = make_sandbox("docker", tmp_path / "clone", image="img:1")
    assert isinstance(docker, DockerSandbox)
    assert docker.deps_dir == str(tmp_path / "clone__deps")  # sibling, never inside the repo
