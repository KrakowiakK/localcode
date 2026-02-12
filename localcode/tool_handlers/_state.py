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

# Track files written via write_file (for next-step hints in read)
WRITTEN_PATHS: set = set()

# Track total tool calls per session (for urgency escalation hints)
TOOL_CALL_COUNT: int = 0

# Monotonic mutation sequence and compact mutation state for tool-result snapshots
MUTATION_SEQ: int = 0
MUTATION_HISTORY: List[Dict[str, Any]] = []
FILE_SHA_STATE: Dict[str, str] = {}

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
    global TOOL_CALL_COUNT, MUTATION_SEQ
    _LAST_PATCH_HASH.clear()
    _NOOP_COUNTS.clear()
    WRITTEN_PATHS.clear()
    TOOL_CALL_COUNT = 0
    MUTATION_SEQ = 0
    MUTATION_HISTORY.clear()
    FILE_SHA_STATE.clear()


def _read_file_bytes(path: str) -> Optional[bytes]:
    """Read file as raw bytes. Returns None on error."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _short_sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _next_mutation_id() -> str:
    global MUTATION_SEQ
    MUTATION_SEQ += 1
    return f"m{MUTATION_SEQ:05d}"


def _record_mutation(
    op: str,
    path: str,
    changed: bool,
    before_sha: str,
    after_sha: str,
    changed_lines_est: Optional[int] = None,
    changed_symbols: Optional[List[str]] = None,
    noop_streak_for_file: int = 0,
) -> Dict[str, Any]:
    mutation_id = _next_mutation_id()
    FILE_SHA_STATE[path] = after_sha
    event: Dict[str, Any] = {
        "mutation_id": mutation_id,
        "op": op,
        "path": path,
        "file": os.path.basename(path),
        "changed": bool(changed),
        "before_sha": before_sha,
        "after_sha": after_sha,
        "noop_streak_for_file": int(noop_streak_for_file or 0),
    }
    if changed_lines_est is not None:
        event["changed_lines_est"] = int(changed_lines_est)
    if changed_symbols:
        event["changed_symbols"] = list(changed_symbols[:10])

    write_streak = 0
    if op == "write" and changed:
        write_streak = 1
        for prev in reversed(MUTATION_HISTORY):
            if prev.get("op") == "write" and prev.get("path") == path and prev.get("changed"):
                write_streak += 1
            else:
                break
    event["write_streak_for_file"] = write_streak

    MUTATION_HISTORY.append(event)
    if len(MUTATION_HISTORY) > 12:
        del MUTATION_HISTORY[:-12]
    return event


def _mutation_state_line(event: Dict[str, Any]) -> str:
    event_safe = dict(event)
    # Keep canonical absolute paths internal only; do not leak them in tool output.
    event_safe.pop("path", None)
    recent = [
        {
            "mutation_id": e.get("mutation_id"),
            "op": e.get("op"),
            "file": e.get("file"),
            "after_sha": e.get("after_sha"),
            "changed": e.get("changed"),
        }
        for e in MUTATION_HISTORY[-2:]
    ]
    payload = {
        "kind": "mutation_state",
        **event_safe,
        "recent": recent,
    }
    return "state_json: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _mutation_decision_hint(event: Dict[str, Any]) -> str:
    changed = bool(event.get("changed"))
    op = str(event.get("op") or "?")
    changed_lines = event.get("changed_lines_est")
    write_streak = int(event.get("write_streak_for_file") or 0)
    noop_streak = int(event.get("noop_streak_for_file") or 0)

    if not changed:
        if noop_streak >= 2:
            return "decision_hint: stop_repeating_same_call; choose_edit_or_finish"
        return "decision_hint: file_unchanged; choose_finish_or_different_edit"

    if op == "write" and write_streak >= 3:
        return "decision_hint: avoid_full_rewrite; use_edit_or_finish"

    if isinstance(changed_lines, int) and changed_lines <= 2:
        return "decision_hint: tiny_change_done; finish_or_one_small_edit"

    return "decision_hint: change_applied; continue_or_finish"


def _mutation_brief_line(event: Dict[str, Any]) -> str:
    changed = bool(event.get("changed"))
    op = str(event.get("op") or "?")
    file_name = str(event.get("file") or "?")
    mutation_id = str(event.get("mutation_id") or "?")
    after_sha = str(event.get("after_sha") or "?")
    changed_lines = event.get("changed_lines_est")
    write_streak = int(event.get("write_streak_for_file") or 0)
    noop_streak = int(event.get("noop_streak_for_file") or 0)

    if not changed:
        next_action = "change_strategy_or_finish"
    elif op == "write" and write_streak >= 2:
        next_action = "prefer_edit_or_finish"
    elif isinstance(changed_lines, int) and changed_lines <= 2:
        next_action = "verify_then_finish_or_small_edit"
    else:
        next_action = "continue_or_finish"

    parts = [
        f"state_brief: mutation={mutation_id}",
        f"op={op}",
        f"file={file_name}",
        f"sha={after_sha}",
        f"changed={'yes' if changed else 'no'}",
    ]
    if isinstance(changed_lines, int):
        parts.append(f"delta_lines={changed_lines}")
    if write_streak:
        parts.append(f"write_streak={write_streak}")
    if noop_streak:
        parts.append(f"noop_streak={noop_streak}")
    parts.append(f"next={next_action}")
    return " ".join(parts)


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
