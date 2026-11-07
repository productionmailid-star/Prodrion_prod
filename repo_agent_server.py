"""
repo_agent_server.py
--------------------
A thin FastAPI wrapper around the repo-agent pipeline, for manual/interactive
use (dev tooling). The production path — worker.py's "analyze_repo" job,
triggered by the dashboard's "Generate fix" button — calls
repo_agent_core.analyze() directly and does not go through this server.

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
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ── Make the repo root importable ──────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent
REPO_AGENT_DIR = REPO_ROOT / "repo-agent"
sys.path.insert(0, str(REPO_ROOT))

import repo_agent_core
from config import settings
from security import require_bearer_token

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Repo Agent Server", version="1.0.0")

# In-memory session store  { session_id: (asyncio.Queue, created_at_epoch) }.
# A run that's never streamed (client starts /api/run then never opens
# /api/stream/{id}) would otherwise leak here forever; _sweep_stale_sessions
# evicts anything older than the stream's own timeout.
_sessions: dict[str, tuple[asyncio.Queue, float]] = {}
_SESSION_MAX_AGE_SECONDS = 300
_SESSION_SWEEP_INTERVAL_SECONDS = 60


async def _sweep_stale_sessions() -> None:
    while True:
        await asyncio.sleep(_SESSION_SWEEP_INTERVAL_SECONDS)
        cutoff = time.monotonic() - _SESSION_MAX_AGE_SECONDS
        stale = [sid for sid, (_, created_at) in _sessions.items() if created_at < cutoff]
        for sid in stale:
            _sessions.pop(sid, None)


@app.on_event("startup")
async def _start_session_sweeper() -> None:
    app.state.sweeper_task = asyncio.create_task(_sweep_stale_sessions())

# ── Config ───────────────────────────────────────────────────────────────────
DEFAULT_REPO_PATH = str(REPO_AGENT_DIR)


def _resolve_allowed_repo_path(requested: str | None) -> str:
    """Confine the analysed path to a directory inside the allowed base.

    Prevents a caller from pointing the scanner/indexer at arbitrary
    directories on the host.
    """
    base = Path(settings.repo_agent_allowed_base or DEFAULT_REPO_PATH).resolve()
    target = Path(requested).resolve() if requested else base
    if target != base and base not in target.parents:
        raise HTTPException(
            status_code=400,
            detail=f"repo_path must be inside {base}",
        )
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="repo_path is not a directory")
    return str(target)


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
@app.post("/api/run", response_model=RunResponse, dependencies=[Depends(require_bearer_token)])
async def run(req: RunRequest):
    repo_path = _resolve_allowed_repo_path(req.repo_path)

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = (queue, time.monotonic())

    loop = asyncio.get_event_loop()

    # Run the blocking pipeline in a thread so we don't block the event loop
    loop.run_in_executor(None, _run_pipeline, req.goal, repo_path, queue)

    return RunResponse(ok=True, session_id=session_id)


# ── SSE stream ───────────────────────────────────────────────────────────────
@app.get("/api/stream/{session_id}", dependencies=[Depends(require_bearer_token)])
async def stream(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    queue, _ = _sessions[session_id]

    async def event_generator():
        try:
            while True:
                # Wait for the next event (with a generous timeout)
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_SESSION_MAX_AGE_SECONDS)
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
    """Run the repo-agent pipeline and stream SSE events onto `queue`."""

    def push(event_type: str, data: dict):
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, "data": data})

    try:
        result = repo_agent_core.analyze(repo_path, goal)
        if result["status"] != "ok":
            push("done", {"error": result.get("error", result["status"])})
            return
        push("plan", {"plan": result["plan"]})
        push("results", {"results": result["results"]})
        push("done", {})
    except Exception as exc:
        push("done", {"error": str(exc)})
