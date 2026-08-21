"""Durable record of completed runs only — deliberately not a general run log.

`run_store.py` explains why persisting a run's *in-flight* state (status,
progress, steps) was reverted: a record still marked "running" after a
server restart is indistinguishable from an orphan, and it caused duplicate
concurrent runs in practice. This store sidesteps that failure mode
entirely by having exactly one write path: `record_completed()`, called
once, after `_execute_run` has already reached `status="completed"`. There
is no update path and nothing here is ever resumed — a completed run is
just a finished fact, browsable after a restart the same in-flight run list
never was.

Backed by a small JSON file (`server/runs/history.json`) rather than a
database: this only ever grows by one record per finished pipeline run, so
loading/rewriting the whole list on each access is simple and fast enough.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

_HISTORY_PATH = Path(__file__).resolve().parent / "runs" / "history.json"


class HistoryStore:
    def __init__(self, path: Path = _HISTORY_PATH) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _read_all(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    async def record_completed(self, record: dict[str, Any]) -> None:
        async with self._lock:
            records = self._read_all()
            records = [r for r in records if r.get("run_id") != record.get("run_id")]
            records.append(record)
            self._write_all(records)

    async def list_all(self) -> list[dict[str, Any]]:
        """Every well-formed record, most recent first.

        `history.json` is gitignored and lives outside anything this process
        controls end-to-end (a crashed write, a hand edit, a future record
        shape) — a record missing its required fields is skipped rather than
        raised, so one bad entry can't 500 the whole history list.
        """
        async with self._lock:
            records = self._read_all()
            valid = [r for r in records if isinstance(r, dict) and r.get("run_id") and r.get("output_dir")]
            return sorted(valid, key=lambda r: r.get("completed_at", ""), reverse=True)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            for record in self._read_all():
                if isinstance(record, dict) and record.get("run_id") == run_id:
                    return record
            return None
