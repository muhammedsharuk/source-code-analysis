from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, CompositeBackend, StateBackend
from .subagents.architecture_agent import create_architecture_agent
from .subagents.code_index_agent import create_code_index_agent
from .subagents.epics_agent import create_epics_agent
from .subagents.feature_agent import create_feature_agent
from .subagents.index_batch_agent import create_index_batch_agent
from .subagents.task_agent import create_task_agent
from .subagents.user_stories_agent import create_user_stories_agent
from prompts.orchestrator import get_orchestrator_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.batch_queue_tools import ORCHESTRATOR_BATCH_QUEUE_TOOLS
from tools.checklist_tools import seed_checklist
from tools.index_batch_queue_tools import ORCHESTRATOR_INDEX_BATCH_QUEUE_TOOLS
from utils.naming import safe_directory_name
from config import large_llm_model

# create_code_index_agent (the old, single-shot Stage A agent) is kept
# registered but is no longer what the orchestrator prompt drives — Stage 2
# now uses the deterministic seed/batch-queue flow and create_index_batch_agent
# instead. It stays available here until a real run of the new path has been
# cross-checked against the real graph, so there is something to fall back
# to or compare against if the new path needs adjustment.
subagents = [
    create_architecture_agent(),
    create_code_index_agent(),
    create_epics_agent(),
    create_feature_agent(),
    create_index_batch_agent(),
    create_task_agent(),
    create_user_stories_agent(),
]

ORCHESTRATOR_TOOLS = (
    CODEBASE_MEMORY_TOOLS
    + ORCHESTRATOR_BATCH_QUEUE_TOOLS
    + ORCHESTRATOR_INDEX_BATCH_QUEUE_TOOLS
    + [seed_checklist]
)

current_dir = Path(__file__).resolve()
parent_dir = current_dir.parent.parent
def create_orchestrator_agent(repo_path: str):
    """Create the orchestrator agent.

    `repo_path` scopes the `/workspace/` handoff directory to this repository
    (under `temp/<safe-repo-name>/`) so that running the pipeline against a
    different repository never overwrites another repository's intermediate
    TASKS.md / USER_STORIES.md / FEATURES.md / EPICS.md / ARCHITECTURE.md files.

    Deliberately no `checkpointer`: a run's in-flight state isn't meant to
    survive a process restart (see `server/run_store.py`'s docstring — a
    prior version resumed runs this way and it produced duplicate concurrent
    runs in practice). Restarting the server abandons any run in progress;
    only its already-completed output is ever browsable afterwards (see
    `server/history_store.py`).
    """
    workspace_dir = parent_dir / "temp" / safe_directory_name(repo_path, default="unnamed-repo")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agent = create_deep_agent(
        model=large_llm_model,
        system_prompt=get_orchestrator_prompt(),
        subagents=subagents,
        tools=ORCHESTRATOR_TOOLS,
        # virtual_mode=True is required here, not optional: CompositeBackend
        # strips the "/workspace/" or "/skills/" prefix before delegating to
        # the routed FilesystemBackend, but leaves the leading "/" on the
        # remainder (e.g. "/workspace/index_partial/x.json" ->
        # "/index_partial/x.json"). With virtual_mode's old default (False),
        # FilesystemBackend treats that leading slash as a real OS-absolute
        # path and resolves it from the drive root, bypassing root_dir
        # entirely — confirmed directly: every Index Batch Agent write to
        # its own partial file was silently landing at
        # C:\index_partial\{batch_id}.json instead of
        # temp\<repo>\index_partial\{batch_id}.json, which is exactly why
        # mark_index_batch_complete kept reporting the file as missing (it
        # checks the real intended path, not wherever the write actually
        # went). virtual_mode=True makes every path resolve relative to
        # root_dir instead, which is what CompositeBackend routing requires.
        backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/skills/": FilesystemBackend(root_dir=parent_dir/ "skills", virtual_mode=True),
            "/workspace/": FilesystemBackend(root_dir=workspace_dir, virtual_mode=True),
        },
    )
    )
    return agent
