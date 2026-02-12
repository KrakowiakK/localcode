"""
Path validation and ignore checks for tool handlers.
"""

import os
import re
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


def to_display_path(path: Optional[str]) -> str:
    """Render path for model-facing tool output without leaking absolute roots."""
    if path is None:
        return ""
    raw = str(path).strip()
    if not raw:
        return ""

    sandbox_root = _state.SANDBOX_ROOT
    try:
        abs_candidate = os.path.abspath(os.path.expanduser(raw))
    except Exception:
        abs_candidate = raw

    if sandbox_root:
        try:
            root_real = os.path.realpath(sandbox_root)
            path_real = os.path.realpath(abs_candidate)
            if _is_path_within_sandbox(path_real, root_real):
                rel = os.path.relpath(path_real, root_real)
                if rel == ".":
                    return "."
                return rel.replace(os.sep, "/")
        except Exception:
            pass

    if not os.path.isabs(raw):
        return raw.replace("\\", "/")

    base = os.path.basename(raw.rstrip("/\\"))
    if base:
        return base
    return raw.replace("\\", "/")


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
            raise ValueError(f"Access denied: path '{to_display_path(path)}' is outside sandbox root")
        target = real_path
    else:
        target = abs_path

    if check_exists and not os.path.exists(target):
        raise ValueError(f"File not found: {to_display_path(path)}")

    return target


def _is_ignored_path(path: str) -> bool:
    try:
        parts = Path(path).parts
    except Exception:
        parts = path.split(os.sep)
    return any(part in DEFAULT_IGNORE_DIRS for part in parts)


_TEST_DIRS = {"test", "tests", "__tests__", "__test__", "spec", "specs"}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}
_TEST_FILE_RE = re.compile(r"(^|[._-])(test|spec)([._-]|$)")


def _is_test_path(path: str) -> bool:
    if not path:
        return False
    normalized = path.replace("\\", "/").lower()
    parts = [p for p in normalized.split("/") if p]
    if not parts:
        return False
    for part in parts[:-1]:
        if part in _TEST_DIRS:
            return True
    filename = parts[-1]
    if ".test." in filename or ".spec." in filename:
        return True
    if filename.startswith(("test_", "spec_")):
        return True
    if "_test." in filename or "_spec." in filename:
        return True
    if _TEST_FILE_RE.search(filename):
        return True
    return False


def _is_benchmark_mode() -> bool:
    value = os.environ.get("LOCALCODE_BENCHMARK", "")
    if str(value).strip().lower() in _TRUTHY:
        return True
    return bool(os.environ.get("BENCHMARK_DIR") or os.environ.get("AIDER_DOCKER"))


def _find_file_in_sandbox(filename: str) -> Optional[str]:
    """Try to find a file by name within the sandbox directory."""
    sandbox = _state.SANDBOX_ROOT
    if not sandbox or not filename:
        return None
    for root, dirs, files in os.walk(sandbox):
        dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORE_DIRS]
        if filename in files:
            return os.path.join(root, filename)
    return None


def _should_block_test_edit(path: str) -> bool:
    override = str(os.environ.get("LOCALCODE_BLOCK_TEST_EDITS", "")).strip().lower()
    if override:
        if override in _FALSEY:
            return False
        if override in _TRUTHY:
            return _is_test_path(path)
    if not _is_benchmark_mode():
        return False
    return _is_test_path(path)
