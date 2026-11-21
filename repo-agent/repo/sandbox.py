"""Running the target repository's own tests, in isolation.

The repository being fixed is customer code: untrusted from this host's point
of view. So execution is opt-in (``PRODRION_REPO_AGENT_SANDBOX``):

``off``     nothing from the repo is ever executed; the agent relies on static
            syntax checks only. The default.
``docker``  every command runs in a throwaway container: no network except
            during dependency install, no capabilities, non-root, read-only
            root filesystem, CPU / memory / process limits, and none of this
            process's environment (so no secrets).
``local``   commands run directly on this host with a scrubbed environment.
            For development and tests only; rejected in production.

:class:`TestHarness` records which tests pass before the agent changes
anything, so a fix is only accepted if it breaks nothing that used to work.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from repo.filters import IGNORE_DIRS

_OUTPUT_CAP_CHARS = 20_000
_SETUP_TIMEOUT_SECONDS = 900.0

_CONTAINER_PRELUDE = (
    "export PATH=/deps/py/bin:/work/node_modules/.bin:$PATH; "
    "mkdir -p /deps/home /deps/py /deps/npm; "
)


@dataclass
class CommandResult:
    exit_code: int | None  # None when the command timed out
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _tail(text: str, limit: int = _OUTPUT_CAP_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters dropped ...]\n" + text[-limit:]


class Sandbox:
    """Interface: run a shell command against the workspace."""

    name = "sandbox"
    #: Whether dependency-install commands may be run here.
    can_install = False

    def run(self, command: str, *, timeout: float, network: bool = False) -> CommandResult:
        raise NotImplementedError


class LocalSandbox(Sandbox):
    """Run on this host. Development / test use only — no isolation."""

    name = "local"
    can_install = False  # never pip/npm-install into the developer's machine

    def __init__(self, workdir: str | os.PathLike[str]):
        self.workdir = str(workdir)

    def _env(self) -> dict[str, str]:
        # Only what a test run needs; in particular no PRODRION_* secrets.
        keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "VIRTUAL_ENV")
        env = {key: os.environ[key] for key in keep if key in os.environ}
        env["HOME"] = self.workdir
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["CI"] = "1"
        return env

    def run(self, command: str, *, timeout: float, network: bool = False) -> CommandResult:
        proc = subprocess.Popen(
            ["sh", "-c", command],
            cwd=self.workdir,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = proc.communicate()
            return CommandResult(None, _tail(out.decode("utf-8", "replace")), timed_out=True)
        return CommandResult(proc.returncode, _tail(out.decode("utf-8", "replace")))


class DockerSandbox(Sandbox):
    """Run each command in a fresh, locked-down container."""

    name = "docker"
    can_install = True

    def __init__(
        self,
        workdir: str | os.PathLike[str],
        deps_dir: str | os.PathLike[str],
        image: str,
        *,
        memory: str = "2g",
        cpus: float = 2.0,
        pids_limit: int = 512,
    ):
        self.workdir = str(Path(workdir).resolve())
        self.deps_dir = str(Path(deps_dir).resolve())
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    def command_line(self, command: str, *, network: bool, name: str) -> list[str]:
        return [
            "docker", "run", "--rm", "--init",
            "--name", name,
            "--network", "bridge" if network else "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory,
            "--cpus", str(self.cpus),
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=512m",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{self.workdir}:/work:rw",
            "-v", f"{self.deps_dir}:/deps:rw",
            "-w", "/work",
            "-e", "HOME=/deps/home",
            "-e", "PYTHONUSERBASE=/deps/py",
            "-e", "PIP_USER=1",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "npm_config_cache=/deps/npm",
            "-e", "CI=1",
            self.image,
            "sh", "-c", _CONTAINER_PRELUDE + command,
        ]  # fmt: skip

    def run(self, command: str, *, timeout: float, network: bool = False) -> CommandResult:
        Path(self.deps_dir).mkdir(parents=True, exist_ok=True)
        name = f"prodrion-fix-{uuid.uuid4().hex[:12]}"
        argv = self.command_line(command, network=network, name=name)
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
            partial = (exc.stdout or b"").decode("utf-8", "replace")
            return CommandResult(None, _tail(partial), timed_out=True)
        except FileNotFoundError:
            return CommandResult(127, "docker is not installed on this host")
        return CommandResult(proc.returncode, _tail(proc.stdout.decode("utf-8", "replace")))


def make_sandbox(
    mode: str,
    workdir: str | os.PathLike[str],
    *,
    deps_dir: str | os.PathLike[str] | None = None,
    image: str = "",
    memory: str = "2g",
    cpus: float = 2.0,
) -> Sandbox | None:
    if mode == "docker":
        deps = deps_dir or (Path(workdir).resolve().parent / f"{Path(workdir).name}__deps")
        return DockerSandbox(workdir, deps, image, memory=memory, cpus=cpus)
    if mode == "local":
        return LocalSandbox(workdir)
    return None


# ── What to run ──────────────────────────────────────────────────────────


@dataclass
class TestPlan:
    __test__ = False  # not a pytest test class

    setup: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


_PYTHON_REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)
_PYTEST_COMMAND = "python -m pytest -q -rfE --no-header -p no:cacheprovider"


def _has_python_tests(root: Path) -> bool:
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for name in filenames:
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                return True
    return False


def detect_test_plan(
    root: str | os.PathLike[str], *, setup_override: str = "", test_override: str = ""
) -> TestPlan:
    """Work out how to install dependencies and run this repo's tests."""
    root = Path(root)
    plan = TestPlan()

    if _has_python_tests(root):
        for name in _PYTHON_REQUIREMENT_FILES:
            if (root / name).is_file():
                plan.setup.append(f"python -m pip install -q -r {shlex.quote(name)}")
        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
            plan.setup.append("python -m pip install -q -e .")
        plan.setup.append("python -m pip install -q pytest")
        plan.commands.append(_PYTEST_COMMAND)

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError, AttributeError):
            scripts = {}
        test_script = scripts.get("test", "") if isinstance(scripts, dict) else ""
        if test_script and "no test specified" not in test_script:
            install = "npm ci" if (root / "package-lock.json").is_file() else "npm install"
            plan.setup.append(f"{install} --ignore-scripts --no-audit --no-fund")
            plan.commands.append("npm test")

    if setup_override:
        plan.setup = [setup_override]
    if test_override:
        plan.commands = [test_override]
    return plan


# ── Before / after comparison ────────────────────────────────────────────

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_SUMMARY_LINE = re.compile(r"^.*\b\d+ (?:passed|failed|errors?|skipped)\b.*$", re.MULTILINE)


@dataclass
class TestRun:
    __test__ = False

    command: str
    exit_code: int | None
    timed_out: bool
    failed: frozenset[str]
    summary: str
    output: str

    @property
    def usable(self) -> bool:
        """The suite actually ran (0 = all passed, 1 = some tests failed)."""
        return not self.timed_out and self.exit_code in (0, 1)


def _to_run(command: str, result: CommandResult) -> TestRun:
    summaries = _SUMMARY_LINE.findall(result.output)
    summary = summaries[-1].strip(" =") if summaries else f"exit code {result.exit_code}"
    if result.timed_out:
        summary = "timed out"
    return TestRun(
        command=command,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        failed=frozenset(_FAILED_LINE.findall(result.output)),
        summary=summary,
        output=result.output,
    )


class TestHarness:
    """Baseline the repo's tests, then judge later runs against that baseline."""

    __test__ = False

    def __init__(self, sandbox: Sandbox, plan: TestPlan, *, timeout: float = 300.0):
        self.sandbox = sandbox
        self.plan = plan
        self.timeout = timeout
        self.baseline: list[TestRun] = []
        self.available = False
        self.reason = "tests have not been baselined"

    def prepare(self) -> None:
        """Install dependencies (if allowed) and record the starting test state."""
        if not self.plan.commands:
            self.reason = "no test suite was detected in the repository"
            return
        if self.sandbox.can_install:
            for command in self.plan.setup:
                self.sandbox.run(command, timeout=_SETUP_TIMEOUT_SECONDS, network=True)

        self.baseline = [self._run(command) for command in self.plan.commands]
        usable = [run for run in self.baseline if run.usable]
        if usable:
            self.available = True
            self.reason = ""
        else:
            first = self.baseline[0]
            self.reason = (
                f"the test suite could not be run before the change "
                f"(`{first.command}`: {first.summary})"
            )

    def _run(self, command: str) -> TestRun:
        return _to_run(command, self.sandbox.run(command, timeout=self.timeout, network=False))

    def describe_baseline(self) -> str:
        if not self.available:
            return f"unavailable: {self.reason}"
        return "; ".join(f"`{run.command}`: {run.summary}" for run in self.baseline if run.usable)

    def check(self) -> dict:
        """Re-run the tests and compare with the baseline.

        Returns ``{"ran", "ok", "regressions": [str], "lines": [str], "output": str}``.
        ``ok`` is False only for a demonstrated regression.
        """
        if not self.available:
            return {"ran": False, "ok": True, "regressions": [], "lines": [], "output": ""}

        regressions: list[str] = []
        lines: list[str] = []
        outputs: list[str] = []
        for before in self.baseline:
            if not before.usable:
                continue
            after = self._run(before.command)
            outputs.append(after.output)
            lines.append(f"`{before.command}`: {before.summary} before -> {after.summary} after")

            if not after.usable:
                regressions.append(f"`{before.command}` no longer runs: {after.summary}")
                continue
            newly_failing = sorted(after.failed - before.failed)
            if newly_failing:
                regressions.append(
                    f"`{before.command}`: newly failing: " + ", ".join(newly_failing[:20])
                )
            elif before.exit_code == 0 and after.exit_code != 0:
                regressions.append(f"`{before.command}` passed before and fails now")

        return {
            "ran": True,
            "ok": not regressions,
            "regressions": regressions,
            "lines": lines,
            "output": _tail("\n".join(outputs), 6000),
        }
