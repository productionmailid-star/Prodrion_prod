"""Synchronous entry point to the repo fix agent, decoupled from any transport.

`repo_agent_server` wraps this for SSE; the background worker calls it directly
to turn a bucket of customer complaints into a verified code change.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

_REPO_AGENT_DIR = Path(__file__).resolve().parent / "repo-agent"
if str(_REPO_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_AGENT_DIR))

log = logging.getLogger("prodrion.repo_agent")


def analyze(
    repo_path: str,
    goal: str,
    *,
    reports: list[str] | None = None,
    in_place: bool = False,
    deps_dir: str | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """Investigate ``goal`` in the repository and produce a verified fix.

    ``reports`` are the customer complaints behind the goal (verbatim); they
    give the agent the actual symptoms instead of just a category label.

    The repository at ``repo_path`` is never modified unless ``in_place`` is
    true: by default the agent works on a throwaway copy. The worker passes
    ``in_place=True`` because its clone is already disposable.

    Returns a dict with ``status``:

    * ``ok`` — ``diff`` is a unified diff (``git apply`` format) that passed
      verification; also ``summary``, ``root_cause``, ``files_changed``,
      ``verification``.
    * ``not_actionable`` — the agent investigated and declined (``summary``
      holds its reason).
    * ``no_fix`` — no verified fix within the step / time budget (``error``).
    * ``error`` — nothing to analyse (``error``).

    Operational failures (model API unreachable after retries) raise
    ``agents.fix_agent.AgentError`` so the caller can retry later.
    """
    source = Path(repo_path)
    if not source.is_dir():
        return {"status": "error", "error": f"{repo_path} is not a directory"}

    if in_place:
        return _run(source, goal, reports, deps_dir, on_event)

    from repo.filters import IGNORE_DIRS

    with tempfile.TemporaryDirectory(prefix="prodrion-fix-") as scratch:
        copy = Path(scratch) / "repo"
        shutil.copytree(source, copy, symlinks=True, ignore=shutil.ignore_patterns(*IGNORE_DIRS))
        return _run(copy, goal, reports, deps_dir or str(Path(scratch) / "deps"), on_event)


def _run(
    root: Path,
    goal: str,
    reports: list[str] | None,
    deps_dir: str | None,
    on_event: Callable[[dict], None] | None,
) -> dict:
    from agents.fix_agent import FixAgent
    from repo.sandbox import TestHarness, detect_test_plan, make_sandbox
    from repo.workspace import Workspace

    from config import get_settings
    from llm import get_llm

    settings = get_settings()
    workspace = Workspace(root)
    if not workspace.files():
        return {"status": "error", "error": "No readable source files found in the repository."}

    mode = settings.repo_agent_sandbox
    tests_disabled_reason = ""
    if mode == "docker" and shutil.which("docker") is None:
        # Never fall back to running customer code unsandboxed; say so in the ticket.
        log.error(
            "PRODRION_REPO_AGENT_SANDBOX=docker but docker is not installed; not running tests"
        )
        tests_disabled_reason = "the docker sandbox is configured but docker is not installed"
        mode = "off"

    sandbox = make_sandbox(
        mode,
        root,
        deps_dir=deps_dir,
        image=settings.repo_agent_sandbox_image,
        memory=settings.repo_agent_sandbox_memory,
        cpus=settings.repo_agent_sandbox_cpus,
    )
    harness = None
    if sandbox is not None:
        plan = detect_test_plan(
            root,
            setup_override=settings.repo_agent_setup_command,
            test_override=settings.repo_agent_test_command,
        )
        harness = TestHarness(sandbox, plan, timeout=settings.repo_agent_command_timeout_seconds)
        harness.prepare()
        log.info("Test baseline: %s", harness.describe_baseline())

    agent = FixAgent(
        workspace,
        get_llm(),
        sandbox=sandbox,
        harness=harness,
        max_steps=settings.repo_agent_max_steps,
        timeout_seconds=settings.repo_agent_timeout_seconds,
        command_timeout=settings.repo_agent_command_timeout_seconds,
        tests_disabled_reason=tests_disabled_reason,
        on_event=on_event,
    )
    result = agent.run(goal, reports).to_dict()
    result["goal"] = goal
    log.info(
        "Fix agent finished: status=%s steps=%d files=%s",
        result["status"],
        result["steps"],
        result["files_changed"],
    )
    return result
