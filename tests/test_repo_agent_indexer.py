"""Regression test for a real production bug: CodeIndexer used one hardcoded
`./chroma_db` path shared across every repo it ever analysed. The *first*
repo analysed in a process's lifetime got indexed correctly; every repo
after that silently reused the first repo's stale index (`is_empty()` was
already False, so nothing got re-indexed, and search() returned chunks from
the wrong codebase) — a real cross-tenant data bug for the production
worker, which analyses a different customer's repo on every "Generate Fix"
job. Fixed by making the persist path a required-for-multi-repo-callers
constructor argument instead of a hardcoded default.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repo-agent"))

from repo.indexer import CodeIndexer  # noqa: E402

# conftest.py sets PRODRION_LLM_PROVIDER=fake for the whole test session
# before config.settings is ever constructed, so get_embedder() already
# resolves to the offline FakeEmbedder here with no extra setup.


def _chunk(file: str, code: str) -> dict:
    return {"file": file, "start_line": 1, "end_line": 1, "type": "function", "code": code}


def test_indexers_with_different_paths_do_not_share_data(tmp_path):
    index_a = CodeIndexer(str(tmp_path / "repo_a_index"))
    index_b = CodeIndexer(str(tmp_path / "repo_b_index"))

    assert index_a.is_empty()
    assert index_b.is_empty()

    index_a.add_chunks([_chunk("repo_a_only.py", "def handle_login(): return 1" * 5)])

    assert not index_a.is_empty()
    # The bug: a second CodeIndexer() with the default/shared path would
    # report is_empty() == False here too, having inherited repo A's data.
    assert index_b.is_empty(), "repo B's index must not see repo A's chunks"

    results = index_b.search("login", k=5)
    assert results["documents"] == [[]] or results["documents"] == []


def test_default_path_is_still_the_old_single_repo_behaviour(tmp_path, monkeypatch):
    # The standalone CLI (repo-agent/main.py) relies on CodeIndexer() with no
    # args still working the same way it always did.
    monkeypatch.chdir(tmp_path)
    indexer = CodeIndexer()
    assert indexer.is_empty()
    assert (tmp_path / "chroma_db").exists()
