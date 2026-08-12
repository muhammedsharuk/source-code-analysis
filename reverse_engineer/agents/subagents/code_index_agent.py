from prompts.code_index_subagent import get_code_index_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.batch_queue_tools import CODE_INDEX_AGENT_BATCH_QUEUE_TOOLS
from config import standard_llm_model

def create_code_index_agent():
    return {
        "name": "code_index_agent",
        "description": (
            "An agent that builds a mechanical structural index of the repository "
            "(coverage units and their entry points) and triggers batching for the "
            "Task Agent. Does not investigate behavior or write TASKS.md."
        ),
        "system_prompt": get_code_index_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + CODE_INDEX_AGENT_BATCH_QUEUE_TOOLS,
        "skills": ["/skills/"],
    }
