from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, CompositeBackend, StateBackend
from .subagents.architecture_agent import create_architecture_agent
from .subagents.code_index_agent import create_code_index_agent
from .subagents.epics_agent import create_epics_agent
from .subagents.feature_agent import create_feature_agent
from .subagents.task_agent import create_task_agent
from .subagents.user_stories_agent import create_user_stories_agent
from prompts.orchestrator import get_orchestrator_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.batch_queue_tools import ORCHESTRATOR_BATCH_QUEUE_TOOLS
from utils.naming import safe_directory_name
from config import standard_llm_model

subagents = [
    create_architecture_agent(),
    create_code_index_agent(),
    create_epics_agent(),
    create_feature_agent(),
    create_task_agent(),
    create_user_stories_agent(),
]

ORCHESTRATOR_TOOLS = CODEBASE_MEMORY_TOOLS + ORCHESTRATOR_BATCH_QUEUE_TOOLS

current_dir = Path(__file__).resolve()
parent_dir = current_dir.parent.parent
def create_orchestrator_agent(repo_path: str):
    """Create the orchestrator agent.

    `repo_path` scopes the `/workspace/` handoff directory to this repository
    (under `temp/<safe-repo-name>/`) so that running the pipeline against a
    different repository never overwrites another repository's intermediate
    TASKS.md / USER_STORIES.md / FEATURES.md / EPICS.md / ARCHITECTURE.md files.
    """
    workspace_dir = parent_dir / "temp" / safe_directory_name(repo_path, default="unnamed-repo")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agent = create_deep_agent(
        model=standard_llm_model,
        system_prompt=get_orchestrator_prompt(),
        subagents=subagents,
        tools=ORCHESTRATOR_TOOLS,
        backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/skills/": FilesystemBackend(root_dir=parent_dir/ "skills"),
            "/workspace/": FilesystemBackend(root_dir=workspace_dir),
        },
    )
    )
    return agent
