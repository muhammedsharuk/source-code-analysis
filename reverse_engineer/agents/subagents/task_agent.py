from prompts.task_subagent import get_task_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.batch_queue_tools import TASK_AGENT_BATCH_QUEUE_TOOLS
from config import standard_llm_model

def create_task_agent():
    return {
        "name": "task_agent",
        "description": (
            "An agent that documents implementation tasks for ONE batch of the "
            "repository. Must be delegated only a project_name and batch_id; it "
            "fetches its own scoped work via get_batch_details."
        ),
        "system_prompt": get_task_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + TASK_AGENT_BATCH_QUEUE_TOOLS,
        "skills": ["/skills/"],
    }
