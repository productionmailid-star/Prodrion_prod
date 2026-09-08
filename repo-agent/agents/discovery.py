from pathlib import Path

from agents.llm import LocalLLM


class DiscoveryAgent:
    """
    Uses semantic search + LLM reasoning to identify which files
    in the repo are relevant to the developer's goal.
    """

    def __init__(self, indexer, repo_files: list, repo_path: str):
        self.llm = LocalLLM()
        self.indexer = indexer
        self.repo_files = repo_files
        self.repo_root = Path(repo_path).resolve()

    # ── Helpers ──────────────────────────────────────────────

    def _resolve_relative(self, file_path: str) -> str | None:
        """Convert an absolute path to a repo-relative posix path."""
        p = Path(file_path).resolve()
        try:
            return p.relative_to(self.repo_root).as_posix()
        except ValueError:
            return None

    # ── Main ─────────────────────────────────────────────────

    def discover(self, goal: str) -> list:
        """
        Return a deduplicated list of repo-relative file paths
        that are most likely to need modification for the goal.
        """

        # ── Semantic retrieval ───────────────────────────────
        results = self.indexer.search(goal, k=8)

        # Map file → line ranges from vector results
        hit_files: dict[str, list[str]] = {}

        for meta in results["metadatas"][0]:
            rel = self._resolve_relative(meta["file"])
            if rel is None:
                continue
            hit_files.setdefault(rel, []).append(
                f"{meta['start_line']}–{meta['end_line']}"
            )

        # Build context summary for the LLM
        hit_summary = "\n".join(
            f"  {file}  (lines {', '.join(ranges[:3])})"
            for file, ranges in hit_files.items()
        )

        repo_file_list = "\n".join(f"  {f}" for f in self.repo_files)

        # ── LLM grounding ────────────────────────────────────
        system = """\
You are a senior software engineer performing a code-impact analysis.

Given a developer goal and a list of files in a backend repository,
decide which files MUST be modified to achieve the goal.

Rules:
- Only list files that ALREADY EXIST in the repository.
- Choose the MINIMUM set of files — avoid unnecessary changes.
- Do not invent file names.
- Output ONLY a plain list, one file per line, no explanations.
"""

        user = f"""\
Developer Goal:
{goal}

All repository files:
{repo_file_list}

Files flagged by semantic search (likely relevant):
{hit_summary if hit_summary else "  (none found by search)"}

Return only the files that must be modified, one per line:
"""

        llm_response = self.llm.chat(system, user)

        # ── Parse LLM output ─────────────────────────────────
        # Accept any line that matches a real repo file
        valid_files = set(self.repo_files)
        chosen: list[str] = []
        seen: set[str] = set()

        for line in llm_response.splitlines():
            candidate = line.strip().strip("-•*").strip()
            # Normalize backslashes (Windows paths from LLM)
            candidate = candidate.replace("\\", "/")
            if candidate in valid_files and candidate not in seen:
                chosen.append(candidate)
                seen.add(candidate)

        # Fallback: if LLM returned nothing useful, use semantic hits
        if not chosen:
            chosen = list(hit_files.keys())

        return chosen
