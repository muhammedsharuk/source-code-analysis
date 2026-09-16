"""Publishes job progress to NATS, if configured.

Subject scheme: `stream.<job_id>.<event_type>` -- deliberately matches
asdlc-assistant's own convention (`stream.<thread_id>.<run_id>.<eventName>`,
confirmed against their `server.py`/`streaming_v3.py`) closely enough that
publishing here lands in their existing `AGENT_STREAMS` JetStream stream
(`subjects: ["stream.>"]`) with no new stream to configure -- just without
the redundant run_id segment, since this service has no multi-run-per-job
concept the way their chat threads do (one job, one run).

Event types are this service's own, not asdlc-assistant's chat-message
vocabulary (`text_delta`, `tool_call_delta`, ...) -- there is no chat here,
so forcing this pipeline's progress into that shape would fit nothing. See
ASDLC_INTEGRATION_PLAN.md for the reasoning:
  - `log`      -- one of this pipeline's existing friendly log lines, e.g.
                  {id, timestamp, level, message} (see event_handler.py's
                  `_emit`).
  - `step_progress` -- the 4-step/percent progress array ({steps: [...]}),
                  the same shape as `events.AnalysisStep`.
  - `end`      -- the run finished (status: completed/failed/stopped).

NATS_URL unset (the default) means publishing is a silent no-op -- this
service works standalone without any NATS server at all. When it *is*
configured, a publish failure is swallowed and logged, never allowed to
break the actual pipeline run: a dropped progress event is a much smaller
problem than an aborted analysis.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_nc: Any = None
_connect_failed = False


def _configured() -> bool:
    return bool(os.environ.get("NATS_URL"))


async def _connection() -> Any | None:
    """Lazy singleton connection. Returns None if unconfigured or unreachable."""
    global _nc, _connect_failed

    if not _configured():
        return None
    if _nc is not None:
        return _nc
    if _connect_failed:
        # Don't retry a connection on every single publish call if the
        # server was unreachable once -- that would turn a missing/down
        # NATS server into a repeated multi-second delay on every tool
        # call for the rest of the run. Reconnecting requires a process
        # restart; this is a service meant to run standalone without NATS
        # configured at all, not one that depends on it being reachable.
        return None

    try:
        import nats  # local import: keep `nats-py` an optional dependency path

        _nc = await nats.connect(
            os.environ["NATS_URL"],
            token=os.environ.get("NATS_AUTH_TOKEN") or None,
            connect_timeout=5,
            max_reconnect_attempts=-1,
        )
        logger.info("Connected to NATS at %s", os.environ["NATS_URL"])
        return _nc
    except Exception as exc:
        _connect_failed = True
        logger.warning("Could not connect to NATS (publishing disabled for this run): %s", exc)
        return None


async def publish_event(job_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Publish one event; never raises -- a failed publish must not abort the pipeline."""
    nc = await _connection()
    if nc is None:
        return
    try:
        payload = json.dumps(data, default=str).encode()
        await nc.publish(f"stream.{job_id}.{event_type}", payload)
    except Exception as exc:
        logger.warning("NATS publish failed [job=%s type=%s]: %s", job_id, event_type, exc)


async def close() -> None:
    global _nc
    if _nc is not None:
        try:
            await _nc.drain()
        except Exception:
            pass
        _nc = None
