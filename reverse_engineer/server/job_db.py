"""Direct write-back of job completion status into the caller's Postgres.

This service is not the source of truth for job records (see
ASDLC_INTEGRATION_PLAN.md) -- whatever calls this service (e.g.
asdlc-assistant's backend) owns the `code_analysis_jobs` row, created
*before* calling this service's `POST /jobs`. This module only ever
UPDATEs an existing row when a job finishes; it never INSERTs one. If no
row exists for a `job_id` (e.g. this service was called directly, not
through that integration), the UPDATE just matches zero rows -- not an
error.

DATABASE_URL unset (the default) means this is a silent no-op, exactly
like `nats_publisher.py` -- this service works standalone with no database
configured at all. A write failure here is logged and swallowed, never
allowed to mask this job's own real outcome (already recorded via NATS's
`end` event by the time this runs).
"""

from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["active", "complete", "failed"]

# Mirrors the mapping the old get_job() used internally, before the
# job-record surface moved out of this service.
_STATUS_MAP: dict[str, JobStatus] = {
    "completed": "complete",
    "failed": "failed",
    "stopped": "failed",
}


def _configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def mark_job_complete(job_id: str, internal_status: str) -> None:
    """UPDATE code_analysis_jobs.status for this job. Never raises."""
    if not _configured():
        return

    status = _STATUS_MAP.get(internal_status)
    if status is None:
        logger.warning("Unknown internal status %r for job %s -- not writing to DB.", internal_status, job_id)
        return

    try:
        import psycopg  # local import: keep psycopg an optional dependency path

        async with await psycopg.AsyncConnection.connect(
            os.environ["DATABASE_URL"], autocommit=True, connect_timeout=5
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE code_analysis_jobs SET status = %s, updated_at = NOW() WHERE job_id = %s",
                    (status, job_id),
                )
                if cur.rowcount == 0:
                    logger.info("No code_analysis_jobs row for job %s -- nothing to update.", job_id)
    except Exception as exc:
        logger.warning("Could not write job completion to DB [job=%s]: %s", job_id, exc)
