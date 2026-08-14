"""Shared helper for resolving the physical `/workspace/` directory for a project.

Extracted from `tools/batch_queue_tools.py` so that `tools/markdown_file_tools.py`
can resolve the same directory without importing `batch_queue_tools` (which
itself imports `markdown_file_tools`, so that direction would be circular).
"""

import json
from pathlib import Path

from utils.naming import safe_directory_name

_current_file = Path(__file__).resolve()
_reverse_engineer_root = _current_file.parent.parent
TEMP_ROOT = _reverse_engineer_root / "temp"

CODE_INDEX_FILENAME = "code_index.json"
CHECKLIST_FILENAME = "code_index_checklist.json"


def _find_by_index_lookup(project_name: str, fallback: Path) -> Path:
    if not TEMP_ROOT.exists():
        return fallback
    for child in TEMP_ROOT.iterdir():
        if not child.is_dir():
            continue
        index_path = child / CODE_INDEX_FILENAME
        if not index_path.exists():
            continue
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("project_name") == project_name:
            return child
    return fallback


def resolve_workspace_dir(project_name: str) -> Path:
    """Resolve the physical `/workspace/` directory for this project.

    Mirrors the orchestrator's `/workspace/` filesystem route (rooted at
    `temp/<safe_directory_name(repo_path)>/` at agent-creation time, see
    `agents/orchestrator.py`). Falls back to scanning `temp/` for a directory
    whose `code_index.json` records a matching `project_name`, for the rare
    case where `project_name` diverges from the sanitized repo path.
    """
    primary = TEMP_ROOT / safe_directory_name(project_name, default="unnamed-repo")
    if primary.is_dir():
        return primary
    return _find_by_index_lookup(project_name, fallback=primary)
