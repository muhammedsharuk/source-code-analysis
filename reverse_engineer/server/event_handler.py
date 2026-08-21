"""Translates the orchestrator's LangGraph event stream into frontend-facing events.

Adapted from a reference implementation built for a different (SQS-backed,
chat-oriented) project. What transfers directly, because it's LangGraph/
deepagents framework behavior rather than anything product-specific:

- The event names themselves (`on_tool_start`, `on_tool_end`, `on_tool_error`)
  are LangChain's own callback event vocabulary from `astream_events`.
- Detecting a subagent dispatch via `tool_name == "task"` and reading
  `subagent_type` off the tool's input — deepagents' built-in `task` tool
  (`deepagents/middleware/subagents.py`) works exactly this way regardless
  of product.
- Unwrapping a subagent's result from the `Command` object the `task` tool
  returns (`Command.update["messages"][-1].content`) — also confirmed
  directly against that same module.

What does NOT transfer, and was rebuilt for this pipeline specifically:

- There's no SQS here — results are handed to `RunManager.publish*`, which
  fans them out to live SSE subscribers and appends them to a per-run
  on-disk log (see `run_store.py`).
- The subagent output contract is different, and the log is deliberately
  quiet about most of it. `index_batch_agent`/`task_agent` each run dozens
  of times per pipeline run (`_BATCH_SUBAGENTS` below) — the orchestrator's
  own batch-queue tools already retry a failed one silently, so neither a
  dispatch nor a completion of either is narrated on its own; only the step
  progress bar reflects that movement. The other four subagents (user
  stories, feature, epics, architecture) run once each and don't return
  structured JSON at all — the orchestrator confirms their success by
  checking the workspace file they wrote, not by parsing their reply — and
  each of those does get a friendly one-line narration (`_DOC_SUBAGENT_MESSAGES`)
  since each is a real, user-meaningful milestone.
- There's no `on_chain_start`/`on_chain_end` handling for a "final assistant
  response" — that's chat-specific plumbing for detecting when a model
  finished answering. This orchestrator isn't a chat model producing one
  answer; it's a long batch pipeline. "Done" here is simply detected by
  `astream_events` running to completion (see `run_manager.py`), so only
  tool-level events carry meaningful signal and are handled here.
- The tool → message/step mapping table below is specific to this
  pipeline's actual deterministic tools and subagents (`seed_checklist`,
  the batch-queue tools, the 7 registered subagents), verified against
  their real docstrings/return shapes in `tools/*.py`, not guessed.
"""

import ast
import json
import uuid
from datetime import datetime
from typing import Any, Protocol

from .events import LogLevel, default_steps

# deepagents' own built-in filesystem/bookkeeping tools. Subagents use these
# constantly for workspace handoff; surfacing every read/write as a log line
# would drown out the meaningful pipeline events below.
SKIP_TOOLS = {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "write_todos"}


# `index_batch_agent`/`task_agent` each run dozens of times per pipeline run
# (once per index/task batch — 22 and 44 respectively on a real repo), so
# narrating every dispatch/completion would drown the log in near-identical
# lines that tell a non-technical reader nothing beyond what the step
# progress bar already shows. They're intentionally silent in the log; see
# `_on_tool_start`/`_handle_task_end`.
_BATCH_SUBAGENTS = {"index_batch_agent", "task_agent"}

# The other four subagents each run exactly once per pipeline run and each
# completing is a real, user-meaningful milestone — these get a friendly
# dispatch/completion line. `code_index_agent` is registered but currently
# unused by the orchestrator prompt (see agents/orchestrator.py), so it has
# no dispatch/completion narration of its own.
_DOC_SUBAGENT_MESSAGES: dict[str, dict[str, str]] = {
    "user_stories_agent": {
        "start": "Writing user stories from what the app actually does...",
        "done": "User stories are ready.",
    },
    "feature_agent": {
        "start": "Grouping the work into features...",
        "done": "Feature list is ready.",
    },
    "epics_agent": {
        "start": "Organizing the features into larger epics...",
        "done": "Epics are ready.",
    },
    "architecture_agent": {
        "start": "Writing up the system's architecture overview...",
        "done": "Architecture overview is ready.",
    },
}
_DOC_SUBAGENTS = tuple(_DOC_SUBAGENT_MESSAGES)


class _RunManagerProtocol(Protocol):
    async def publish(self, run_id: str, logline: dict[str, Any]) -> None: ...
    async def publish_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None: ...
    async def publish_terminal(self, run_id: str, status: str, message: str) -> None: ...
    async def set_project_name(self, run_id: str, project_name: str) -> None: ...
    async def save_progress(self, run_id: str, progress: dict[str, Any]) -> None: ...


def _humanize(tool_name: str) -> str:
    return tool_name.replace("_", " ")


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _unwrap_task_command(tool_output: Any) -> str | None:
    """Pull the subagent's raw reply text out of the `Command` the `task` tool returns."""
    update = getattr(tool_output, "update", None)
    if not isinstance(update, dict):
        return None
    messages = update.get("messages") or []
    if not messages:
        return None
    content = getattr(messages[-1], "content", None)
    return content if isinstance(content, str) else None


def _unwrap_mcp_envelope(parsed: Any) -> Any:
    """Unwrap CodebaseMemoryCLI's `{"content": [{"type": "text", "text": "<json>"}]}` envelope.

    Every tool in `tools/codebase_memory_tools.py` (index_repository,
    index_status, search_graph, get_architecture, ...) goes through
    `CodebaseMemoryCLI._run()`, which always passes `--json` to the
    `codebase-memory-mcp` binary. Confirmed directly against a real run:
    with that flag, the binary's output is this MCP-style envelope, not the
    flat dict those tools' own docstrings describe — a manual CLI call
    without `--json` returns the flat dict; the exact same call with
    `--json` returns this envelope instead. `checklist_tools.py` already has
    to unwrap this same shape for its own direct `search_graph` calls
    (`_unwrap_cli_result`); this generalizes that to any tool that produces
    it, since every CodebaseMemoryCLI-backed tool shares the same envelope.
    """
    if not isinstance(parsed, dict):
        return parsed
    content = parsed.get("content")
    if not (isinstance(content, list) and content and isinstance(content[0], dict) and "text" in content[0]):
        return parsed
    try:
        return json.loads(content[0]["text"])
    except (ValueError, TypeError):
        return parsed


def _serialize_tool_output(tool_output: Any) -> Any:
    """Best-effort conversion of a non-`task` tool's return value into a plain dict/value."""
    if tool_output is None:
        return None
    raw = getattr(tool_output, "content", tool_output)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            pass
        else:
            return _unwrap_mcp_envelope(parsed)
        # Last-resort fallback for a tool's content coming through as
        # Python-literal syntax (e.g. single-quoted repr) rather than JSON
        # or the envelope above — safe parse, no arbitrary code execution.
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError):
            return raw
    return raw


# Plain-language narration for our actual tools, verified against each
# tool's real docstring/return shape in tools/*.py — reworded so a
# non-technical reader never has to see words like "batch", "checklist",
# "agent", "node/edge", or "all_clear". `end` receives (output_dict,
# input_dict). Tools not listed here, and the four per-batch bookkeeping
# tools listed in `_SILENT_PROGRESS_TOOLS` below, stay out of the log
# entirely — only SKIP_TOOLS are hidden from *processing*, but these are
# hidden from the *log* specifically because at 20-100+ batches per run
# they'd be pure noise; the step progress bar already shows this movement.
_TOOL_MESSAGES: dict[str, dict[str, Any]] = {
    "index_repository": {
        "start": "Reading through the repository...",
        "level": "INFO",
        "end": lambda out, inp: (
            f"Finished scanning the codebase ({out.get('nodes', 'many')} pieces of code found)."
            if not out.get("error")
            else f"Ran into a problem scanning the repository: {out.get('error')}"
        ),
        "end_level": "SUCCESS",
    },
    "seed_checklist": {
        "start": "Mapping out the codebase's structure...",
        "level": "INFO",
        "end": lambda out, inp: f"Found {out.get('total_entries', 'several')} areas of the codebase to analyze.",
        "end_level": "SUCCESS",
    },
    "merge_index_batches": {
        "end": lambda out, inp: (
            f"Finished analyzing {out.get('total_units', 'the')} components across the codebase in detail."
        ),
        "end_level": "SUCCESS",
    },
    "requeue_index_batch_from_problems": {
        "start": "Double-checking nothing in the codebase was missed...",
        "level": "INFO",
        "end": lambda out, inp: (
            "Double-check complete — everything's accounted for."
            if out.get("all_clear")
            else (
                "Found a few areas to take another look at — re-analyzing them now."
                if out.get("requeued")
                else "A few parts of the codebase couldn't be fully analyzed automatically."
            )
        ),
        "end_level": "SUCCESS",
    },
    "build_batch_queue": {
        "end": lambda out, inp: "Code index complete. Now tracing how each part of the app works...",
        "end_level": "INFO",
    },
    "merge_task_batches": {
        "end": lambda out, inp: "Finished documenting every workflow in the app.",
        "end_level": "SUCCESS",
    },
}

# Purely internal bookkeeping — fires once per index/task batch (dozens of
# times on a real repo). Progress still needs to be tracked from these (see
# `_update_progress_for_tool`), but they never reach the log.
_SILENT_PROGRESS_TOOLS = {
    "index_status",
    "build_index_batch_queue",
    "mark_index_batch_complete",
    "mark_index_batch_failed",
    "mark_batch_complete",
    "mark_batch_failed",
}


class EventHandler:
    """Consumes one `astream_events` event at a time for a single run."""

    def __init__(self, run_id: str, run_manager: _RunManagerProtocol) -> None:
        self._run_id = run_id
        self._run_manager = run_manager
        self._steps = self._default_steps()
        self._index_total = 0
        self._index_done = 0
        self._task_total = 0
        self._task_done = 0
        self._docs_done: set[str] = set()
        self.project_name: str | None = None

    @staticmethod
    def _default_steps() -> list[dict[str, Any]]:
        return [s.model_dump() for s in default_steps()]

    def dump_progress(self) -> dict[str, Any]:
        return {
            "index_total": self._index_total,
            "index_done": self._index_done,
            "task_total": self._task_total,
            "task_done": self._task_done,
            "docs_done": sorted(self._docs_done),
            "project_name": self.project_name,
        }

    async def handle_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "on_tool_start":
            await self._on_tool_start(event)
        elif name == "on_tool_end":
            await self._on_tool_end(event)
        elif name == "on_tool_error":
            await self._on_tool_error(event)

    async def _on_tool_start(self, event: dict[str, Any]) -> None:
        tool_name = event.get("name")
        if tool_name in SKIP_TOOLS:
            return
        tool_input = event.get("data", {}).get("input", {}) or {}

        if tool_name == "task":
            subagent_type = tool_input.get("subagent_type")
            if subagent_type in _BATCH_SUBAGENTS:
                return
            spec = _DOC_SUBAGENT_MESSAGES.get(subagent_type)
            if spec:
                await self._emit("INFO", spec["start"])
            return

        if tool_name in _SILENT_PROGRESS_TOOLS:
            return

        spec = _TOOL_MESSAGES.get(tool_name)
        if spec and spec.get("start"):
            await self._emit(spec.get("level", "INFO"), spec["start"])

    async def _on_tool_end(self, event: dict[str, Any]) -> None:
        tool_name = event.get("name")
        if tool_name in SKIP_TOOLS:
            return
        tool_output = event.get("data", {}).get("output")
        tool_input = event.get("data", {}).get("input", {}) or {}

        if tool_name == "task":
            await self._handle_task_end(tool_input, tool_output)
            return

        serialized = _serialize_tool_output(tool_output)
        data = serialized if isinstance(serialized, dict) else {}

        # tool_safe (utils/tool_safety.py) catches exceptions in our own
        # deterministic tools and returns them as {"ok": False, ...} rather
        # than raising, so a real failure here won't reach on_tool_error.
        if data.get("ok") is False and tool_name not in _SILENT_PROGRESS_TOOLS:
            await self._emit(
                "ERROR",
                f"Something went wrong ({_humanize(tool_name)}): {data.get('error_message', 'unknown error')}",
            )
            await self._update_progress_for_tool(tool_name, data)
            return

        if tool_name not in _SILENT_PROGRESS_TOOLS:
            spec = _TOOL_MESSAGES.get(tool_name)
            if spec and spec.get("end"):
                try:
                    message = spec["end"](data, tool_input)
                except Exception:
                    message = None
                if message:
                    await self._emit(spec.get("end_level", "SUCCESS"), message)

        await self._update_progress_for_tool(tool_name, data)

    async def _on_tool_error(self, event: dict[str, Any]) -> None:
        tool_name = event.get("name")
        if tool_name in SKIP_TOOLS:
            return
        err = (event.get("data") or {}).get("error")
        message = str(err) if err is not None else "unknown error"

        if tool_name == "task":
            tool_input = event.get("data", {}).get("input", {}) or {}
            subagent_type = tool_input.get("subagent_type")
            area = _DOC_SUBAGENT_MESSAGES.get(subagent_type, {}).get("done", "part of the analysis")
            await self._emit("ERROR", f"Ran into a problem while working on: {area}")
            return

        await self._emit("ERROR", f"Something went wrong ({_humanize(tool_name)}).")

    async def _handle_task_end(self, tool_input: dict[str, Any], tool_output: Any) -> None:
        subagent_type = tool_input.get("subagent_type")

        # Each of these runs dozens of times per pipeline run; a failed one
        # is silently retried by the orchestrator's own batch-queue logic
        # (see mark_index_batch_failed/mark_batch_failed), so neither its
        # success nor its failure is worth narrating on its own — only the
        # step progress bar reflects it.
        if subagent_type in _BATCH_SUBAGENTS:
            return

        spec = _DOC_SUBAGENT_MESSAGES.get(subagent_type)
        if not spec:
            return

        text = _unwrap_task_command(tool_output)
        if text is None:
            await self._emit("WARN", f"Ran into a snag while working on: {spec['done']}")
            return

        await self._emit("SUCCESS", spec["done"])

        self._docs_done.add(subagent_type)
        progress = round(len(self._docs_done) / len(_DOC_SUBAGENTS) * 100)
        status = "complete" if progress >= 100 else "active"
        await self._set_step_status("docs", status, progress)
        await self._run_manager.save_progress(self._run_id, self.dump_progress())

    async def _update_progress_for_tool(self, tool_name: str, data: dict[str, Any]) -> None:
        if tool_name in ("index_repository", "index_status") and data.get("project"):
            self.project_name = data["project"]
            await self._run_manager.set_project_name(self._run_id, self.project_name)

        if tool_name == "index_repository" and not data.get("error"):
            await self._set_step_status("index", "complete", 100)

        elif tool_name == "build_index_batch_queue":
            self._index_total = data.get("total_batches") or self._index_total
            await self._set_step_status("code-index", "active", self._code_index_progress())

        elif tool_name == "mark_index_batch_complete":
            if data.get("accepted") or data.get("status") == "failed_permanent":
                self._index_done += 1
            await self._set_step_status("code-index", "active", self._code_index_progress())

        elif tool_name == "mark_index_batch_failed":
            if data.get("status") == "failed_permanent":
                self._index_done += 1
            await self._set_step_status("code-index", "active", self._code_index_progress())

        elif tool_name == "build_batch_queue":
            await self._set_step_status("code-index", "complete", 100)
            self._task_total = data.get("total_batches") or self._task_total
            await self._set_step_status("tasks", "active", self._task_progress())

        elif tool_name == "mark_batch_complete":
            self._task_done += 1
            await self._set_step_status("tasks", "active", self._task_progress())

        elif tool_name == "mark_batch_failed":
            if data.get("status") == "failed_permanent":
                self._task_done += 1
            await self._set_step_status("tasks", "active", self._task_progress())

        elif tool_name == "merge_task_batches":
            await self._set_step_status("tasks", "complete", 100)
            await self._set_step_status("docs", "active", 0)

        await self._run_manager.save_progress(self._run_id, self.dump_progress())

    def _code_index_progress(self) -> int:
        if not self._index_total:
            return 0
        return min(100, round(self._index_done / self._index_total * 100))

    def _task_progress(self) -> int:
        if not self._task_total:
            return 0
        return min(100, round(self._task_done / self._task_total * 100))

    async def _set_step_status(self, step_id: str, status: str, progress: int) -> None:
        for step in self._steps:
            if step["id"] == step_id:
                step["status"] = status
                step["progress"] = progress
                break
        await self._run_manager.publish_steps(self._run_id, self._steps)

    async def _emit(self, level: LogLevel, message: str) -> None:
        line = {"id": uuid.uuid4().hex, "timestamp": _now_hms(), "level": level, "message": message}
        await self._run_manager.publish(self._run_id, line)

    async def emit_terminal(self, status: str, message: str) -> None:
        await self._run_manager.publish_terminal(self._run_id, status, message)
