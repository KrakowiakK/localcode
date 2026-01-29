"""
Shared constants, mutable state, and utility functions for tool handlers.

This is the single source of truth for all shared globals used across
tool_handlers modules. localcode.py sets mutable values at startup.
No module in tool_handlers/ imports from localcode.localcode.
"""

import hashlib
import json
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 250 * 1024  # 250KB
MAX_SINGLE_FILE_SCAN = 2 * 1024 * 1024  # 2MB
DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_GLOB_RESULTS = 100
MAX_GREP_RESULTS = 100
DEFAULT_IGNORE_DIRS = {".git", "node_modules", ".localcode", ".nanocode", "__pycache__"}

MAX_FILE_VERSIONS = 200
MAX_SHELL_OUTPUT_CHARS = 30000
DEFAULT_SHELL_TIMEOUT_MS = 30000
MAX_SHELL_TIMEOUT_MS = 10 * 60 * 1000

# ---------------------------------------------------------------------------
# Mutable state (set by localcode.py at startup)
# ---------------------------------------------------------------------------

# Track last-read versions (LRU cache, max 200 entries)
FILE_VERSIONS: OrderedDict = OrderedDict()

# Sandbox root (cwd by default unless --no-sandbox)
SANDBOX_ROOT: Optional[str] = None

# Alias resolution (alias -> canonical) and display name overrides (canonical -> display)
TOOL_ALIAS_MAP: Dict[str, str] = {}
TOOL_DISPLAY_MAP: Dict[str, str] = {}

# Unsupported tools error messages
UNSUPPORTED_TOOLS: Dict[str, str] = {}

# Track last patch hash per file to detect repeated identical patches
_LAST_PATCH_HASH: Dict[str, str] = {}

# Track consecutive no-op counts per file per tool
_NOOP_COUNTS: Dict[str, Dict[str, int]] = {}  # {path: {"apply_patch": N, "write": N}}

# Extract the first file path from a unified patch block.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File):\s+(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def normalize_args(args: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(args, dict):
        return None
    return dict(args)


def _require_args_dict(args: Any, tool_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    normalized = normalize_args(args)
    if normalized is None:
        return None, f"error: invalid arguments for tool '{tool_name}': expected object"
    return normalized, None


def _reset_noop_tracking() -> None:
    _LAST_PATCH_HASH.clear()
    _NOOP_COUNTS.clear()


def _read_file_bytes(path: str) -> Optional[bytes]:
    """Read file as raw bytes. Returns None on error."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _track_file_version(path: str, content: str) -> None:
    """Store file content in LRU cache, evicting oldest if over limit."""
    if path in FILE_VERSIONS:
        FILE_VERSIONS.move_to_end(path)
    FILE_VERSIONS[path] = content
    while len(FILE_VERSIONS) > MAX_FILE_VERSIONS:
        FILE_VERSIONS.popitem(last=False)


def extract_patch_file(patch_text: str) -> Optional[str]:
    if not patch_text:
        return None
    match = _PATCH_FILE_RE.search(patch_text)
    if not match:
        return None
    return match.group(1).strip()
