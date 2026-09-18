"""Owns the lifecycle of every pipeline job: starting, tracking, stopping.

A job's actual agent execution runs in a background `asyncio.Task`,
independent of any HTTP request -- a caller starts a job, gets a `job_id`
back immediately, and everything about the job's progress from then on is
published live to NATS (see `nats_publisher.py`), not polled from this
service. This service now only answers two questions over HTTP: "run this"
(`POST /jobs`) and "give me the files it produced" (`GET
/jobs/{job_id}/files*`) -- job records, status, and history live in
whatever owns the caller side of this integration (see
ASDLC_INTEGRATION_PLAN.md), not here.

There is deliberately no persistence of a run's *in-flight* state (status,
progress, steps) across a server restart (see `run_store.py`'s docstring for
why — a prior version had this and it caused duplicate concurrent runs in
practice, worse than the problem it solved). That means restarting the
server abandons any run in flight; there's nothing to resume, and nothing
silently restarts on its own.

A run's *finished* state is a different matter — once a run reaches
`"completed"`, it's a closed fact, not something that could ever look like
an orphan on restart. `HistoryStore` records exactly that (see its
docstring), so a completed analysis and its generated documents stay
browsable after a restart even though nothing about how it got there does.

Stopping a run: `stop_run()` cancels its `asyncio.Task`. `_execute_run`
catches `asyncio.CancelledError` specifically (it is not an `Exception`
subclass, so the generic handler below it would not catch it) to record a
clean `"stopped"` status and terminal event instead of leaving the run's
last-known status stuck at `"running"` forever.
"""

import asyncio
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from agents.orchestrator import create_orchestrator_agent

from . import job_db, nats_publisher
from .event_handler import EventHandler
from .history_store import HistoryStore
from .run_store import RunStore
from utils.codebase_memory import CodebaseMemoryCLI
from utils.git_clone import UnsafeCloneError, safe_clone_repo
from utils.naming import safe_directory_name
from utils.zip_extract import UnsafeZipError, safe_extract_zip

_REVERSE_ENGINEER_ROOT = Path(__file__).resolve().parent.parent
UPLOADS_ROOT = _REVERSE_ENGINEER_ROOT / "uploads"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # keep in step with zip_extract.MAX_UNCOMPRESSED_BYTES
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Bounds for `_snapshot_graph`'s codebase-memory calls, which run against an index that's
# just been used (and may still be held open) by the orchestrator's own codebase-memory-mcp
# session -- generous enough for a metadata/Cypher query plus retries under lock contention,
# but never unbounded, so a stuck subprocess can't hang the run's "completed" event forever.
_CALL_TIMEOUT_SECONDS = 45.0
_GRAPH_TIMEOUT_SECONDS = 200.0  # covers all 3 calls above sequentially, plus buffer


def _derive_name(repo_path: str) -> str:
    cleaned = repo_path.rstrip("/\\")
    return cleaned.replace("\\", "/").split("/")[-1] or "repository"


def _log_line(level: str, message: str) -> dict[str, Any]:
    """Same shape `EventHandler._emit` publishes -- used here directly for the
    clone step, which happens before an `EventHandler` for this run exists."""
    return {"id": uuid.uuid4().hex, "timestamp": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}


class RunManager:
    def __init__(self) -> None:
        self._store = RunStore()
        self._history = HistoryStore()
        self._tasks: dict[str, asyncio.Task] = {}

    # --- lifecycle --------------------------------------------------------

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await nats_publisher.close()

    # --- starting/observing/stopping jobs -----------------------------------

    async def start_job(
        self,
        job_id: str | None,
        name: str,
        file: UploadFile | None = None,
        *,
        repo_url: str | None = None,
        git_username: str | None = None,
        git_token: str | None = None,
    ) -> dict[str, Any]:
        """Start a job from either an uploaded repo zip or a repo URL to clone.

        Exactly one of `file` / `repo_url` must be given -- callers validate
        this at the HTTP layer too (see `app.py`), but it's enforced here
        again since this is also the direct/manual entry point.

        `job_id` is caller-supplied when the caller needs to control it (e.g.
        asdlc-assistant's backend, which addresses this the same way it
        addresses every other agent service's sessions) -- generated here
        when omitted, e.g. for direct/manual use of this API.

        Everything this method creates is this job's own private copy. It is
        deleted in `_execute_run`'s cleanup regardless of how the run ends,
        per the "treat every upload as one-off" retention decision — see
        `docker-compose.yml`'s absence of an uploads volume, which is
        deliberate: nothing here is meant to survive past its own run.

        A zip's bytes are consumed right here, before returning -- it's a
        local disk write, fast, and the `UploadFile` needs to be read while
        this request is still open. A repo clone is different: it's a
        network call that can take minutes, so it's deferred into the
        background task itself (`_execute_run`) instead of blocking this
        response -- the caller gets `job_id` back immediately either way,
        matching every other job's "trigger it, watch progress over NATS"
        shape.
        """
        if bool(file) == bool(repo_url):
            raise ValueError("Provide exactly one of a zip file upload or a repo_url.")

        run_id = job_id or uuid.uuid4().hex
        run_dir = UPLOADS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        extracted_dir = run_dir / "source"
        clone: dict[str, str | None] | None = None

        if file is not None:
            zip_path = run_dir / "upload.zip"
            try:
                size = 0
                with zip_path.open("wb") as out:
                    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit.")
                        out.write(chunk)
                safe_extract_zip(zip_path, extracted_dir)
            except (ValueError, UnsafeZipError):
                shutil.rmtree(run_dir, ignore_errors=True)
                raise
            finally:
                # The compressed copy is never needed again once extraction has
                # either succeeded or failed -- only the extracted tree (or
                # nothing, on failure) needs to survive this method.
                zip_path.unlink(missing_ok=True)
        else:
            clone = {"repo_url": repo_url, "username": git_username, "token": git_token}

        repo_path = str(extracted_dir)
        record = await self._store.create(run_id, repo_path, name)
        self._tasks[run_id] = asyncio.create_task(self._execute_run(run_id, repo_path, ephemeral=True, clone=clone))
        return {"job_id": run_id, "name": name, "status": "active"}

    def stop_run(self, run_id: str) -> bool:
        """Cancel a run's background task. Returns False if it wasn't running."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def output_dir_for(self, run_id: str) -> Path | None:
        """Resolve the project's persisted-output directory.

        While the run is still active in this process, `project_name` comes
        from the in-memory progress snapshot (populated once
        `index_repository`/`index_status` resolves it — see
        `EventHandler._update_progress_for_tool`), falling back to the run
        registry's own copy of the same field (set by the same call, via
        `set_project_name`) in case the two ever drift. Once the run has
        completed, `HistoryStore` has the resolved directory recorded
        directly, which is what makes a completed run's files browsable even
        after a restart.
        """
        progress = self._store.load_progress(run_id) or {}
        project_name = progress.get("project_name")
        if not project_name:
            record = await self._store.get(run_id)
            project_name = (record or {}).get("project_name")
        if project_name:
            from config import output_dir
            from utils.naming import safe_directory_name

            return output_dir / safe_directory_name(project_name, default="unnamed-project")

        completed = await self._history.get(run_id)
        if completed is None or not completed.get("output_dir"):
            return None
        return Path(completed["output_dir"])

    # --- publish surface used by EventHandler ------------------------------
    #
    # Live progress goes to NATS (a no-op if NATS_URL isn't configured, see
    # nats_publisher.py), not to any in-process store -- nothing left in
    # this service reads it back over HTTP.

    async def publish(self, run_id: str, logline: dict[str, Any]) -> None:
        await nats_publisher.publish_event(run_id, "log", logline)

    async def publish_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        await nats_publisher.publish_event(run_id, "step_progress", {"steps": steps})

    async def publish_terminal(self, run_id: str, status: str, message: str) -> None:
        await nats_publisher.publish_event(run_id, "end", {"status": status, "message": message})

    async def set_project_name(self, run_id: str, project_name: str) -> None:
        await self._store.update(run_id, project_name=project_name)

    async def save_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        self._store.save_progress(run_id, progress)

    # --- execution ----------------------------------------------------------

    async def _execute_run(
        self, run_id: str, repo_path: str, ephemeral: bool = False, clone: dict[str, str | None] | None = None
    ) -> None:
        """Run one pipeline end-to-end.

        `ephemeral=True` (uploads only, see `start_job`) means this
        run's extracted source, workspace temp dir, and knowledge-graph index
        are this run's own private, disposable copies -- all three are torn
        down in `finally` regardless of how the run ends. `ephemeral=False`
        (the existing path-based flow) leaves all three exactly as before:
        the caller supplied that path and may still need it, or may want to
        re-analyze it later with the graph index intact for a fast
        incremental re-index.

        `clone`, when given (repo-URL jobs only -- see `start_job`), means
        `repo_path` doesn't exist on disk yet: this method's first job is to
        clone it there before anything else can run. A clone failure is
        handled the same as any other run failure below, just with a
        clearer message than a generic exception would give.
        """
        handler = EventHandler(run_id, self)

        try:
            if clone is not None:
                await self.publish(run_id, _log_line("INFO", f"Cloning {clone['repo_url']}..."))
                try:
                    await asyncio.to_thread(
                        safe_clone_repo,
                        clone["repo_url"],
                        Path(repo_path),
                        username=clone.get("username"),
                        token=clone.get("token"),
                    )
                except UnsafeCloneError as exc:
                    await self._store.update(run_id, status="failed", error=str(exc))
                    await handler.emit_terminal("failed", f"Could not clone repository: {exc}")
                    await job_db.mark_job_complete(run_id, "failed")
                    return
                await self.publish(run_id, _log_line("SUCCESS", "Repository cloned."))

            orchestrator = create_orchestrator_agent(repo_path)
            # LangGraph's default recursion_limit (25 *graph steps*, not tool
            # calls or batches) is nowhere near enough here — this pipeline
            # loops once per index batch and once per task batch, each
            # iteration costing multiple graph steps, easily hundreds on a
            # real repo. Confirmed against a real run: the default 25 was
            # hit and aborted the pipeline with a GraphRecursionError before
            # even finishing Stage 2. main.py's plain .invoke() call has this
            # same default-limit exposure; it just surfaces here first
            # because the server is what actually gets run against a
            # multi-batch repo end-to-end.
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}
            request = (
                f"Index the repository at path '{repo_path}' and run the full "
                "reverse-engineering pipeline end-to-end, following your "
                "instructions exactly, until every stage completes or a "
                "genuine blocking failure occurs."
            )
            input_ = {"messages": [{"role": "user", "content": request}]}

            async for event in orchestrator.astream_events(input_, version="v2", config=config):
                await handler.handle_event(event)

            await self._store.update(run_id, status="completed")
            await self._record_completed(run_id, repo_path, handler)
            await handler.emit_terminal("completed", "Pipeline run completed.")
            await job_db.mark_job_complete(run_id, "completed")
        except asyncio.CancelledError:
            await self._store.update(run_id, status="stopped")
            await handler.emit_terminal("stopped", "Pipeline run stopped.")
            await job_db.mark_job_complete(run_id, "stopped")
            raise
        except Exception as exc:
            await self._store.update(run_id, status="failed", error=str(exc))
            await handler.emit_terminal("failed", f"Pipeline run failed: {exc}")
            await job_db.mark_job_complete(run_id, "failed")
        finally:
            self._tasks.pop(run_id, None)
            if ephemeral:
                await self._cleanup_ephemeral_run(run_id, repo_path)

    async def _cleanup_ephemeral_run(self, run_id: str, repo_path: str) -> None:
        """Delete an uploaded run's private copies: extracted source, workspace
        temp dir, and knowledge-graph index -- run regardless of whether the
        pipeline completed, failed, or was stopped, so nothing from a one-off
        upload lingers. `output/<project>/*.md` is deliberately untouched:
        that is the actual deliverable.
        """
        record = await self._store.get(run_id)
        project_name = (record or {}).get("project_name")

        def _cleanup() -> None:
            shutil.rmtree(UPLOADS_ROOT / run_id, ignore_errors=True)
            workspace_dir = _REVERSE_ENGINEER_ROOT / "temp" / safe_directory_name(repo_path, default="unnamed-repo")
            shutil.rmtree(workspace_dir, ignore_errors=True)
            if project_name:
                try:
                    # Bounded, same as `_snapshot_graph`'s calls just above -- without
                    # this, a lock still held by the orchestrator's own just-finished
                    # codebase-memory-mcp session makes `subprocess.run` block forever
                    # (see `CodebaseMemoryCLI.delete_project`), silently hanging this
                    # whole background task and leaving the job stuck at "active"
                    # forever, since nothing after this line ever runs.
                    CodebaseMemoryCLI().delete_project(project=project_name, timeout_seconds=_CALL_TIMEOUT_SECONDS)
                except Exception:
                    # Best-effort: a stray/partial knowledge-graph index left
                    # behind is a disk-space nuisance, never a reason to mask
                    # this run's own real outcome (already recorded above).
                    pass

        await asyncio.to_thread(_cleanup)

    async def _record_completed(self, run_id: str, repo_path: str, handler: EventHandler) -> None:
        """Write this run's one-time, permanent history record.

        Called exactly once, only from the success branch above — never on
        `"stopped"` or `"failed"`, and never updated afterwards. See
        `HistoryStore`'s docstring for why that single-write shape is what
        keeps this safe to reload after a restart.
        """
        record = await self._store.get(run_id)
        output_dir = await self.output_dir_for(run_id)
        if output_dir is None:
            return
        await self._history.record_completed(
            {
                "run_id": run_id,
                "name": (record or {}).get("name") or _derive_name(repo_path),
                "repo_path": repo_path,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "output_dir": str(output_dir),
            }
        )

        # Capture the call graph now, while this run's codebase-memory index
        # still exists -- `_cleanup_ephemeral_run` deletes that index
        # unconditionally once `_execute_run`'s `finally` block runs right
        # after this method returns, so `graph.json` is the only place a
        # call graph survives past this run. Best-effort in every sense: a
        # failure or a timeout here never affects the run's own
        # already-recorded outcome, and `_execute_run`'s "completed" event
        # (see `_GRAPH_TIMEOUT_SECONDS` above) always still fires -- without
        # that bound, a hang here (e.g. the orchestrator's own
        # codebase-memory-mcp session still holding the index open, causing
        # the fresh CLI calls below to contend for it) would silently swallow
        # the run's completion instead of just this one step.
        project_name = (record or {}).get("project_name")
        if project_name:
            await handler.set_step("graph", "active", 0)
            await self.publish(run_id, _log_line("INFO", "Capturing the call graph for the Graph view..."))
            try:
                counts = await asyncio.wait_for(self._snapshot_graph(output_dir, project_name), timeout=_GRAPH_TIMEOUT_SECONDS)
                await self.publish(
                    run_id,
                    _log_line(
                        "SUCCESS",
                        f"Call graph ready — {counts['functions']} functions, {counts['calls']} calls across "
                        f"{counts['packages']} packages.",
                    ),
                )
            except asyncio.TimeoutError:
                await self.publish(
                    run_id,
                    _log_line("WARNING", f"Call graph capture timed out after {_GRAPH_TIMEOUT_SECONDS}s — the Graph view will be empty for this run."),
                )
            except Exception as exc:
                await self.publish(run_id, _log_line("WARNING", f"Could not capture call graph: {exc}"))
            # Always complete the step, success or not -- this is the run's very last
            # step, so leaving it "active" on a failure would leave the progress bar
            # stuck forever instead of reflecting that the run itself is done.
            await handler.set_step("graph", "complete", 100)

    async def _snapshot_graph(self, output_dir: Path, project_name: str) -> dict[str, int]:
        """Write `graph.json` and return its node/edge counts for the caller to log.

        Package-level overview from `get_architecture` (already deduped and
        weighted by that tool) plus a function-level call graph from
        `query_graph` (the only tool that returns real `(source, target)`
        edge pairs rather than per-node degree counts or hop-distance
        traces).

        Node ids are `qualified_name`, not the bare `name` -- this indexer
        reuses bare names across files/components routinely (two
        `isValidEmail`s, two `validateForm`s, etc. are common even in a small
        repo), so `name` is only safe to use as a display label.

        The `MATCH (a:Function)-[:CALLS]->(b:Function)` shape only matches
        edges where both ends already resolved to a `Function` node, which is
        what keeps unresolved call targets out of the snapshot for free --
        no separate filtering step needed.

        Each call below gets its own `timeout_seconds` bound -- see
        `CodebaseMemoryCLI._run` -- so a stuck subprocess can't hang this
        method forever; `asyncio.wait_for` at the call site above is the
        second, outer bound covering the whole method plus thread scheduling.
        """

        def _build() -> dict[str, Any]:
            cli = CodebaseMemoryCLI()
            architecture = cli.unwrap(cli.get_architecture(project=project_name, aspects=["all"], timeout_seconds=_CALL_TIMEOUT_SECONDS)) or {}
            node_rows = cli.unwrap(
                cli.query_graph(
                    project=project_name,
                    query=(
                        "MATCH (f:Function) RETURN f.qualified_name AS id, f.name AS name, "
                        "f.file_path AS file_path, f.start_line AS start_line, f.end_line AS end_line, "
                        "f.is_entry_point AS is_entry_point"
                    ),
                    timeout_seconds=_CALL_TIMEOUT_SECONDS,
                )
            ) or {}
            edge_rows = cli.unwrap(
                cli.query_graph(
                    project=project_name,
                    query=(
                        "MATCH (a:Function)-[r:CALLS]->(b:Function) RETURN DISTINCT "
                        "a.qualified_name AS source, b.qualified_name AS target, count(r) AS weight"
                    ),
                    timeout_seconds=_CALL_TIMEOUT_SECONDS,
                )
            ) or {}

            def _rows_to_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
                columns = result.get("columns") or []
                return [dict(zip(columns, row)) for row in result.get("rows") or []]

            # `query_graph` renders every property as a string (e.g. `"start_line": "11"`,
            # `"is_entry_point": "false"`) -- coerce back to the real types here, once, so
            # nothing downstream has to re-parse them (and never `bool("false")`, which is
            # `True`: it's a non-empty string).
            function_nodes = _rows_to_dicts(node_rows)
            for node in function_nodes:
                node["start_line"] = int(node["start_line"])
                node["end_line"] = int(node["end_line"])
                node["is_entry_point"] = node["is_entry_point"] == "true"

            function_edges = _rows_to_dicts(edge_rows)
            for edge in function_edges:
                edge["weight"] = int(edge["weight"])

            # `architecture["packages"]` and `architecture["boundaries"]` aren't always the
            # same namespace. Confirmed against a real small FastAPI project: `packages`
            # held only its 5 *external* pip dependencies (bcrypt, fastapi, ...), each with
            # fan_in=fan_out=0, while `boundaries` used its own internal module names
            # (main, crud, database, security, models) as endpoints -- names that never
            # appear in `packages` at all. Building nodes from `packages` alone and edges
            # from `boundaries` alone (the original approach) left every boundary edge
            # pointing at a node id that didn't exist, so dagre's `g.hasNode(...)` guard
            # silently dropped all of them client-side: 5 disconnected package boxes, 0
            # visible calls, even though the underlying function-level graph was correct.
            # Union both name sets so every boundary edge always has a matching node,
            # regardless of which naming scheme a given project's `packages` happens to
            # use; `layers` (same namespace as `boundaries`) fills in a node's `layer`
            # classification when `packages` didn't cover it.
            package_by_name = {p["name"]: p for p in architecture.get("packages", []) if p.get("name")}
            layer_by_name = {l["name"]: l for l in architecture.get("layers", []) if l.get("name")}
            boundaries = [b for b in architecture.get("boundaries", []) if b.get("from") and b.get("to")]
            boundary_names = {b["from"] for b in boundaries} | {b["to"] for b in boundaries}

            package_nodes = [
                {
                    "id": name,
                    "name": name,
                    "node_count": package_by_name.get(name, {}).get("node_count", 0),
                    "fan_in": package_by_name.get(name, {}).get("fan_in", 0),
                    "fan_out": package_by_name.get(name, {}).get("fan_out", 0),
                    "layer": layer_by_name.get(name, {}).get("layer"),
                }
                for name in package_by_name.keys() | boundary_names
            ]

            return {
                "project": project_name,
                "packages": {
                    "nodes": package_nodes,
                    "edges": [{"source": b["from"], "target": b["to"], "weight": b["call_count"]} for b in boundaries],
                },
                "functions": {
                    "nodes": function_nodes,
                    "edges": function_edges,
                },
            }

        graph = await asyncio.to_thread(_build)

        def _write() -> None:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

        await asyncio.to_thread(_write)
        return {
            "functions": len(graph["functions"]["nodes"]),
            "calls": len(graph["functions"]["edges"]),
            "packages": len(graph["packages"]["nodes"]),
        }


run_manager = RunManager()
