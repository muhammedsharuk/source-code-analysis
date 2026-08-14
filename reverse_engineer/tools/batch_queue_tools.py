"""Deterministic, non-LLM batch queue for scoping Task Agent work.

These tools coordinate the Code Index Agent (Stage A) and the batched Task
Agent (Stage C) through files stored on the shared `/workspace/` filesystem:

- `code_index.json` — written once by the Code Index Agent; a mechanical
  inventory of coverage units and their entry points.
- `batch_queue.json` — the mutable work queue derived from that index. Also
  holds `next_task_id`, a single global counter used by `allocate_task_ids`
  so that every batch's TASK-IDs are unique across the whole merged
  document, even though batches are investigated independently and never
  see each other's output.
- `tasks_partial/{batch_id}.md` — each batch's own isolated Markdown output,
  written once by that batch's Task Agent invocation. Batches never share a
  file with each other, so there is no read-then-append race and no need
  for any subagent to check for or avoid duplicating another batch's
  content — a batch (even on retry) always just overwrites its own file.
- `TASKS.md` — the final merged document, assembled deterministically by
  `merge_task_batches` from the `tasks_partial/` files once every batch has
  finished, in queue order.

Every state transition here is plain, deterministic Python — no LLM
judgment involved — so the pipeline can resume safely if interrupted
mid-run, and a batch is never silently skipped, handed out twice, or merged
out of order.

Workspace directory resolution note: the orchestrator's `/workspace/`
filesystem route is rooted at `temp/<safe_directory_name(repo_path)>/` at
agent-creation time (see agents/orchestrator.py), before the repository has
even been indexed and before `project_name` is known. These tool functions
only ever receive `project_name` (matching every other tool in this
project, e.g. `save_markdown_document`). In the normal flow the indexer
assigns `project_name` using the same sanitization as
`safe_directory_name(repo_path)` (the orchestrator prompt never passes a
custom `name` to `index_repository`), so `temp/<safe_directory_name(project_name)>/`
resolves to the same physical directory in practice. As a safety net for
the rare case that ever changes, `resolve_workspace_dir` (in
`utils/workspace.py`, shared with `tools/markdown_file_tools.py`) falls back
to scanning `temp/` for a directory whose `code_index.json` records a
matching `project_name`.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from tools.checklist_tools import verify_checklist_coverage
from tools.markdown_file_tools import save_markdown_document
from utils.tool_safety import tool_safe
from utils.workspace import CODE_INDEX_FILENAME, TEMP_ROOT, resolve_workspace_dir

MAX_ATTEMPTS = 3
MAX_ENTRY_POINTS_PER_BATCH = 8

BATCH_QUEUE_FILENAME = "batch_queue.json"
TASKS_PARTIAL_DIRNAME = "tasks_partial"
MERGED_TASKS_FILENAME = "TASKS.md"


class BatchQueueError(RuntimeError):
    """Raised when the code index or batch queue cannot be read, written, or resolved."""


class _FileLock:
    """A simple, dependency-free, cross-platform exclusive lock using a sentinel file.

    Uses `os.O_CREAT | os.O_EXCL` (atomic "create if not exists" on both
    Windows and POSIX) as the mutual-exclusion primitive, so read-modify-write
    sequences on the same queue file can't interleave across threads or
    processes.
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
                    raise BatchQueueError(
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
        raise BatchQueueError(f"Expected file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise BatchQueueError(f"File is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatchQueueError(f"File is not valid JSON: {path} ({exc})") from exc


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _find_batch(queue: dict[str, Any], batch_id: str) -> dict[str, Any]:
    for batch in queue.get("batches", []):
        if batch["batch_id"] == batch_id:
            return batch
    raise BatchQueueError(f"Batch '{batch_id}' was not found in {BATCH_QUEUE_FILENAME}.")


@tool_safe
def build_batch_queue(project_name: str, force_rebuild: bool = False) -> dict[str, Any]:
    """Deterministically group the code index into batches for the Task Agent.

    Reads `/workspace/code_index.json` and greedily packs consecutive units
    into batches so each batch contains at most ~8 total entry points
    (a single unit that alone exceeds that becomes its own batch, unsplit).
    Writes the result to `/workspace/batch_queue.json`. This is plain
    deterministic bookkeeping — call it once, immediately after the Code
    Index Agent finishes writing code_index.json.

    Before building (not on the early-return resume path below), this
    calls `verify_checklist_coverage` itself and refuses with a
    `BatchQueueError` if it does not report `all_clear: true`. This
    precondition is enforced here rather than left to the caller's
    discretion: an earlier version of this pipeline relied on the Code
    Index Agent's own prompt instructions to check coverage first, and a
    real run called this tool anyway despite unresolved problems. Making
    the check a hard precondition means that outcome is no longer possible
    regardless of what any caller does or skips.

    Args:
        project_name: The exact indexed project name.
        force_rebuild: When True, rebuild and overwrite an existing queue,
            discarding any progress already recorded in it. When False
            (default) and a queue already exists, it is left untouched and
            its current summary is returned instead — this makes re-running
            the pipeline resumable without losing in-progress or completed
            batch state.

    Returns:
        A dict: {"total_batches": int, "total_units": int}.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        if queue_path.exists() and not force_rebuild:
            existing = _read_json(queue_path)
            batches = existing.get("batches", [])
            total_units = sum(len(batch.get("unit_ids", [])) for batch in batches)
            return {"total_batches": len(batches), "total_units": total_units}

        coverage = verify_checklist_coverage(project_name)
        if not coverage.get("all_clear", False):
            problems = coverage.get("problems", [])
            detail = coverage.get("error_message") or f"{len(problems)} unresolved checklist problem(s)"
            raise BatchQueueError(
                "Refusing to build the batch queue: verify_checklist_coverage has not reported "
                f"all_clear=true ({detail}). This is enforced here, not left to the caller, "
                "because a run previously called build_batch_queue anyway despite unresolved "
                "problems. Resolve every reported problem by actually investigating the area it "
                "names, then call verify_checklist_coverage again before retrying build_batch_queue. "
                f"First problems: {problems[:5]}"
            )

        index = _read_json(workspace_dir / CODE_INDEX_FILENAME)
        units = index.get("units", [])

        batches: list[dict[str, Any]] = []
        current_unit_ids: list[str] = []
        current_entry_point_count = 0

        def flush_batch() -> None:
            nonlocal current_unit_ids, current_entry_point_count
            if current_unit_ids:
                batches.append(
                    {
                        "batch_id": f"batch_{len(batches) + 1:03d}",
                        "unit_ids": current_unit_ids,
                        "status": "pending",
                        "attempts": 0,
                        "task_ids_written": [],
                        "last_error": None,
                    }
                )
            current_unit_ids = []
            current_entry_point_count = 0

        for unit in units:
            unit_id = unit.get("unit_id")
            entry_point_count = len(unit.get("entry_points", []))

            if entry_point_count > MAX_ENTRY_POINTS_PER_BATCH:
                # Oversized unit: flush whatever is pending, then it gets its
                # own dedicated batch. Stage A is expected to have already
                # pre-split composite units small enough that this is rare.
                flush_batch()
                current_unit_ids.append(unit_id)
                current_entry_point_count = entry_point_count
                flush_batch()
                continue

            if current_unit_ids and current_entry_point_count + entry_point_count > MAX_ENTRY_POINTS_PER_BATCH:
                flush_batch()

            current_unit_ids.append(unit_id)
            current_entry_point_count += entry_point_count

        flush_batch()

        queue = {"project_name": project_name, "next_task_id": 1, "batches": batches}
        _atomic_write_json(queue_path, queue)

        total_units = sum(len(batch["unit_ids"]) for batch in batches)
        return {"total_batches": len(batches), "total_units": total_units}


@tool_safe
def get_next_pending_batch(project_name: str) -> dict[str, Any] | None:
    """Atomically claim the next pending batch of units for the Task Agent.

    Finds the first batch with status "pending", marks it "in_progress", and
    persists that change before returning — so the same batch is never
    handed out twice, even across separate/concurrent calls.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {"batch_id": str, "unit_ids": list[str]} for the newly claimed
        batch, or None (a deliberate, explicit None — not an empty dict) if
        no pending batch remains.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        for batch in queue.get("batches", []):
            if batch["status"] == "pending":
                batch["status"] = "in_progress"
                _atomic_write_json(queue_path, queue)
                return {"batch_id": batch["batch_id"], "unit_ids": batch["unit_ids"]}

    return None


@tool_safe
def get_batch_details(project_name: str, batch_id: str) -> dict[str, Any]:
    """Fetch the full entry-point payload for one batch.

    Resolves `batch_id`'s unit_ids against batch_queue.json, then returns
    the matching units' complete entry-point detail from code_index.json.
    This is the ONLY call a batched Task Agent subagent needs to see its own
    scoped work — call it first, using only the batch_id you were handed by
    the orchestrator, so the full payload lands in your own fresh context
    rather than the orchestrator's.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch identifier handed to this subagent invocation.

    Returns:
        {"batch_id": str, "units": [{"unit_id", "unit_name", "entry_points": [...]}]}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue = _read_json(workspace_dir / BATCH_QUEUE_FILENAME)
    index = _read_json(workspace_dir / CODE_INDEX_FILENAME)

    batch = _find_batch(queue, batch_id)

    units_by_id = {unit["unit_id"]: unit for unit in index.get("units", [])}
    matched_units = []
    for unit_id in batch["unit_ids"]:
        unit = units_by_id.get(unit_id)
        if unit is None:
            raise BatchQueueError(
                f"Unit '{unit_id}' referenced by batch '{batch_id}' was not found in {CODE_INDEX_FILENAME}."
            )
        matched_units.append(unit)

    return {"batch_id": batch_id, "units": matched_units}


@tool_safe
def allocate_task_ids(project_name: str, batch_id: str, count: int) -> dict[str, Any]:
    """Atomically reserve a block of globally-unique, sequential TASK-IDs.

    Every batch is investigated independently and never sees another
    batch's output, so no batch can safely invent its own numbering
    starting at 1 — every batch doing that would produce duplicate
    TASK-001, TASK-002, ... IDs once `merge_task_batches` concatenates all
    of them into one document. Call this exactly once you know the final
    number of tasks your batch will produce (after investigation, before
    writing your partial file), and use the returned IDs, in that order,
    for your tasks — never number tasks yourself.

    Reads and increments a single shared counter (`next_task_id` in
    `batch_queue.json`) under the same file lock used for all other queue
    mutations, so concurrent or sequential calls from different batches
    never receive overlapping ranges.

    If a batch calls this, then fails and is retried, the IDs from the
    failed attempt are never reused — they simply become permanent gaps in
    the sequence. This is expected and harmless: uniqueness across the
    final document is what matters, not a gap-free sequence.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch identifier this allocation is for (used only
            for traceability; the counter itself is global, not per-batch).
        count: The exact number of TASK-IDs to reserve. Must be a positive
            integer no greater than the number of tasks you actually wrote.

    Returns:
        {"batch_id": str, "task_ids": ["TASK-001", "TASK-002", ...]} — a
        list of exactly `count` IDs, in ascending order, formatted as
        `TASK-{n:03d}`.
    """
    if count <= 0:
        raise BatchQueueError(f"count must be a positive integer, got {count}.")

    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        # Defaults to 1 for queues written before this counter existed.
        next_task_id = queue.get("next_task_id", 1)
        task_ids = [f"TASK-{n:03d}" for n in range(next_task_id, next_task_id + count)]
        queue["next_task_id"] = next_task_id + count
        _atomic_write_json(queue_path, queue)

    return {"batch_id": batch_id, "task_ids": task_ids}


@tool_safe
def mark_batch_complete(project_name: str, batch_id: str, task_ids: list[str]) -> dict[str, Any]:
    """Mark a batch as successfully completed.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch that finished successfully.
        task_ids: The TASK-IDs written to this batch's tasks_partial/{batch_id}.md file.

    Returns:
        {"batch_id": str, "status": "done"}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        batch = _find_batch(queue, batch_id)
        batch["status"] = "done"
        batch["task_ids_written"] = list(task_ids)
        batch["last_error"] = None
        _atomic_write_json(queue_path, queue)

    return {"batch_id": batch_id, "status": "done"}


@tool_safe
def mark_batch_failed(project_name: str, batch_id: str, error: str) -> dict[str, Any]:
    """Record a batch failure and decide whether it will be retried.

    Increments the batch's attempt counter. While attempts remain (fewer
    than 3 total), the batch is reset to "pending" so
    `get_next_pending_batch` will hand it out again later. Once attempts
    reach 3, the batch is marked "failed_permanent" and will no longer be
    retried automatically.

    Args:
        project_name: The exact indexed project name.
        batch_id: The batch that failed.
        error: A short description of what went wrong.

    Returns:
        {"batch_id": str, "status": "pending" | "failed_permanent", "attempts": int, "will_retry": bool}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue_path = workspace_dir / BATCH_QUEUE_FILENAME

    with _FileLock(queue_path):
        queue = _read_json(queue_path)
        batch = _find_batch(queue, batch_id)
        batch["attempts"] = batch.get("attempts", 0) + 1
        batch["last_error"] = error
        will_retry = batch["attempts"] < MAX_ATTEMPTS
        batch["status"] = "pending" if will_retry else "failed_permanent"
        _atomic_write_json(queue_path, queue)

        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "attempts": batch["attempts"],
            "will_retry": will_retry,
        }


@tool_safe
def get_batch_queue_status(project_name: str) -> dict[str, Any]:
    """Return batch counts grouped by status, for progress checks.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {"pending": int, "in_progress": int, "done": int, "failed_permanent": int, "total": int}
    """
    workspace_dir = resolve_workspace_dir(project_name)
    queue = _read_json(workspace_dir / BATCH_QUEUE_FILENAME)

    counts = {"pending": 0, "in_progress": 0, "done": 0, "failed_permanent": 0}
    for batch in queue.get("batches", []):
        status = batch.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1

    counts["total"] = sum(counts.values())
    return counts


@tool_safe
def merge_task_batches(project_name: str) -> dict[str, Any]:
    """Deterministically merge every completed batch's partial file into TASKS.md.

    Reads `batch_queue.json` in queue order (the same order units were
    discovered by the Code Index Agent) and, for every batch with status
    "done", reads its `/workspace/tasks_partial/{batch_id}.md` file. Each
    partial file is already a flat sequence of TASK-{id} entries from Stage
    C, so ordering is the only thing this step needs to get right — it just
    concatenates them in that order and writes the result to
    `/workspace/TASKS.md`.

    Also persists the merged document via `save_markdown_document` itself:
    by this point the complete content is already in hand, so doing the
    persistence here avoids a redundant read-then-save LLM turn.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {
            "total_batches_merged": int,
            "total_bytes": int,
            "skipped_batches": [{"batch_id": str, "status": str}, ...],
            "persisted": str | None,
        }
        `skipped_batches` lists any batch that was not status "done" (e.g.
        still "pending"/"in_progress", or "failed_permanent") or whose
        partial file was unexpectedly missing — call `get_batch_queue_status`
        first to confirm none remain pending/in_progress before calling this,
        so any entries here should only ever be "failed_permanent" batches.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    partial_dir = workspace_dir / TASKS_PARTIAL_DIRNAME
    queue = _read_json(workspace_dir / BATCH_QUEUE_FILENAME)

    sections: list[str] = []
    skipped: list[dict[str, str]] = []

    for batch in queue.get("batches", []):
        batch_id = batch["batch_id"]
        if batch.get("status") != "done":
            skipped.append({"batch_id": batch_id, "status": batch.get("status", "unknown")})
            continue

        partial_path = partial_dir / f"{batch_id}.md"
        if not partial_path.exists():
            skipped.append({"batch_id": batch_id, "status": "done_but_partial_file_missing"})
            continue

        content = partial_path.read_text(encoding="utf-8").strip()
        if content:
            sections.append(content)

    merged_content = ("\n\n".join(sections)).strip()

    tasks_path = workspace_dir / MERGED_TASKS_FILENAME
    tasks_path.write_text(f"{merged_content}\n" if merged_content else "", encoding="utf-8")

    result: dict[str, Any] = {
        "total_batches_merged": len(sections),
        "total_bytes": len(merged_content.encode("utf-8")),
        "skipped_batches": skipped,
        "persisted": None,
    }

    if merged_content:
        result["persisted"] = save_markdown_document(project_name, "tasks", merged_content)

    return result


# Full set, for wiring convenience.
BATCH_QUEUE_TOOLS = [
    build_batch_queue,
    get_next_pending_batch,
    get_batch_details,
    allocate_task_ids,
    mark_batch_complete,
    mark_batch_failed,
    get_batch_queue_status,
    merge_task_batches,
]

# The orchestrator drives the loop and marks outcomes, but must never see the
# full per-batch entry-point payload (that belongs only in the Task Agent
# subagent's own context) — so get_batch_details is intentionally excluded.
ORCHESTRATOR_BATCH_QUEUE_TOOLS = [
    build_batch_queue,
    get_next_pending_batch,
    mark_batch_complete,
    mark_batch_failed,
    get_batch_queue_status,
    merge_task_batches,
]

# The Code Index Agent only ever needs to trigger batching once, right after
# it writes code_index.json.
CODE_INDEX_AGENT_BATCH_QUEUE_TOOLS = [build_batch_queue]

# The batched Task Agent subagent needs to fetch its own scoped payload and
# reserve globally-unique TASK-IDs for what it writes; marking outcomes is
# the orchestrator's job (see design notes in prompts/orchestrator.py).
TASK_AGENT_BATCH_QUEUE_TOOLS = [get_batch_details, allocate_task_ids]
