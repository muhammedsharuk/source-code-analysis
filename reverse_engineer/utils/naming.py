"""Shared helpers for turning external identifiers into safe directory names."""

import re


def safe_directory_name(value: str, default: str = "unnamed") -> str:
    """Convert an arbitrary string (repo path, project name, ...) into a safe directory name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or default
