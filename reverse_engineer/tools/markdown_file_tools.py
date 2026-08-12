"""Tools for saving generated reverse-engineering documents as Markdown."""

from typing import Literal

from config import output_dir
from utils.naming import safe_directory_name


DocumentType = Literal["tasks", "user_stories", "features", "epics", "architecture"]

_DOCUMENT_FILENAMES: dict[DocumentType, str] = {
    "tasks": "TASKS.md",
    "user_stories": "USER_STORIES.md",
    "features": "FEATURES.md",
    "epics": "EPICS.md",
    "architecture": "ARCHITECTURE.md",
}


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


MARKDOWN_FILE_TOOLS = [save_markdown_document]
