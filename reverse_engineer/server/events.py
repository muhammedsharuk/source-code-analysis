"""Job/DTO models used by the job API (see server/app.py) and by the
step-progress shape published to NATS (see nats_publisher.py / event_handler.py).

There is no live-log or job-detail model here anymore: a job's progress is
published to NATS as it happens, not queried back from this service -- see
ASDLC_INTEGRATION_PLAN.md.
"""

from typing import Literal, Optional

from pydantic import BaseModel

LogLevel = Literal["INFO", "SUCCESS", "DEBUG", "WARN", "ANALYZING", "ERROR"]
StepStatus = Literal["complete", "active", "pending"]
StepId = Literal["index", "code-index", "tasks", "docs"]
JobStatusValue = Literal["active", "complete", "failed"]


class AnalysisStep(BaseModel):
    id: StepId
    label: str
    description: str
    status: StepStatus
    progress: int


class JobSummary(BaseModel):
    job_id: str
    name: str
    status: JobStatusValue


class FileNode(BaseModel):
    name: str
    path: str
    type: Literal["file", "folder"]
    children: Optional[list["FileNode"]] = None


def default_steps() -> list[AnalysisStep]:
    """The 4 macro-steps `ExecutionPage` renders, all starting pending.

    These collapse the orchestrator prompt's 9 pipeline stages: `index` is
    Stage 1, `code-index` is Stage 2, `tasks` is Stage 3, and `docs` covers
    Stages 5-9 (user stories, features, epics, architecture).
    """
    return [
        AnalysisStep(
            id="index",
            label="Repository Indexing",
            description="Indexing the repository into the knowledge graph.",
            status="pending",
            progress=0,
        ),
        AnalysisStep(
            id="code-index",
            label="Building Code Index",
            description="Seeding the checklist and resolving structural units by batch.",
            status="pending",
            progress=0,
        ),
        AnalysisStep(
            id="tasks",
            label="Generating Tasks",
            description="Tracing each workflow end-to-end and documenting it.",
            status="pending",
            progress=0,
        ),
        AnalysisStep(
            id="docs",
            label="Generating Documentation",
            description="User stories, features, epics, and architecture overview.",
            status="pending",
            progress=0,
        ),
    ]
