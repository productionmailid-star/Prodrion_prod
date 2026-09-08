"""
repo_agent_server.py
--------------------
A thin FastAPI wrapper around the repo-agent pipeline.
Place this file at the project root (same level as goal_to_repo_agent.py).

Exposes:
  POST /api/run          — start a pipeline session
  GET  /api/stream/{id}  — SSE stream for plan / results / done events
  GET  /api/health       — liveness check

Start with:
  uvicorn repo_agent_server:app --port 7845

Does NOT modify any file inside repo-agent/.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ── Make repo-agent importable ───────────────────────────────────────────────
REPO_AGENT_DIR = Path(__file__).parent / "repo-agent"
sys.path.insert(0, str(REPO_AGENT_DIR))

from agents.behaviour_locator import BehaviorLocator
from agents.discovery import DiscoveryAgent
from agents.goal_validator import GoalValidator
from agents.planner import ChangePlanner
from repo.chunker import CodeChunker
from repo.indexer import CodeIndexer
from repo.scanner import RepoScanner

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Repo Agent Server", version="1.0.0")

# In-memory session store  { session_id: asyncio.Queue }
_sessions: dict[str, asyncio.Queue] = {}

# ── Config ───────────────────────────────────────────────────────────────────
# Override with the path you want the agent to analyse.
# Can also be set via the POST /api/run body.
DEFAULT_REPO_PATH = str(REPO_AGENT_DIR)


# ── Schemas ──────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    goal: str
    repo_path: str | None = None   # overrides DEFAULT_REPO_PATH per request


class RunResponse(BaseModel):
    ok: bool
    session_id: str
    message: str | None = None


# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Run (start pipeline) ─────────────────────────────────────────────────────
@app.post("/api/run", response_model=RunResponse)
async def run(req: RunRequest):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = queue

    repo_path = req.repo_path or DEFAULT_REPO_PATH
    loop = asyncio.get_event_loop()

    # Run the blocking pipeline in a thread so we don't block the event loop
    loop.run_in_executor(None, _run_pipeline, req.goal, repo_path, queue)

    return RunResponse(ok=True, session_id=session_id)


# ── SSE stream ───────────────────────────────────────────────────────────────
@app.get("/api/stream/{session_id}")
async def stream(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    queue = _sessions[session_id]

    async def event_generator():
        try:
            while True:
                # Wait for the next event (with a generous timeout)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    yield "event: error\ndata: {\"error\": \"pipeline timed out\"}\n\n"
                    break

                event_type = event.get("type")
                payload    = json.dumps(event.get("data", {}))

                yield f"event: {event_type}\ndata: {payload}\n\n"

                if event_type == "done":
                    break
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Pipeline (runs in thread pool) ───────────────────────────────────────────
def _run_pipeline(goal: str, repo_path: str, queue: asyncio.Queue):
    """
    Executes the full repo-agent pipeline synchronously.
    Pushes SSE events onto `queue` as work completes.
    """

    def push(event_type: str, data: dict):
        """Thread-safe enqueue."""
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, "data": data})

    try:
        # ── 1. Scan ──────────────────────────────────────────────────────
        scanner = RepoScanner(repo_path)
        files = scanner.scan_files()

        if not files:
            push("done", {"error": "No supported source files found in repo."})
            return

        # ── 2. Chunk ─────────────────────────────────────────────────────
        chunker = CodeChunker()
        all_chunks = []
        for file in files:
            full_path = Path(repo_path) / file
            all_chunks.extend(chunker.chunk_file(full_path))

        # ── 3. Index ─────────────────────────────────────────────────────
        indexer = CodeIndexer()
        if indexer.is_empty():
            indexer.add_chunks(all_chunks)
        # (reuse existing index if present — keeps the server fast)

        # ── 4. Validate goal ─────────────────────────────────────────────
        validator = GoalValidator(files)
        status = validator.validate(goal).upper()

        if "INVALID" in status:
            push("done", {
                "error": "Goal is not compatible with this repository architecture.",
                "status": status,
            })
            return

        # ── 5. Discover relevant files ───────────────────────────────────
        discovery = DiscoveryAgent(indexer, files, repo_path)
        discovered_files = discovery.discover(goal)

        if not discovered_files:
            push("done", {"error": "No relevant files found for this goal."})
            return

        # ── 6. Locate target lines ───────────────────────────────────────
        locator = BehaviorLocator(repo_path)
        hits = locator.locate(goal, discovered_files)

        # ── 7. Plan changes ──────────────────────────────────────────────
        planner = ChangePlanner(repo_path)
        patches = planner.plan(goal, hits)

        # Build a serialisable plan summary
        plan = {
            "goal": goal,
            "files_to_modify": discovered_files,
            "hit_count": len(hits),
        }

        push("plan", {"plan": plan})

        # Build serialisable results
        results = []
        for patch in patches:
            results.append({
                "warnings": patch.get("warnings", []),
                "patch": patch.get("patch", ""),
            })

        push("results", {"results": results})
        push("done", {})

    except Exception as exc:
        push("done", {"error": str(exc)})
