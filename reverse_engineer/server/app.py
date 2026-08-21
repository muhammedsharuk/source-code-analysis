"""FastAPI server the frontend talks to instead of `mockApi.ts`'s simulated bodies.

Run with: `uvicorn server.app:app --reload` (from the `reverse_engineer/`
directory, so the existing `agents`/`tools`/`config` absolute imports keep
resolving the same way they do for `main.py`).

Endpoint shapes mirror `frontend/src/lib/mockApi.ts` + `types.ts` field-for-
field, so swapping the frontend over should only mean replacing each mock
function's body with a real `fetch`/`EventSource` call — no component
changes.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .events import AnalysisStep, FileNode, ProjectSummary
from .run_manager import run_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_manager.startup()
    try:
        yield
    finally:
        await run_manager.shutdown()


app = FastAPI(title="Deep Index Server", lifespan=lifespan)

# Local dev only: the Vite frontend runs on a different origin/port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartAnalysisRequest(BaseModel):
    repoPath: str


@app.get("/projects", response_model=list[ProjectSummary])
async def list_projects() -> list[ProjectSummary]:
    """Every analysis for the home dashboard: active, failed, and completed.

    See `RunManager.list_projects` for how in-process runs (real-time
    status) and `HistoryStore`'s completed-only record (survives a restart)
    are merged into one list.
    """
    return [ProjectSummary(**p) for p in await run_manager.list_projects()]


@app.post("/projects", response_model=ProjectSummary)
async def start_analysis(body: StartAnalysisRequest) -> ProjectSummary:
    if not body.repoPath.strip():
        raise HTTPException(status_code=400, detail="repoPath is required.")
    record = await run_manager.start_run(body.repoPath.strip())
    return ProjectSummary(id=record["run_id"], name=record["name"], repoUrl=record["repo_path"], status="active")


@app.post("/projects/{run_id}/stop")
async def stop_analysis(run_id: str) -> dict:
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")
    stopped = run_manager.stop_run(run_id)
    if not stopped:
        raise HTTPException(status_code=409, detail="This run is not currently active.")
    return {"status": "stopping"}


@app.get("/projects/{run_id}", response_model=ProjectSummary)
async def get_project(run_id: str) -> ProjectSummary:
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")
    return ProjectSummary(**project)


@app.get("/projects/{run_id}/steps", response_model=list[AnalysisStep])
async def get_steps(run_id: str) -> list[AnalysisStep]:
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")
    return [AnalysisStep(**s) for s in run_manager.get_steps(run_id)]


@app.get("/projects/{run_id}/logs/stream")
async def stream_logs(run_id: str):
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")

    async def event_source():
        # Subscribe before replaying history, so nothing published between
        # "read history" and "start listening live" is missed — a possible
        # duplicate at the boundary is a much smaller problem for a log
        # viewer than a gap would be.
        queue = run_manager.subscribe(run_id)
        try:
            for envelope in run_manager.replay(run_id):
                yield _format_sse(envelope)
            while True:
                envelope = await queue.get()
                yield _format_sse(envelope)
                if envelope.get("kind") == "terminal":
                    break
        except asyncio.CancelledError:
            raise
        finally:
            run_manager.unsubscribe(run_id, queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")


def _format_sse(envelope: dict) -> str:
    kind = envelope.get("kind", "log")
    payload = envelope.get("payload")
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


@app.get("/projects/{run_id}/files", response_model=list[FileNode])
async def get_result_files(run_id: str) -> list[FileNode]:
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")
    output_dir = await run_manager.output_dir_for(run_id)
    if output_dir is None or not output_dir.is_dir():
        return []
    children = [
        FileNode(name=p.name, path=p.name, type="file")
        for p in sorted(output_dir.glob("*.md"))
    ]
    if not children:
        return []
    return [FileNode(name="output", path="output", type="folder", children=children)]


@app.get("/projects/{run_id}/files/content")
async def get_file_content(run_id: str, path: str = Query(...)) -> str:
    project = await run_manager.get_project(run_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Unknown project id.")
    output_dir = await run_manager.output_dir_for(run_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail="No output yet for this project.")

    # path is a bare filename ("TASKS.md"), possibly prefixed with the
    # "output/" folder segment used in the tree above — strip that segment
    # rather than trusting any caller-supplied path as a filesystem path.
    filename = Path(path).name
    file_path = output_dir / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"No such file: {filename}")
    return file_path.read_text(encoding="utf-8")
