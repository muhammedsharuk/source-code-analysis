"""Minimal HTTP API for the reverse-engineering pipeline: trigger a run, fetch its files.

Job records, status, and history are owned by whatever calls this service
(e.g. asdlc-assistant's backend, via its own Postgres) -- this service does
not track or expose a job list or status endpoint. Live progress is
published to NATS as it happens (see nats_publisher.py), not polled from
here. See ASDLC_INTEGRATION_PLAN.md.

Run with: `uvicorn server.app:app --reload` (from the `reverse_engineer/`
directory, so the existing `agents`/`tools`/`config` absolute imports keep
resolving the same way they do for `main.py`).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from utils.git_clone import UnsafeCloneError
from utils.zip_extract import UnsafeZipError

from .events import FileNode, JobSummary
from .run_manager import run_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_manager.startup()
    try:
        yield
    finally:
        await run_manager.shutdown()


app = FastAPI(title="Reverse Engineer Trigger API", lifespan=lifespan)

# Local dev only: the Vite frontend runs on a different origin/port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/jobs", response_model=JobSummary)
async def start_job(
    name: str = Form(...),
    job_id: str | None = Form(None),
    file: UploadFile | None = File(None),
    repo_url: str | None = Form(None),
    git_username: str | None = Form(None),
    git_token: str | None = Form(None),
) -> JobSummary:
    """Trigger the pipeline: from an uploaded zip, or a repo URL to clone,
    and runs it in the background.

    `job_id` is optional -- a caller that needs to control the id itself
    (e.g. asdlc-assistant's backend, which already generated a Postgres row
    for this job before calling here) passes one, so this service's
    internal run id matches the caller's own record; omit it to have one
    generated here for direct/manual use of this API.

    Exactly one of `file` / `repo_url` is required. `git_username`/`git_token`
    are only meaningful with `repo_url`, for a private repository -- see
    `utils/git_clone.py` for how they're used and discarded.
    """
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required.")
    if bool(file) == bool(repo_url):
        raise HTTPException(status_code=400, detail="Provide exactly one of a zip file upload or a repo_url.")
    if file is not None and (not file.filename or not file.filename.lower().endswith(".zip")):
        raise HTTPException(status_code=400, detail="Only .zip uploads are supported.")
    try:
        record = await run_manager.start_job(
            job_id, name.strip(), file, repo_url=repo_url, git_username=git_username, git_token=git_token
        )
    except (ValueError, UnsafeZipError, UnsafeCloneError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JobSummary(**record)


@app.get("/jobs/{job_id}/files", response_model=list[FileNode])
async def get_job_files(job_id: str) -> list[FileNode]:
    output_dir = await run_manager.output_dir_for(job_id)
    if output_dir is None or not output_dir.is_dir():
        return []
    children = [FileNode(name=p.name, path=p.name, type="file") for p in sorted(output_dir.glob("*.md"))]
    if not children:
        return []
    return [FileNode(name="output", path="output", type="folder", children=children)]


@app.get("/jobs/{job_id}/graph")
async def get_job_graph(job_id: str) -> dict:
    """The call graph captured for this job -- see `RunManager._snapshot_graph`.

    Only present once the run has completed: the snapshot is written at
    the same point `output/<project>/*.md` is, and a run that's still in
    progress, failed, or never produced one has no `graph.json` to read.
    """
    output_dir = await run_manager.output_dir_for(job_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail="No output yet for this job.")
    graph_path = output_dir / "graph.json"
    if not graph_path.is_file():
        raise HTTPException(status_code=404, detail="No call graph was captured for this job.")
    return json.loads(graph_path.read_text(encoding="utf-8"))


@app.get("/jobs/{job_id}/files/content")
async def get_job_file_content(job_id: str, path: str = Query(...)) -> PlainTextResponse:
    output_dir = await run_manager.output_dir_for(job_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail="No output yet for this job.")

    # path is a bare filename ("TASKS.md"), possibly prefixed with the
    # "output/" folder segment used in the tree above — strip that segment
    # rather than trusting any caller-supplied path as a filesystem path.
    filename = Path(path).name
    file_path = output_dir / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"No such file: {filename}")
    # A bare `-> str` return here would have FastAPI JSON-encode the string
    # (wrapping it in quotes, escaping newlines as literal `\n`) instead of
    # sending it as real text -- explicit PlainTextResponse avoids that.
    return PlainTextResponse(file_path.read_text(encoding="utf-8"))
