"""Command-line entry point for the repo fix agent.

    python repo-agent/main.py /path/to/repo "Checkout crashes when no coupon is applied"
    python repo-agent/main.py /path/to/repo "..." --report "review text" --out fix.patch

The repository is never modified: the agent works on a temporary copy and
prints a unified diff you can apply with ``git apply``.
"""

import argparse
import sys
from pathlib import Path

# Make the repo root importable so the shared `config`, `llm` and
# `repo_agent_core` modules resolve when this is run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repo_agent_core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Investigate a problem in a repo and propose a fix."
    )
    parser.add_argument("repo", nargs="?", help="path to the repository (default: prompt)")
    parser.add_argument("goal", nargs="?", help="the problem to fix (default: prompt)")
    parser.add_argument(
        "--report", action="append", default=[], help="a customer report / symptom (repeatable)"
    )
    parser.add_argument("--out", help="write the patch to this file instead of stdout")
    args = parser.parse_args(argv)

    repo = args.repo or input("Repository path: ").strip() or "."
    if not Path(repo).is_dir():
        print(f"error: {repo!r} is not a directory", file=sys.stderr)
        return 2
    goal = args.goal or input("Describe the problem to fix:\n> ").strip()
    if not goal:
        print("error: no problem description given", file=sys.stderr)
        return 2

    def progress(event: dict) -> None:
        print(f"[step {event['step']}] {', '.join(event['tools'])}", file=sys.stderr)

    result = repo_agent_core.analyze(repo, goal, reports=args.report, on_event=progress)

    if result["status"] != "ok":
        detail = result.get("error") or result.get("summary") or ""
        print(f"No fix produced ({result['status']}): {detail}", file=sys.stderr)
        return 1

    tests = result["verification"]["tests"]
    print(f"\nRoot cause: {result['root_cause']}", file=sys.stderr)
    print(f"Fix: {result['summary']}", file=sys.stderr)
    print(
        "Tests: " + ("; ".join(tests["lines"]) if tests["ran"] else f"not run ({tests['reason']})"),
        file=sys.stderr,
    )
    if args.out:
        Path(args.out).write_text(result["diff"], encoding="utf-8")
        print(f"Patch written to {args.out} — apply with: git apply {args.out}", file=sys.stderr)
    else:
        print(result["diff"], end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
