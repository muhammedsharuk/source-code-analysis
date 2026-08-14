from prompts.index_batch_agent_subagent import get_index_batch_agent_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.index_batch_queue_tools import INDEX_BATCH_AGENT_TOOLS
from config import standard_llm_model

def create_index_batch_agent():
    return {
        "name": "index_batch_agent",
        "description": (
            "An agent that resolves ONE small batch of checklist paths into "
            "coverage units and entry points for the structural code index. "
            "Must be delegated only a project_name and batch_id; it fetches its "
            "own scoped work via get_index_batch_details."
        ),
        "system_prompt": get_index_batch_agent_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + INDEX_BATCH_AGENT_TOOLS,
        "skills": ["/skills/"],
    }
