"""Owns the lifecycle of every pipeline run: starting, streaming, stopping.

A run's actual agent execution is decoupled from any single HTTP/SSE
connection — it runs in a background `asyncio.Task`, and any number of SSE
subscribers can attach to (and detach from) the same run's live event feed
without affecting it. This is what lets a page refresh reattach to an
in-progress run instead of restarting or losing it, for as long as this
process stays alive.

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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.orchestrator import create_orchestrator_agent

from .event_handler import EventHandler
from .events import default_steps
from .history_store import HistoryStore
from .run_store import RunStore


def _derive_name(repo_path: str) -> str:
    cleaned = repo_path.rstrip("/\\")
    return cleaned.replace("\\", "/").split("/")[-1] or "repository"


class RunManager:
    def __init__(self) -> None:
        self._store = RunStore()
        self._history = HistoryStore()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # --- lifecycle --------------------------------------------------------

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    # --- starting/observing/stopping runs -----------------------------------

    async def start_run(self, repo_path: str) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        name = _derive_name(repo_path)
        record = await self._store.create(run_id, repo_path, name)
        self._tasks[run_id] = asyncio.create_task(self._execute_run(run_id, repo_path))
        return record

    def stop_run(self, run_id: str) -> bool:
        """Cancel a run's background task. Returns False if it wasn't running."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def get_project(self, run_id: str) -> dict[str, Any] | None:
        record = await self._store.get(run_id)
        if record is not None:
            status = {"running": "active", "completed": "complete", "failed": "failed", "stopped": "failed"}.get(
                record["status"], "active"
            )
            return {"id": run_id, "name": record["name"], "repoUrl": record["repo_path"], "status": status}

        # Not an active/known-this-process run — it may be a completed run
        # from before a restart, which only `HistoryStore` still remembers.
        completed = await self._history.get(run_id)
        if completed is None:
            return None
        return {
            "id": run_id,
            "name": completed.get("name") or "repository",
            "repoUrl": completed.get("repo_path") or "",
            "status": "complete",
        }

    async def list_projects(self) -> list[dict[str, Any]]:
        """Every analysis the home dashboard should show: this process's own
        runs (active, failed, or completed — real-time status) plus any
        older completed run from before a restart that `HistoryStore` still
        remembers and this process hasn't seen. A run present in both is
        only listed once, using the live in-memory status.
        """
        in_memory = await self._store.list_all()
        seen_ids = {r["run_id"] for r in in_memory}
        projects = [
            {
                "id": r["run_id"],
                "name": r["name"],
                "repoUrl": r["repo_path"],
                "status": {
                    "running": "active",
                    "completed": "complete",
                    "failed": "failed",
                    "stopped": "failed",
                }.get(r["status"], "active"),
            }
            for r in in_memory
        ]
        # `list_all` already filters out records missing run_id/output_dir
        # (see HistoryStore); name/repo_path still get a safe default here in
        # case a future record shape ever omits them.
        projects.extend(
            {
                "id": r["run_id"],
                "name": r.get("name") or "repository",
                "repoUrl": r.get("repo_path") or "",
                "status": "complete",
            }
            for r in await self._history.list_all()
            if r["run_id"] not in seen_ids
        )
        return projects

    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        return self._store.load_steps(run_id) or [s.model_dump() for s in default_steps()]

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

    # --- subscription (SSE fan-out) ----------------------------------------

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(run_id)
        if subscribers and queue in subscribers:
            subscribers.remove(queue)

    def replay(self, run_id: str) -> list[dict[str, Any]]:
        """Everything already sent for this run, for a client attaching mid-run."""
        events = list(self._store.read_events(run_id))
        steps = self._store.load_steps(run_id)
        if steps is not None:
            events.append({"kind": "steps", "payload": steps})
        return events

    # --- publish surface used by EventHandler ------------------------------

    async def publish(self, run_id: str, logline: dict[str, Any]) -> None:
        envelope = {"kind": "log", "payload": logline}
        self._store.append_event(run_id, envelope)
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(envelope)

    async def publish_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        self._store.save_steps(run_id, steps)
        envelope = {"kind": "steps", "payload": steps}
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(envelope)

    async def publish_terminal(self, run_id: str, status: str, message: str) -> None:
        envelope = {"kind": "terminal", "payload": {"status": status, "message": message}}
        self._store.append_event(run_id, envelope)
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(envelope)

    async def set_project_name(self, run_id: str, project_name: str) -> None:
        await self._store.update(run_id, project_name=project_name)

    async def save_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        self._store.save_progress(run_id, progress)

    # --- execution ----------------------------------------------------------

    async def _execute_run(self, run_id: str, repo_path: str) -> None:
        handler = EventHandler(run_id, self)

        try:
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
            await self._record_completed(run_id, repo_path)
            await handler.emit_terminal("completed", "Pipeline run completed.")
        except asyncio.CancelledError:
            await self._store.update(run_id, status="stopped")
            await handler.emit_terminal("stopped", "Pipeline run stopped.")
            raise
        except Exception as exc:
            await self._store.update(run_id, status="failed", error=str(exc))
            await handler.emit_terminal("failed", f"Pipeline run failed: {exc}")
        finally:
            self._tasks.pop(run_id, None)

    async def _record_completed(self, run_id: str, repo_path: str) -> None:
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


run_manager = RunManager()
