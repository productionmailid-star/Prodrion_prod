import os
import sys
from pathlib import Path

from agents.behaviour_locator import BehaviorLocator
from agents.discovery import DiscoveryAgent
from agents.goal_validator import GoalValidator
from agents.planner import ChangePlanner
from agents.reporter import Reporter
from repo.chunker import CodeChunker
from repo.indexer import CodeIndexer
from repo.scanner import RepoScanner


def print_header():
    print("\n" + "=" * 60)
    print("   CODE AGENT  —  Powered by CodeLlama + Ollama")
    print("=" * 60)


def get_repo_path():
    path = input("\nRepository path (press Enter for current directory): ").strip()
    if not path:
        path = os.getcwd()
    p = Path(path)
    if not p.exists() or not p.is_dir():
        print(f"\n  Error: '{path}' is not a valid directory.")
        sys.exit(1)
    return str(p.resolve())


def step(n, total, label):
    print(f"\n[{n}/{total}] {label}")


def main():
    print_header()

    TOTAL_STEPS = 7

    # ── STEP 1 — Scan ──────────────────────────────────────────
    step(1, TOTAL_STEPS, "Scanning repository...")

    REPO_PATH = get_repo_path()
    print(f"  Using: {REPO_PATH}")

    scanner = RepoScanner(REPO_PATH)
    files   = scanner.scan_files()

    if not files:
        print("  No supported source files found. Check filters.py for extensions.")
        sys.exit(1)

    print(f"  Found {len(files)} source file(s)")

    # ── STEP 2 — Chunk ─────────────────────────────────────────
    step(2, TOTAL_STEPS, "Chunking code...")

    chunker    = CodeChunker()
    all_chunks = []

    for file in files:
        full_path = Path(REPO_PATH) / file
        all_chunks.extend(chunker.chunk_file(full_path))

    print(f"  Generated {len(all_chunks)} chunk(s)")

    # ── STEP 3 — Index ─────────────────────────────────────────
    step(3, TOTAL_STEPS, "Preparing index...")

    indexer = CodeIndexer()

    if indexer.is_empty():
        print("  No existing index — indexing now...")
        indexer.add_chunks(all_chunks)
    else:
        choice = input("  Index already exists. Re-index? (y/N): ").strip().lower()
        if choice == "y":
            print("  Clearing old index and re-indexing...")
            indexer.clear()
            indexer.add_chunks(all_chunks)
        else:
            print("  Using existing index.")

    # ── STEP 4 — Goal ──────────────────────────────────────────
    step(4, TOTAL_STEPS, "Goal input")

    print()
    goal = input("  Describe your goal:\n  > ").strip()

    if not goal:
        print("\n  No goal provided. Exiting.")
        sys.exit(0)

    # ── STEP 5 — Validate ──────────────────────────────────────
    step(5, TOTAL_STEPS, "Validating goal against repository...")

    validator = GoalValidator(files)
    status    = validator.validate(goal)

    print(f"  Result: {status}")

    if "INVALID" in status.upper():
        print("\n  Goal is not compatible with this repository architecture.")
        print("  Tip: Try rephrasing to focus on backend logic.")
        sys.exit(0)

    # ── STEP 6 — Discover ──────────────────────────────────────
    step(6, TOTAL_STEPS, "Discovering relevant files...")

    discovery        = DiscoveryAgent(indexer, files, REPO_PATH)
    discovered_files = discovery.discover(goal)

    if not discovered_files:
        print("  No relevant files found. Try rephrasing your goal.")
        sys.exit(0)

    print(f"  Found {len(discovered_files)} relevant file(s):")
    for f in discovered_files:
        print(f"    • {f}")

    # ── STEP 7 — Locate exact lines ────────────────────────────
    step(7, TOTAL_STEPS, "Locating target lines and generating patches...")

    locator = BehaviorLocator(REPO_PATH)
    hits    = locator.locate(goal, discovered_files)

    if not hits:
        print("\n  Could not locate specific lines to change.")
        print("  Try a more specific goal, e.g. 'deactivate user instead of deleting'.")
        sys.exit(0)

    print(f"\n  Planning {len(hits)} change(s)...")

    planner = ChangePlanner(REPO_PATH)
    patches = planner.plan(goal, hits)

    # ── Report ─────────────────────────────────────────────────
    reporter = Reporter()
    reporter.display(goal, discovered_files, patches)


if __name__ == "__main__":
    main()
