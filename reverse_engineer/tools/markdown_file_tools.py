"""Tools for saving generated reverse-engineering documents as Markdown."""

from typing import Literal

from config import output_dir
from utils.naming import safe_directory_name
from utils.tool_safety import tool_safe
from utils.workspace import resolve_workspace_dir


DocumentType = Literal["tasks", "user_stories", "features", "epics", "architecture"]

_DOCUMENT_FILENAMES: dict[DocumentType, str] = {
    "tasks": "TASKS.md",
    "user_stories": "USER_STORIES.md",
    "features": "FEATURES.md",
    "epics": "EPICS.md",
    "architecture": "ARCHITECTURE.md",
}


@tool_safe
def save_markdown_document(
    project_name: str,
    document_type: DocumentType,
    content: str,
) -> str:
    """Save a completed reverse-engineering document as a Markdown file.

    Args:
        project_name: Name of the project being documented.
        document_type: One of tasks, user_stories, features, epics, or architecture.
        content: The complete Markdown document to save.

    Returns:
        A message containing the absolute path of the saved file.
    """
    if document_type not in _DOCUMENT_FILENAMES:
        allowed = ", ".join(_DOCUMENT_FILENAMES)
        raise ValueError(f"Unsupported document type. Use one of: {allowed}")

    content = content.strip()
    if not content:
        raise ValueError("Markdown content cannot be empty.")

    project_directory = output_dir / safe_directory_name(project_name, default="unnamed-project")
    project_directory.mkdir(parents=True, exist_ok=True)

    output_path = project_directory / _DOCUMENT_FILENAMES[document_type]
    output_path.write_text(f"{content}\n", encoding="utf-8")
    return f"Markdown document saved to {output_path.resolve()}"


@tool_safe
def persist_workspace_document(
    project_name: str,
    document_type: DocumentType,
) -> str:
    """Persist an already-written `/workspace/<NAME>.md` document as final output.

    Reads the document straight from the project's `/workspace/` directory —
    the same file the responsible agent already wrote and verified with the
    filesystem tools — and saves it via `save_markdown_document` itself. Use
    this instead of retyping the full document as the `content` argument:
    re-emitting an entire multi-hundred-line document as a second LLM output
    risks the model summarizing or truncating it instead of repeating it
    verbatim (this is why `merge_task_batches` reads and persists tasks
    itself rather than asking the Task Agent to retransmit them).

    Args:
        project_name: The exact indexed project name.
        document_type: One of user_stories, features, epics, or architecture.
            The corresponding `/workspace/<NAME>.md` file must already exist
            and be non-empty.

    Returns:
        A message containing the absolute path of the saved file.
    """
    if document_type not in _DOCUMENT_FILENAMES:
        allowed = ", ".join(_DOCUMENT_FILENAMES)
        raise ValueError(f"Unsupported document type. Use one of: {allowed}")

    workspace_path = resolve_workspace_dir(project_name) / _DOCUMENT_FILENAMES[document_type]
    if not workspace_path.exists():
        raise ValueError(f"Expected workspace file not found: {workspace_path}")

    content = workspace_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Workspace file is empty: {workspace_path}")

    return save_markdown_document(project_name, document_type, content)


MARKDOWN_FILE_TOOLS = [save_markdown_document]
WORKSPACE_PERSIST_TOOLS = [persist_workspace_document]
