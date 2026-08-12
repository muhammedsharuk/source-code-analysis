from prompts.architecture_subagent import get_architecture_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.markdown_file_tools import MARKDOWN_FILE_TOOLS
from config import standard_llm_model

def create_architecture_agent():
    return {
        "name": "architecture_agent",
        "description": "A agent that discovers the architecture of the project",
        "system_prompt": get_architecture_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + MARKDOWN_FILE_TOOLS,
        "skills":["/skills/"]
    }