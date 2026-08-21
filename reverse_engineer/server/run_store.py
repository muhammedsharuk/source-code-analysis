"""In-memory run registry, per-run event log, and per-run step snapshot.

Deliberately NOT persisted to disk. An earlier version of this module wrote
everything here (plus a LangGraph checkpointer in `run_manager.py`) to
survive a server restart. In practice that caused a worse problem than the
one it solved: any run still marked "running" when the process restarted
looked identical to a genuinely orphaned run, so `run_manager.startup()`
would silently resume it — running concurrently with whatever new run the
user then intentionally started, with no UI indication either was
happening. Confirmed against a real session: exactly this produced two
simultaneous runs from what looked like one click.

Restoring durable persistence later needs to solve that distinction first
(e.g. a liveness lease per run, not just a status string) rather than
reintroducing this same file-backed version as-is.

Every method here keeps the same signature the disk-backed version had, so
nothing in `run_manager.py`/`event_handler.py` needed to change — only this
module's storage.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._registry: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._steps: dict[str, list[dict[str, Any]]] = {}
        self._progress: dict[str, dict[str, Any]] = {}

    # --- registry -----------------------------------------------------

    async def create(self, run_id: str, repo_path: str, name: str) -> dict[str, Any]:
        async with self._lock:
            record = {
                "run_id": run_id,
                "repo_path": repo_path,
                "name": name,
                "project_name": None,
                "status": "running",
                "error": None,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            self._registry[run_id] = record
            return dict(record)

    async def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            record = self._registry.get(run_id)
            if record is None:
                raise KeyError(f"Unknown run_id: {run_id}")
            record.update(fields)
            record["updated_at"] = _now_iso()
            return dict(record)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            record = self._registry.get(run_id)
            return dict(record) if record else None

    async def list_running(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(r) for r in self._registry.values() if r.get("status") == "running"]

    async def list_all(self) -> list[dict[str, Any]]:
        """Every run this process knows about, regardless of status.

        Unlike `HistoryStore`, this is never the sole record of anything —
        it only reflects the current process's lifetime, which is exactly
        why the home dashboard also needs `HistoryStore` for runs from
        before a restart.
        """
        async with self._lock:
            return [dict(r) for r in self._registry.values()]

    # --- per-run event log ---------------------------------------------

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        self._events.setdefault(run_id, []).append(event)

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(run_id, []))

    # --- per-run step snapshot ------------------------------------------

    def save_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        self._steps[run_id] = list(steps)

    def load_steps(self, run_id: str) -> list[dict[str, Any]] | None:
        steps = self._steps.get(run_id)
        return list(steps) if steps is not None else None

    # --- per-run progress counters ---------------------------------------

    def save_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        self._progress[run_id] = dict(progress)

    def load_progress(self, run_id: str) -> dict[str, Any] | None:
        progress = self._progress.get(run_id)
        return dict(progress) if progress is not None else None
