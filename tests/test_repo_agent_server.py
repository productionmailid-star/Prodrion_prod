"""End-to-end test of the dev SSE wrapper (repo_agent_server.py): start a run,
then read the event stream the agent thread pushes onto the server's loop."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import llm
import repo_agent_server
from llm.fake import FakeLLM


def _reply(tool, args):
    return json.dumps({"thought": "t", "actions": [{"tool": tool, "args": args}]})


@pytest.fixture
def client(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n")
    monkeypatch.setattr(repo_agent_server.settings, "repo_agent_allowed_base", str(tmp_path))

    llm.reset_cache()
    llm._llm = FakeLLM(
        script=[
            _reply("edit_file", {"path": "app.py", "old": "return 41", "new": "return 42"}),
            _reply("finish", {"summary": "Fixed the answer.", "root_cause": "Off by one."}),
        ]
    )
    with TestClient(repo_agent_server.app) as test_client:
        yield test_client, repo
    llm.reset_cache()


def _events(text):
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_run_streams_steps_then_the_verified_diff(client):
    test_client, repo = client
    started = test_client.post("/api/run", json={"goal": "answer is wrong", "repo_path": str(repo)})
    assert started.status_code == 200
    session_id = started.json()["session_id"]

    stream = test_client.get(f"/api/stream/{session_id}")
    events = _events(stream.text)

    assert [name for name, _ in events] == ["step", "step", "plan", "results", "done"]
    assert events[2][1]["plan"]["files_to_modify"] == ["app.py"]
    results = events[3][1]
    assert results["root_cause"] == "Off by one."
    assert "-    return 41\n+    return 42\n" in results["diff"]
    assert events[4][1] == {"status": "ok"}
    assert (repo / "app.py").read_text() == "def answer():\n    return 41\n"  # source untouched


def test_run_rejects_paths_outside_the_allowed_base(client, tmp_path_factory):
    test_client, _ = client
    outside = tmp_path_factory.mktemp("outside")
    resp = test_client.post("/api/run", json={"goal": "x", "repo_path": str(outside)})
    assert resp.status_code == 400
