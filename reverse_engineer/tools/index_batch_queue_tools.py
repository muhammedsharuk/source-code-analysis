"""Deterministic batch queue that splits Code Index (Stage A) work across fresh subagents.

The Code Index Agent used to do all of this in one long-running session:
orient, enumerate every checklist entry, judge composite/root-file entry
points, sweep for registration-only functionality, then self-check its own
completeness before batching Stage C. Across real runs that single session
degraded under its own length — thin enumeration, false "nothing here"
`reason`s, and (once) proceeding to batching despite its own coverage check
failing. None of those were one-off bugs; they were symptoms of one agent
carrying the whole checklist's workload in one context.

This module gives each checklist entry to a small, fresh "Index Batch
Agent" invocation instead — a handful of entries per batch, no accumulated
context from any other batch. Two things are deliberately NOT left to that
agent's own judgment, because they were exactly what went wrong before:

- Scope exclusion. A checklist entry with narrower entries nested under it
  (e.g. a broad `frontend/src/features` alongside `frontend/src/features/auth`)
  must not have its own investigation pull in results that actually belong to
  a nested entry — otherwise two unrelated batches can produce overlapping or
  duplicate results for the same files. `search_own_scope_entry_points` below
  enforces this by construction (it looks up and excludes nested checklist
  paths itself, the same way `checklist_tools._recheck_reason` already does),
  so no agent is ever handed raw, unfiltered results and asked to exclude
  the right things itself.
- Coverage bookkeeping. An agent never writes its own `covered_by`/`status`
  claims here. `merge_index_batches` derives them mechanically from what a
  batch actually produced: real units it wrote become `covered_by`, an
  empty result with a `reason` becomes a verified-empty entry, and an entry
  a batch was assigned but never resolved is simply left `pending` — which
  `verify_checklist_coverage` will then flag on its own. This removes an
  entire category of self-reported "done" that has been unreliable all
  night, the same way `build_batch_queue` no longer trusts a caller's word
  that `verify_checklist_coverage` passed.

Every state transition here is plain, deterministic Python, mirroring
`tools/batch_queue_tools.py`'s design for Stage C: a batch is never handed
out twice, a batch that reports success with a missing or incomplete
partial file is rejected rather than trusted, and a batch that keeps
failing gets a bounded number of retries rather than looping forever.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from tools.checklist_tools import (
    _is_ancestor,
    _is_test_path,
    _normalize,
    _own_scope_prefix,
    _scope_file_pattern,
    _unwrap_cli_result,
    verify_checklist_coverage,
)
from utils.codebase_memory import CodebaseMemoryCLI
from utils.tool_safety import tool_safe
from utils.workspace import CHECKLIST_FILENAME, CODE_INDEX_FILENAME, resolve_workspace_dir

MAX_ATTEMPTS = 3
# Bounds how many times requeue_index_batch_from_problems will spin up a new
# retry batch for still-failing paths, so a genuinely stuck path terminates
# as a reported problem instead of looping the orchestrator indefinitely.
MAX_REQUEUE_ROUNDS = 3
DEFAULT_INDEX_BATCH_SIZE = 4

_SCOPE_PAGE_SIZE = 100
_SCOPE_MAX_PAGES = 15

INDEX_BATCH_QUEUE_FILENAME = "index_batch_queue.json"
INDEX_PARTIAL_DIRNAME = "index_partial"


class IndexBatchQueueError(RuntimeError):
    """Raised when the checklist or index batch queue cannot be read, written, or resolved."""


class _FileLock:
    """A simple, dependency-free, cross-platform exclusive lock using a sentinel file.

    Duplicated from `tools/batch_queue_tools.py` rather than imported, so this
    module stays independent (matching how `tools/checklist_tools.py` keeps
    its own `_read_json` rather than sharing one) — same mechanism, own error
    type.
    """

    def __init__(self, target_path: Path, timeout_seconds: float = 15.0, poll_interval_seconds: float = 0.05):
        self._lock_path = target_path.with_suffix(target_path.suffix + ".lock")
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise IndexBatchQueueError(
                        f"Timed out after {self._timeout_seconds}s waiting for lock on {self._lock_path}."
                    )
                time.sleep(self._poll_interval_seconds)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._lock_path.unlink(missing_ok=True)
        return False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise IndexBatchQueueError(f"Expected file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise IndexBatchQueueError(f"File is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndexBatchQueueError(f"File is not valid JSON: {path} ({exc})") from exc


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _find_index_batch(queue: dict[str, Any], batch_id: str) -> dict[str, Any]:
    for batch in queue.get("batches", []):
        if batch["batch_id"] == batch_id:
            return batch
    raise IndexBatchQueueError(f"Batch '{batch_id}' was not found in {INDEX_BATCH_QUEUE_FILENAME}.")


def _apply_failure(batch: dict[str, Any], error: str) -> bool:
    """Record a failed attempt on `batch` in place. Returns True if it will be retried."""
    batch["attempts"] = batch.get("attempts", 0) + 1
    batch["last_error"] = error
    will_retry = batch["attempts"] < MAX_ATTEMPTS
    batch["status"] = "pending" if will_retry else "failed_permanent"
    return will_retry


@tool_safe
def build_index_batch_queue(
    project_name: str, batch_size: int = DEFAULT_INDEX_BATCH_SIZE, force_rebuild: bool = False
) -> dict[str, Any]:
    """Deterministically group the checklist into small batches for the Index Batch Agent.

    Reads `/workspace/code_index_checklist.json` (call `seed_checklist` first)
    and chunks its entries, in the order they appear, into batches of at most
    `batch_size` checklist paths each. Writes `/workspace/index_batch_queue.json`.

    Args:
        project_name: The exact indexed project name.
        batch_size: Maximum number of checklist paths per batch. Kept small
            (default 4) so each Index Batch Agent invocation gets a workload
            small enough to investigate thoroughly in a fresh context.
        force_rebuild: When True, rebuild and overwrite an existing queue.
            When False (default) and a queue already exists, it is left
            untouched and its current summary is returned instead.

    Returns:
        {"total_batches": int, "total_entries": int}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / INDEX_BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        if queue_path.exists() and not force_rebuild:
            existing = _read_json(queue_path)
            batches = existing.get("batches", [])
            total_entries = sum(len(b.get("paths", [])) for b in batches)
            return {"total_batches": len(batches), "total_entries": total_entries}

        checklist = _read_json(workspace_dir / CHECKLIST_FILENAME)
        entries = checklist.get("entries", [])
        if not entries:
            raise IndexBatchQueueError(
                f"No entries found in {CHECKLIST_FILENAME} — call seed_checklist first."
            )

        batches: list[dict[str, Any]] = []
        for i in range(0, len(entries), max(1, batch_size)):
            chunk = entries[i : i + batch_size]
            batches.append(
                {
                    "batch_id": f"ibatch_{len(batches) + 1:03d}",
                    "paths": [e["path"] for e in chunk],
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                }
            )

        queue = {"project_name": project_name, "requeue_rounds": 0, "batches": batches}
        _atomic_write_json(queue_path, queue)

        return {"total_batches": len(batches), "total_entries": len(entries)}


@tool_safe
def get_next_index_batch(project_name: str) -> dict[str, Any] | None:
    """Atomically claim the next pending index batch.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {"batch_id": str, "paths": list[str]} for the newly claimed batch,
        or None (a deliberate, explicit None) if no pending batch remains.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / INDEX_BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        for batch in queue.get("batches", []):
            if batch["status"] == "pending":
                batch["status"] = "in_progress"
                _atomic_write_json(queue_path, queue)
                return {"batch_id": batch["batch_id"], "paths": batch["paths"]}

    return None


@tool_safe
def get_index_batch_details(project_name: str, batch_id: str) -> dict[str, Any]:
    """Fetch the checklist entries assigned to one index batch, with their exclusion scope.

    This is the only call a batched Index Batch Agent subagent needs to see
    its own scoped work. For each assigned path, `excluded_subpaths` lists
    any other checklist entries nested underneath it — content already
    claimed by those narrower entries, which this entry's own investigation
    must not re-claim. `search_own_scope_entry_points` enforces this
    automatically; `excluded_subpaths` here is for your own awareness of why
    some files under your path won't appear in its results.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch identifier handed to this subagent invocation.

    Returns:
        {"batch_id": str, "entries": [{"path", "size_hint", "excluded_subpaths"}]}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue = _read_json(workspace_dir / INDEX_BATCH_QUEUE_FILENAME)
    batch = _find_index_batch(queue, batch_id)

    checklist = _read_json(workspace_dir / CHECKLIST_FILENAME)
    all_entries = checklist.get("entries", [])
    all_paths = [e.get("path", "") for e in all_entries]
    size_by_path = {e.get("path", ""): e.get("size_hint") for e in all_entries}

    entries = []
    for path in batch["paths"]:
        excluded_subpaths = [p for p in all_paths if p != path and _is_ancestor(path, p)]
        entries.append(
            {
                "path": path,
                "size_hint": size_by_path.get(path),
                "excluded_subpaths": excluded_subpaths,
            }
        )

    return {"batch_id": batch_id, "entries": entries}


@tool_safe
def search_own_scope_entry_points(project_name: str, path: str, label: str) -> dict[str, Any]:
    """Find candidates of one graph label under a checklist path's OWN scope only.

    Paginates `search_graph(label=label, file_pattern=f"{path}/**")` (recursive
    matching, confirmed against the real CLI) and filters out: known
    test-path/test-file conventions, and any result whose file_path falls
    under a narrower checklist entry nested inside `path` (that content
    belongs to that nested entry's own batch, not this one). This is the
    mechanical replacement for the pattern that used to fail: an agent
    querying broadly and being trusted to notice and exclude a nested
    child's files itself. Call this once per (path, label) you want to
    check for entry-point candidates — it never returns results outside
    this entry's own real scope.

    Args:
        project_name: The exact indexed project name.
        path: The checklist entry's own path (must match a real entry in
            code_index_checklist.json — get it from get_index_batch_details).
        label: The graph label to search (e.g. "Function", "Method",
            "Class", "Route", "Interface" — whatever this project's
            get_graph_schema shows).

    Returns:
        {
          "path": str, "label": str, "excluded_subpaths": list[str],
          "total_matched": int, "results": [...], "truncated": bool,
        }
        `results` are the raw search_graph result objects for this label,
        already filtered to this path's own scope and deduplicated by
        qualified_name. `truncated` is True only if pagination hit its
        page-count safety limit before exhausting real matches.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    checklist = _read_json(workspace_dir / CHECKLIST_FILENAME)
    all_paths = [e.get("path", "") for e in checklist.get("entries", [])]
    excluded_subpaths = [p for p in all_paths if p != path and _is_ancestor(path, p)]

    own_scope_prefix = _own_scope_prefix(path)
    normalized_excluded = [_normalize(p) for p in excluded_subpaths]

    def is_own_scope(file_path: str) -> bool:
        if _is_test_path(file_path):
            return False
        normalized_fp = _normalize(file_path)
        if not normalized_fp.startswith(own_scope_prefix):
            return False
        return not any(normalized_fp == n or normalized_fp.startswith(n + "/") for n in normalized_excluded)

    cli = CodebaseMemoryCLI()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    truncated = True
    for _ in range(_SCOPE_MAX_PAGES):
        raw = cli.search_graph(
            project=project_name, label=label, file_pattern=_scope_file_pattern(path), limit=_SCOPE_PAGE_SIZE, offset=offset
        )
        data = _unwrap_cli_result(raw)
        for result in data.get("results", []):
            file_path = result.get("file_path", "")
            if not is_own_scope(file_path):
                continue
            key = result.get("qualified_name") or f"{file_path}::{result.get('name', '')}"
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
        if not data.get("has_more"):
            truncated = False
            break
        offset += _SCOPE_PAGE_SIZE

    return {
        "path": path,
        "label": label,
        "excluded_subpaths": excluded_subpaths,
        "total_matched": len(results),
        "results": results,
        "truncated": truncated,
    }


@tool_safe
def mark_index_batch_complete(project_name: str, batch_id: str) -> dict[str, Any]:
    """Validate and accept one index batch's partial file as done.

    Reads `/workspace/index_partial/{batch_id}.json` itself rather than
    trusting the agent's own "success" report: it must exist, parse as
    JSON, and contain a `results` entry for every path this batch was
    assigned, each with either real `units` or a `reason`. If any of that
    is missing, this is treated as a failed attempt (same retry accounting
    as `mark_index_batch_failed`) rather than silently accepted — an agent
    that reports success with a missing or incomplete partial must not be
    able to make that stick.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch that finished.

    Returns:
        On success: {"batch_id": str, "accepted": True, "status": "done"}
        On rejection: {"batch_id": str, "accepted": False, "status": "pending" | "failed_permanent",
                       "attempts": int, "will_retry": bool, "detail": str}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / INDEX_BATCH_QUEUE_FILENAME
    partial_path = workspace_dir / INDEX_PARTIAL_DIRNAME / f"{batch_id}.json"

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        batch = _find_index_batch(queue, batch_id)

        error: str | None = None
        partial: dict[str, Any] | None = None

        if not partial_path.exists():
            error = f"No partial file found at {partial_path}."
        else:
            try:
                partial = json.loads(partial_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                error = f"Partial file is not valid JSON: {exc}."

        if error is None:
            results = partial.get("results", []) if isinstance(partial, dict) else []
            results_by_path = {r.get("path"): r for r in results if isinstance(r, dict)}
            missing = [p for p in batch["paths"] if p not in results_by_path]
            unresolved = [
                p
                for p in batch["paths"]
                if p in results_by_path and not results_by_path[p].get("units") and not results_by_path[p].get("reason")
            ]
            if missing:
                error = f"Partial file is missing results for assigned path(s): {missing}."
            elif unresolved:
                error = f"Partial file has no units and no reason for path(s): {unresolved}."

        if error is not None:
            will_retry = _apply_failure(batch, error)
            _atomic_write_json(queue_path, queue)
            return {
                "batch_id": batch_id,
                "accepted": False,
                "status": batch["status"],
                "attempts": batch["attempts"],
                "will_retry": will_retry,
                "detail": error,
            }

        batch["status"] = "done"
        batch["last_error"] = None
        _atomic_write_json(queue_path, queue)

    return {"batch_id": batch_id, "accepted": True, "status": "done"}


@tool_safe
def mark_index_batch_failed(project_name: str, batch_id: str, error: str) -> dict[str, Any]:
    """Record an index batch failure and decide whether it will be retried.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch that failed.
        error: A short description of what went wrong.

    Returns:
        {"batch_id": str, "status": "pending" | "failed_permanent", "attempts": int, "will_retry": bool}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / INDEX_BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        batch = _find_index_batch(queue, batch_id)
        will_retry = _apply_failure(batch, error)
        _atomic_write_json(queue_path, queue)

        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "attempts": batch["attempts"],
            "will_retry": will_retry,
        }


@tool_safe
def get_index_batch_queue_status(project_name: str) -> dict[str, Any]:
    """Return index batch counts grouped by status, for progress checks.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {"pending": int, "in_progress": int, "done": int, "failed_permanent": int, "total": int}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue = _read_json(workspace_dir / INDEX_BATCH_QUEUE_FILENAME)

    counts = {"pending": 0, "in_progress": 0, "done": 0, "failed_permanent": 0}
    for batch in queue.get("batches", []):
        status = batch.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1

    counts["total"] = sum(counts.values())
    return counts


@tool_safe
def merge_index_batches(project_name: str) -> dict[str, Any]:
    """Deterministically rebuild code_index.json and the checklist from every done batch's partial.

    Reads every batch's `/workspace/index_partial/{batch_id}.json` in queue
    order (batches with status other than "done" are skipped and listed in
    `skipped_batches`), keyed by checklist path — a later batch's result for
    a path overwrites an earlier one's, so a requeued retry batch's result
    always wins over the original failed attempt's. `unit_id`s are then
    assigned fresh, in a second pass over the checklist's own entry order
    (not batch order), so this is safe to call again after further retries:
    the result is always rebuilt from scratch from whatever batches are
    currently "done", never appended to.

    For each checklist entry: a path whose winning result has real `units`
    gets those units added to code_index.json and `covered_by` set to their
    new unit_ids; a path whose winning result has no units but a `reason`
    is marked done with that reason; a path with no winning result at all
    (never resolved by any done batch) is left exactly as it was — almost
    always still "pending" — so `verify_checklist_coverage` surfaces it as
    a real, visible problem instead of it silently vanishing.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {
          "total_units": int, "total_entries": int, "total_entries_resolved": int,
          "skipped_batches": [{"batch_id": str, "status": str}, ...],
        }
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue = _read_json(workspace_dir / INDEX_BATCH_QUEUE_FILENAME)
    checklist_path = workspace_dir / CHECKLIST_FILENAME
    checklist = _read_json(checklist_path)
    partial_dir = workspace_dir / INDEX_PARTIAL_DIRNAME

    result_by_path: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    for batch in queue.get("batches", []):
        batch_id = batch["batch_id"]
        if batch.get("status") != "done":
            skipped.append({"batch_id": batch_id, "status": batch.get("status", "unknown")})
            continue

        partial_path = partial_dir / f"{batch_id}.json"
        if not partial_path.exists():
            skipped.append({"batch_id": batch_id, "status": "done_but_partial_file_missing"})
            continue

        partial = json.loads(partial_path.read_text(encoding="utf-8"))
        for r in partial.get("results", []):
            path = r.get("path")
            if path:
                result_by_path[path] = r

    entries = checklist.get("entries", [])
    units: list[dict[str, Any]] = []
    next_unit_number = 1

    for entry in entries:
        result = result_by_path.get(entry.get("path", ""))
        if result is None:
            continue

        entry_units = result.get("units") or []
        if entry_units:
            assigned_ids = []
            for u in entry_units:
                unit_id = f"unit_{next_unit_number:03d}"
                next_unit_number += 1
                units.append(
                    {
                        "unit_id": unit_id,
                        "unit_name": u.get("unit_name", entry.get("path")),
                        "entry_points": u.get("entry_points", []),
                    }
                )
                assigned_ids.append(unit_id)
            entry["status"] = "done"
            entry["covered_by"] = assigned_ids
            entry["reason"] = None
        elif result.get("reason"):
            entry["status"] = "done"
            entry["covered_by"] = []
            entry["reason"] = result.get("reason")

    code_index = {"project_name": project_name, "generated_at": "unknown", "units": units}
    _atomic_write_json(workspace_dir / CODE_INDEX_FILENAME, code_index)
    _atomic_write_json(checklist_path, checklist)

    return {
        "total_units": len(units),
        "total_entries": len(entries),
        "total_entries_resolved": sum(1 for e in entries if e.get("status") == "done"),
        "skipped_batches": skipped,
    }


@tool_safe
def requeue_index_batch_from_problems(project_name: str) -> dict[str, Any]:
    """Re-check checklist coverage and, if it fails, queue exactly the failing paths for retry.

    Calls `verify_checklist_coverage` itself and extracts the failing paths
    from its `problems` list in plain Python — the orchestrator never has to
    parse that list or construct a retry batch itself. Call this in a loop
    after `merge_index_batches`: while it returns `requeued: true`, dispatch
    the new batch it created through the same batch loop as any other, then
    call this again. When it returns `all_clear: true`, coverage is genuinely
    clean. When it returns `requeued: false` with `all_clear: false`, the
    bounded retry budget (`MAX_REQUEUE_ROUNDS`) is exhausted — stop looping
    and report the remaining paths as unresolved rather than retrying forever.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {"requeued": bool, "new_batch_id": str | None, "paths": list[str], "all_clear": bool, "detail": str | None}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / INDEX_BATCH_QUEUE_FILENAME

    coverage = verify_checklist_coverage(project_name)
    if coverage.get("all_clear", False):
        return {"requeued": False, "new_batch_id": None, "paths": [], "all_clear": True, "detail": None}

    problems = coverage.get("problems")
    if problems is None:
        raise IndexBatchQueueError(
            f"verify_checklist_coverage did not report all_clear and returned no problems list: {coverage}"
        )

    failing_paths = sorted({p.get("path") for p in problems if p.get("path")})
    if not failing_paths:
        raise IndexBatchQueueError(
            f"verify_checklist_coverage reported problems with no usable paths to requeue: {problems}"
        )

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        rounds = queue.get("requeue_rounds", 0)
        if rounds >= MAX_REQUEUE_ROUNDS:
            return {
                "requeued": False,
                "new_batch_id": None,
                "paths": failing_paths,
                "all_clear": False,
                "detail": (
                    f"Reached MAX_REQUEUE_ROUNDS={MAX_REQUEUE_ROUNDS} with these paths still unresolved; "
                    "stop retrying and report them as unresolved instead of looping further."
                ),
            }

        batches = queue.setdefault("batches", [])
        new_batch_id = f"ibatch_{len(batches) + 1:03d}"
        batches.append(
            {
                "batch_id": new_batch_id,
                "paths": failing_paths,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
            }
        )
        queue["requeue_rounds"] = rounds + 1
        _atomic_write_json(queue_path, queue)

    return {"requeued": True, "new_batch_id": new_batch_id, "paths": failing_paths, "all_clear": False, "detail": None}


# Full set, for wiring convenience.
INDEX_BATCH_QUEUE_TOOLS = [
    build_index_batch_queue,
    get_next_index_batch,
    get_index_batch_details,
    search_own_scope_entry_points,
    mark_index_batch_complete,
    mark_index_batch_failed,
    get_index_batch_queue_status,
    merge_index_batches,
    requeue_index_batch_from_problems,
]

# The orchestrator drives the loop and marks outcomes, but never needs the
# per-path payload (that belongs only in the Index Batch Agent's own fresh
# context) — mirrors ORCHESTRATOR_BATCH_QUEUE_TOOLS's design for Stage C.
ORCHESTRATOR_INDEX_BATCH_QUEUE_TOOLS = [
    build_index_batch_queue,
    get_next_index_batch,
    mark_index_batch_complete,
    mark_index_batch_failed,
    get_index_batch_queue_status,
    merge_index_batches,
    requeue_index_batch_from_problems,
]

# The batched Index Batch Agent subagent needs to fetch its own scoped paths
# and query its own scope; marking outcomes is the orchestrator's job.
INDEX_BATCH_AGENT_TOOLS = [get_index_batch_details, search_own_scope_entry_points]
