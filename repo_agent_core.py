"""Synchronous repo-agent pipeline, decoupled from any transport.

`repo_agent_server` wraps this for SSE; the background worker calls it directly
to turn a bucketed-feedback goal into a change plan.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO_AGENT_DIR = Path(__file__).resolve().parent / "repo-agent"
if str(_REPO_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_AGENT_DIR))

log = logging.getLogger("prodrion.repo_agent")


def analyze(repo_path: str, goal: str, index_path: str | None = None) -> dict:
    """Run scan -> chunk -> index -> validate -> discover -> locate -> plan.

    ``index_path``, if given, is a job-scoped directory for the vector index
    — callers analysing more than one repo over the process lifetime (the
    production worker) must pass a distinct path per job, since the index
    isn't otherwise keyed by repo and a shared/default one would silently
    serve stale, wrong-repo results after the first analysis. The standalone
    CLI (repo-agent/main.py) omits it and gets the old single-repo-at-a-time
    default (see CodeIndexer).

    Returns ``{"status", "plan"?, "results"?, "error"?}``.
    """
    from agents.behaviour_locator import BehaviorLocator
    from agents.discovery import DiscoveryAgent
    from agents.goal_validator import GoalValidator
    from agents.planner import ChangePlanner
    from repo.chunker import CodeChunker
    from repo.indexer import CodeIndexer
    from repo.scanner import RepoScanner

    files = RepoScanner(repo_path).scan_files()
    if not files:
        return {"status": "error", "error": "No supported source files found in repo."}

    chunker = CodeChunker()
    chunks = []
    for rel in files:
        chunks.extend(chunker.chunk_file(Path(repo_path) / rel))

    indexer = CodeIndexer(index_path) if index_path else CodeIndexer()
    if indexer.is_empty():
        indexer.add_chunks(chunks)

    status = GoalValidator(files).validate(goal).upper()
    if "INVALID" in status:
        return {
            "status": "invalid_goal",
            "error": "Goal is not compatible with this repository architecture.",
        }

    discovered = DiscoveryAgent(indexer, files, repo_path).discover(goal)
    if not discovered:
        return {"status": "no_match", "error": "No relevant files found for this goal."}

    hits = BehaviorLocator(repo_path).locate(goal, discovered)
    patches = ChangePlanner(repo_path).plan(goal, hits)

    return {
        "status": "ok",
        "plan": {
            "goal": goal,
            "files_to_modify": discovered,
            "hit_count": len(hits),
        },
        "results": [
            {"warnings": p.get("warnings", []), "patch": p.get("patch", "")} for p in patches
        ],
    }
