from prompts.user_stories_subagent import get_user_stories_subagent_prompt
from tools.codebase_memory_tools import CODEBASE_MEMORY_TOOLS
from tools.markdown_file_tools import MARKDOWN_FILE_TOOLS, WORKSPACE_PERSIST_TOOLS
from config import large_llm_model

def create_user_stories_agent():
    return {
        "name": "user_stories_agent",
        "description": "A agent that creates user stories for the project",
        "system_prompt": get_user_stories_subagent_prompt(),
        "model": large_llm_model,
        "tools": CODEBASE_MEMORY_TOOLS + MARKDOWN_FILE_TOOLS + WORKSPACE_PERSIST_TOOLS,
    }