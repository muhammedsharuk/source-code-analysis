from prompts.feature_subagent import get_feature_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.markdown_file_tools import MARKDOWN_FILE_TOOLS
from config import standard_llm_model

def create_feature_agent():
    return {
        "name": "feature_agent",
        "description": "A agent that groups user stories into named features for the project",
        "system_prompt": get_feature_subagent_prompt(),
        "model": standard_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + MARKDOWN_FILE_TOOLS,
    }
