from prompts.epics_subagent import get_epics_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.markdown_file_tools import MARKDOWN_FILE_TOOLS
from config import standard_llm_model

def create_epics_agent():
    return {
        "name": "epics_agent",
        "description": "A agent that creates epics for the project",
        "system_prompt": get_epics_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + MARKDOWN_FILE_TOOLS,
    }