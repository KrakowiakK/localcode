"""
Path validation and ignore checks for tool handlers.
"""

import os
from pathlib import Path
from typing import Optional

from localcode.tool_handlers import _state
from localcode.tool_handlers._state import DEFAULT_IGNORE_DIRS


def _is_path_within_sandbox(path: str, sandbox_root: str) -> bool:
    try:
        resolved = os.path.realpath(path)
        sandbox_resolved = os.path.realpath(sandbox_root)
        return resolved == sandbox_resolved or resolved.startswith(sandbox_resolved + os.sep)
    except (OSError, ValueError):
        return False


def _validate_path(path: Optional[str], check_exists: bool = False) -> str:
    """
    Validate path is within sandbox (if enabled) and optionally exists.
    Returns canonical path (realpath if sandbox enabled, else abspath).
    """
    if not path:
        raise ValueError("path is required")

    abs_path = os.path.abspath(path)

    sandbox_root = _state.SANDBOX_ROOT
    if sandbox_root:
        real_path = os.path.realpath(abs_path)
        if not _is_path_within_sandbox(real_path, sandbox_root):
            raise ValueError(f"Access denied: path '{path}' (resolved: {real_path}) is outside sandbox root")
        target = real_path
    else:
        target = abs_path

    if check_exists and not os.path.exists(target):
        raise ValueError(f"File not found: {path} (resolved: {target})")

    return target


def _is_ignored_path(path: str) -> bool:
    try:
        parts = Path(path).parts
    except Exception:
        parts = path.split(os.sep)
    return any(part in DEFAULT_IGNORE_DIRS for part in parts)
