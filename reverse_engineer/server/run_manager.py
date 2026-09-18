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
from utils import layout3d
from utils.codebase_memory import CodebaseMemoryCLI
from utils.git_clone import UnsafeCloneError, safe_clone_repo
from utils.naming import safe_directory_name
from utils.zip_extract import UnsafeZipError, safe_extract_zip

_REVERSE_ENGINEER_ROOT = Path(__file__).resolve().parent.parent
UPLOADS_ROOT = _REVERSE_ENGINEER_ROOT / "uploads"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # keep in step with zip_extract.MAX_UNCOMPRESSED_BYTES
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Bounds for `compute_graph`'s codebase-memory calls. Any one of these can run
# concurrently with some other job's own still-open codebase-memory-mcp session against
# a different project -- generous enough for a metadata/Cypher query plus retries under
# lock contention, but never unbounded, so a stuck subprocess can't hang the HTTP request
# that's waiting on it forever.
_CALL_TIMEOUT_SECONDS = 45.0
# `compute_graph` runs one query for nodes plus one per schema edge type (see its own
# docstring for why), plus a Barnes-Hut physics simulation over the result in pure Python --
# generous enough to cover a real repo's worth of edge types and iterations, but still a
# ceiling, not a target latency.
_GRAPH_TIMEOUT_SECONDS = 600.0

# Cap per `query_graph` call. codebase-memory-mcp 0.8.1 has no working pagination at all
# (`offset` is silently ignored, there's no `has_more` to detect truncation -- confirmed
# directly), so this is a single-shot ceiling, not a page size: a node or edge-type query
# matching more than this many rows is silently truncated on 0.8.1, with no way to fetch the
# rest. 0.11+ does support real pagination, but `compute_graph` doesn't use it, to keep one
# query shape that works on both.
_MAX_QUERY_ROWS = 20000

# Matches codebase-memory-mcp's own layout3d.c DEFAULT_MAX_NODES: a caller-visible cap, not
# just a default -- `compute_graph` doesn't yet accept a per-request override the way
# upstream's `max_nodes` query param does.
_MAX_GRAPH_NODES = 5000
_FUNCTION_LABELS = {"Function", "Method"}
_CALL_EDGE_TYPES = {"CALLS"}
_USAGE_EDGE_TYPES = {"USAGE", "CALL_REFERENCE"}


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

    async def project_name_for(self, run_id: str) -> str | None:
        """Resolve a run's codebase-memory project name -- same precedence as
        `output_dir_for` above, but returning the name itself rather than the
        directory derived from it, for `compute_graph` to query directly.

        The `HistoryStore` fallback needs `project_name` to have actually been
        persisted onto the completed record (see `_record_completed`) -- it's
        not recoverable from `output_dir` alone once this process's in-memory
        progress/registry state is gone (e.g. after a restart).
        """
        progress = self._store.load_progress(run_id) or {}
        project_name = progress.get("project_name")
        if project_name:
            return project_name
        record = await self._store.get(run_id)
        project_name = (record or {}).get("project_name")
        if project_name:
            return project_name
        completed = await self._history.get(run_id)
        return (completed or {}).get("project_name")

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

        `ephemeral=True` (uploads only, see `start_job`) means this run's
        extracted source and workspace temp dir are this run's own private,
        disposable copies -- both are torn down in `finally` regardless of
        how the run ends. The knowledge-graph index is deliberately NOT one
        of those two anymore -- see `_cleanup_ephemeral_run` for why -- so
        `ephemeral=False` (the existing path-based flow) now differs from
        `ephemeral=True` only in whether the extracted-source/workspace copy
        was this run's own to delete in the first place.

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
        """Delete an uploaded run's private, one-off copies: the extracted source and
        the workspace temp dir. Runs regardless of whether the pipeline completed,
        failed, or was stopped, so nothing from a one-off upload lingers.
        `output/<project>/*.md` is deliberately untouched: that is the actual
        deliverable.

        The knowledge-graph index used to be deleted here too. It no longer is:
        `compute_graph` (see `app.py`'s `/jobs/{job_id}/graph`) queries that same
        index live, on demand, the same way codebase-memory-mcp's own graph-ui does
        against its persistent per-project store (`GET /api/layout` in that
        project's `src/ui/http_server.c` -- it never snapshots either, and never
        deletes a project's index except via an explicit `delete_project` call, same
        as here now). Deleting it right after the run would make every subsequent
        graph request 404 forever, since there would be nothing left to query.

        The tradeoff this accepts: every uploaded repo's index now persists on disk
        indefinitely (tens to low-hundreds of MB per project, per `list_projects`),
        not just for this run's lifetime. Nothing here reclaims that space -- an
        explicit `delete_project` call is the only way to, same as upstream.
        """

        def _cleanup() -> None:
            shutil.rmtree(UPLOADS_ROOT / run_id, ignore_errors=True)
            workspace_dir = _REVERSE_ENGINEER_ROOT / "temp" / safe_directory_name(repo_path, default="unnamed-repo")
            shutil.rmtree(workspace_dir, ignore_errors=True)

        await asyncio.to_thread(_cleanup)

    async def _record_completed(self, run_id: str, repo_path: str, handler: EventHandler) -> None:
        """Write this run's one-time, permanent history record, then capture the call
        graph once while the run's codebase-memory index is guaranteed fresh.

        The history write is called exactly once, only from the success branch above --
        never on `"stopped"` or `"failed"`, and never updated afterwards. See
        `HistoryStore`'s docstring for why that single-write shape is what keeps this
        safe to reload after a restart.

        `project_name` is persisted here too (not just derived into `output_dir`) so
        `project_name_for` can still resolve it -- and so `compute_graph` can still be
        called live as a fallback (see `app.py`) -- after this process restarts and
        loses the in-memory `RunStore` state that normally carries it.

        The graph capture itself is best-effort: `compute_graph`'s physics simulation
        isn't free (real repos can take real seconds), so this is bounded by
        `_GRAPH_TIMEOUT_SECONDS` and never affects the run's own already-recorded
        "completed" outcome, win or lose -- a failure or timeout here just means
        `/jobs/{job_id}/graph` falls back to computing it live on first request instead
        of reading a cached file.
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
                "project_name": (record or {}).get("project_name"),
            }
        )

        project_name = (record or {}).get("project_name")
        if not project_name:
            return
        await handler.set_step("graph", "active", 0)
        await self.publish(run_id, _log_line("INFO", "Laying out the call graph for the Graph view..."))
        try:
            graph = await self.compute_graph(project_name)

            def _write() -> None:
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

            await asyncio.to_thread(_write)
            await self.publish(
                run_id,
                _log_line("SUCCESS", f"Call graph ready — {len(graph['nodes'])} nodes, {len(graph['edges'])} edges."),
            )
        except Exception as exc:
            await self.publish(run_id, _log_line("WARNING", f"Could not capture the call graph ahead of time: {exc}"))
        # Always complete the step, success or not -- this is the run's very last step,
        # so leaving it "active" on a failure would leave the progress bar stuck
        # instead of reflecting that the run itself is done.
        await handler.set_step("graph", "complete", 100)

    async def compute_graph(self, project_name: str) -> dict[str, Any]:
        """Query the call graph for a project and lay it out in real 3D using a Python
        port of codebase-memory-mcp's own layout engine.

        Deliberately built to work on both codebase-memory-mcp 0.8.1 (what's actually
        installed/targeted) and 0.11+ (what an earlier version of this method was
        verified against, before that turned out not to be what's really deployed).
        Three real, version-specific engine quirks drove this shape -- all confirmed
        directly, not assumed:

        - 0.8.1 has no `--quiet`/`--format`/`--offset`/`--max-rows` CLI flags at all
          (`--quiet` is parsed as the tool name and fails outright) -- so this uses
          `CodebaseMemoryCLI.query_graph`/`get_graph_schema`'s ordinary raw-JSON-argument
          `--json` invocation, not a flag-based one. `format="json"` is still passed in
          the payload: required and honored on 0.11+, harmlessly ignored on 0.8.1 --
          confirmed directly, so one call shape works unmodified on both.
        - 0.8.1 has no working pagination: `offset` is silently ignored (an `offset=5`
          call returns the identical rows as `offset=0`) and there's no `has_more` field
          to detect truncation. So there's no pagination loop here at all -- each query
          asks for a single, generous `_MAX_QUERY_ROWS` in one call and accepts the
          (real, but distant for anything this size) risk of truncation on a project
          with more matches than that for a single node/edge-type query.
        - `type(r)` on an *untyped* relationship pattern (`MATCH (a)-[r]->(b)`) returns a
          bare numeric code on 0.8.1, not the type's name, but the identical query scoped
          to one explicit type (`MATCH (a)-[r:CALLS]->(b)`) returns the real string. So
          edges are fetched with one `query_graph` call *per* schema edge type (from
          `get_graph_schema`, aggregated with `count(r)`), never as one combined
          `MATCH (a)-[r]->(b)` -- which also happens to route around a *separate* 0.11+
          issue: a single aggregated query spanning multiple relationship types there
          silently drops most of them (confirmed: `CALLS` alone came back as 4 rows
          instead of the real 18). Two different engine bugs on two different versions,
          one query shape avoids both. This is the same reason codebase-memory-mcp's own
          native code (`layout3d.c`) never combines types either: it loops
          `cbm_store_find_edges_by_type` once per schema edge type via a lower-level
          store API that bypasses Cypher aggregation entirely.

        No label filter on nodes -- Function, Method, Class, Variable, File, Module,
        Folder, Project, Route, Decorator, Package, builtins, everything -- matching
        upstream's own unfiltered node query (`cbm_store_search` in `src/ui/layout3d.c`).

        Layout is `utils.layout3d`: a Barnes-Hut octree for repulsion, spring attraction
        along edges, and an anchor spring back toward each node's initial ring position
        (by directory cluster + BFS call-depth) -- the same real physics upstream's
        native C engine runs, computed here in Python instead of depending on that
        engine directly.

        Node ids are `qualified_name`, not the bare `name` -- this indexer reuses bare
        names across files/components routinely (two `isValidEmail`s, two
        `validateForm`s, etc. are common even in a small repo), so `name` is only safe
        to use as a display label.

        `timeout_seconds` bounds each individual subprocess call -- see
        `CodebaseMemoryCLI._run_subprocess` -- so a stuck one can't hang this method
        forever; the `asyncio.wait_for` at the bottom is the second, outer bound covering
        the whole method (queries plus the physics simulation) plus thread scheduling, so
        whatever's awaiting this always gets a response either way.
        """

        def _truthy(v: Any) -> bool:
            return str(v).strip().lower() in ("true", "1")

        def _build() -> dict[str, Any]:
            cli = CodebaseMemoryCLI()

            node_result = cli.unwrap(
                cli.query_graph(
                    project=project_name,
                    query=(
                        "MATCH (n) RETURN n.label AS label, n.name AS name, "
                        "n.qualified_name AS qn, n.file_path AS fp, "
                        "n.start_line AS sl, n.end_line AS el, "
                        "n.is_entry_point AS ie, n.is_test AS it, "
                        "n.is_exported AS ix, n.route_path AS rp"
                    ),
                    format="json",
                    max_rows=_MAX_QUERY_ROWS,
                    timeout_seconds=_CALL_TIMEOUT_SECONDS,
                )
            ) or {}
            node_columns = node_result.get("columns") or []

            raw_nodes = []
            for row in node_result.get("rows") or []:
                r = dict(zip(node_columns, row))
                raw_nodes.append({
                    "label": r.get("label") or "",
                    "name": r.get("name") or "",
                    "qualified_name": r.get("qn") or "",
                    "file_path": r.get("fp") or "",
                    "start_line": int(r["sl"]) if r.get("sl") else 0,
                    "end_line": int(r["el"]) if r.get("el") else 0,
                    "is_entry": _truthy(r.get("ie")),
                    "is_test": _truthy(r.get("it")),
                    "is_exported": _truthy(r.get("ix")),
                    "is_route": bool(r.get("rp")),
                })

            if _MAX_GRAPH_NODES and len(raw_nodes) > _MAX_GRAPH_NODES:
                raw_nodes = raw_nodes[:_MAX_GRAPH_NODES]

            qn_to_idx = {nd["qualified_name"]: i for i, nd in enumerate(raw_nodes)}
            n = len(raw_nodes)

            edges: list[tuple[int, int]] = []
            edge_types: list[str] = []
            in_calls = [0] * n
            in_usage = [0] * n
            degree = [0] * n

            schema = cli.unwrap(
                cli.get_graph_schema(project=project_name, format="json", timeout_seconds=_CALL_TIMEOUT_SECONDS)
            ) or {}
            schema_edge_types = [e["type"] for e in schema.get("edge_types", []) if e.get("type")]

            for edge_type in schema_edge_types:
                edge_result = cli.unwrap(
                    cli.query_graph(
                        project=project_name,
                        query=(
                            f"MATCH (a)-[r:{edge_type}]->(b) RETURN DISTINCT "
                            "a.qualified_name AS source, b.qualified_name AS target, count(r) AS weight"
                        ),
                        format="json",
                        max_rows=_MAX_QUERY_ROWS,
                        timeout_seconds=_CALL_TIMEOUT_SECONDS,
                    )
                ) or {}
                columns = edge_result.get("columns") or []
                for row in edge_result.get("rows") or []:
                    r = dict(zip(columns, row))
                    s = qn_to_idx.get(r.get("source"))
                    t = qn_to_idx.get(r.get("target"))
                    if s is None or t is None:
                        continue  # endpoint outside the max-nodes window, same as the C sampling behavior
                    weight = int(r.get("weight") or 0)
                    edges.append((s, t))
                    edge_types.append(edge_type)
                    degree[s] += weight
                    degree[t] += weight
                    if edge_type in _CALL_EDGE_TYPES:
                        in_calls[t] += weight
                    if edge_type in _USAGE_EDGE_TYPES:
                        in_usage[t] += weight

            cluster_keys = [layout3d.cluster_key(nd["file_path"]) for nd in raw_nodes]
            qualified_names = [nd["qualified_name"] for nd in raw_nodes]
            entry_indices = [
                i for i, nd in enumerate(raw_nodes) if nd["label"] in ("Route", "File", "Module", "Package")
            ]

            depths = layout3d.compute_call_depth(n, edges, entry_indices)
            bodies = layout3d.seed_ring_layout(cluster_keys, qualified_names, depths)
            for body, d in zip(bodies, degree):
                body.mass = float(d + 1)
            layout3d.local_optimize(bodies, edges)

            # Dead-code-ish status, adapted from codebase-memory-mcp's own graph-ui
            # (src/ui/layout3d.c), but with `is_exported` demoted from "checked first" to
            # "only overrides the dead-code call": `is_exported` means very different
            # things per language (a deliberate, selective marker in TS/Java/Go/C#, but
            # near-universal on Python's top-level defs -- confirmed against a real
            # FastAPI project: 8 of 13 functions came back `is_exported=true`). Checking
            # it before connectivity, as upstream does, would relabel every one of those
            # 8 the same flat "exported" grey regardless of whether they had 0 or 5 real
            # callers, destroying the one signal that's actually language-agnostic: the
            # CALLS edge count.
            out_nodes = []
            for i, nd in enumerate(raw_nodes):
                deg = degree[i]
                is_fn = nd["label"] in _FUNCTION_LABELS
                testish = nd["is_test"] or "/test" in nd["file_path"].lower() or "test_" in nd["file_path"].lower()

                if not is_fn:
                    status = "structural"
                elif testish:
                    status = "test"
                elif nd["is_entry"] or nd["is_route"]:
                    status = "entry"
                elif in_calls[i] == 0 and in_usage[i] == 0:
                    status = "exported" if nd["is_exported"] else "dead"
                elif in_calls[i] == 1:
                    status = "single"
                else:
                    status = "normal"

                base_size = layout3d.size_for_label(nd["label"])
                deg_boost = min(deg * 0.3, 10.0) if deg > 5 else 0.0

                out_nodes.append({
                    "id": nd["qualified_name"],
                    "x": bodies[i].x, "y": bodies[i].y, "z": bodies[i].z,
                    "label": nd["label"],
                    "name": nd["name"],
                    "file_path": nd["file_path"],
                    "start_line": nd["start_line"],
                    "end_line": nd["end_line"],
                    "size": base_size + deg_boost,
                    "color": layout3d.stellar_color(deg),
                    "in_calls": in_calls[i],
                    "status": status,
                })

            out_edges = [
                {"source": qualified_names[s], "target": qualified_names[t], "type": et}
                for (s, t), et in zip(edges, edge_types)
            ]

            return {"project": project_name, "nodes": out_nodes, "edges": out_edges, "total_nodes": n}

        return await asyncio.wait_for(asyncio.to_thread(_build), timeout=_GRAPH_TIMEOUT_SECONDS)


run_manager = RunManager()
