#!/usr/bin/env python3
"""
localcode - agent runner with native tool calls.

- Sandbox-safe filesystem tools.
- Robust tool arg repair (number words for read line ranges).
- Tool handlers extracted to localcode/tool_handlers/ package.
"""

import argparse
import glob as globlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure 'localcode' is importable as a package even when this file is run
# directly as a script (python3 /path/to/localcode/localcode.py).
# In that case, the directory containing this file IS the package but Python
# doesn't know that — we add the parent dir to sys.path so that
# `from localcode import hooks` resolves to the package's hooks.py.
_this_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_this_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from localcode import hooks
from localcode.middleware import logging_hook

# Phase 2: tool handlers extracted into localcode/tool_handlers/
from localcode.tool_handlers import (
    # _state: constants and mutable state
    DEFAULT_IGNORE_DIRS,
    DEFAULT_READ_LIMIT,
    DEFAULT_SHELL_TIMEOUT_MS,
    FILE_VERSIONS,
    MAX_FILE_SIZE,
    MAX_FILE_VERSIONS,
    MAX_GLOB_RESULTS,
    MAX_GREP_RESULTS,
    MAX_LINE_LENGTH,
    MAX_SHELL_OUTPUT_CHARS,
    MAX_SHELL_TIMEOUT_MS,
    MAX_SINGLE_FILE_SCAN,
    _LAST_PATCH_HASH,
    _NOOP_COUNTS,
    _PATCH_FILE_RE,
    _read_file_bytes,
    _require_args_dict,
    _reset_noop_tracking,
    _sha256,
    _track_file_version,
    extract_patch_file,
    normalize_args,
    # _path
    _is_ignored_path,
    _is_path_within_sandbox,
    _validate_path,
    # _sandbox
    DANGEROUS_PATTERNS,
    TEST_MENTION_RE,
    _check_dangerous_command,
    _check_sandbox_allowlist,
    _ENV_VAR_ASSIGN_RE,
    _SANDBOX_ALLOWED_CMDS,
    _SANDBOX_INLINE_CODE_RE,
    _SHELL_CD_RE,
    _SHELL_CHAINING_RE,
    # read_handlers
    batch_read,
    read,
    # write_handlers
    edit,
    write,
    # patch_handlers
    _adjust_indent,
    _apply_add_patch,
    _apply_delete_patch,
    _apply_hunks,
    _apply_update_patch,
    _find_sublist,
    _get_indent,
    _log_fuzzy_match,
    _normalize_indent,
    _parse_hunks,
    apply_patch_fn,
    # search_handlers
    glob_fn,
    grep_fn,
    ls_fn,
    search_fn,
    # shell_handler
    _shell_payload,
    _truncate_shell_output,
    shell,
    # dispatch
    _NUMBER_WORDS,
    _TOOL_ARG_NUMBER_FIELDS,
    _extract_patch_block,
    _parse_number_words,
    _repair_number_word_args,
    _validate_tool_args,
    display_tool_name,
    is_tool_error,
    process_tool_call,
    resolve_tool_name,
    # schema
    build_feedback_text,
    build_tools,
    get_tool_feedback_template,
    make_openai_tools,
    render_feedback_template,
    render_tool_description,
)
# Import _state module directly for setting mutable globals at startup
from localcode.tool_handlers import _state as _tool_state
# Also need _sandbox's DANGEROUS_COMMAND_RES for backward compat
from localcode.tool_handlers._sandbox import _DANGEROUS_COMMAND_RES
DANGEROUS_COMMAND_RES = _DANGEROUS_COMMAND_RES

API_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "gpt-oss-120b@8bit"
MODEL = DEFAULT_MODEL
MAX_TOKENS = 16000
MAX_TURNS = 20

INFERENCE_PARAMS = {
    "temperature": 0,
    "top_p": None,
    "top_k": None,
    "min_p": None,
    "presence_penalty": None,
    "frequency_penalty": None,
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(BASE_DIR, "agents")
TOOL_DIR = os.path.join(BASE_DIR, "tools")
LOG_DIR = os.path.join(BASE_DIR, "logs")
SESSION_DIR = os.path.join(BASE_DIR, ".localcode/sessions")

LOG_PATH: Optional[str] = None  # Deprecated: use logging_hook.get_log_path(). Kept for backward compat.
AGENT_NAME: Optional[str] = None
AGENT_SETTINGS: Dict[str, Any] = {}
CONTINUE_SESSION = False
INTERACTIVE_MODE = False
LAST_RUN_SUMMARY: Optional[Dict[str, Any]] = None
RUN_NAME: Optional[str] = None
TASK_ID: Optional[str] = None
TASK_INDEX: Optional[int] = None
TASK_TOTAL: Optional[int] = None

# Sandbox root — proxy that delegates to _tool_state.SANDBOX_ROOT
# Use property-like access via the _tool_state module for the canonical value.
SANDBOX_ROOT: Optional[str] = None

# Current conversation messages (for tools that need history access like plan_solution)
CURRENT_MESSAGES: List[Dict[str, Any]] = []

# ANSI colors
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, RED, YELLOW = "\033[34m", "\033[36m", "\033[32m", "\033[31m", "\033[33m"

LOGO = f"""{CYAN}██╗      ██████╗  ██████╗  █████╗ ██╗      ██████╗ ██████╗ ██████╗ ███████╗
██║     ██╔═══██╗██╔════╝ ██╔══██╗██║     ██╔════╝██╔═══██╗██╔══██╗██╔════╝
██║     ██║   ██║██║      ███████║██║     ██║     ██║   ██║██║  ██║█████╗
██║     ██║   ██║██║      ██╔══██║██║     ██║     ██║   ██║██║  ██║██╔══╝
███████╗╚██████╔╝╚██████╗ ██║  ██║███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚══════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
                                                         by webdrroid.co.uk{RESET}"""

DEFAULT_TOOL_ORDER = ["read", "write", "edit", "apply_patch", "glob", "grep", "search", "ls"]

# These proxy to _tool_state for backward compatibility
UNSUPPORTED_TOOLS: Dict[str, str] = _tool_state.UNSUPPORTED_TOOLS

# Alias resolution (alias -> canonical) and display name overrides (canonical -> display)
TOOL_ALIAS_MAP: Dict[str, str] = _tool_state.TOOL_ALIAS_MAP

# Tool categories for semantic filtering (tool_name -> "read"/"write")
TOOL_CATEGORIES: Dict[str, str] = {}

TOOL_DISPLAY_MAP: Dict[str, str] = _tool_state.TOOL_DISPLAY_MAP


def _turn_summary_enabled() -> bool:
    value = os.environ.get("LOCALCODE_TURN_SUMMARY", "")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _thinking_visible() -> bool:
    value = os.environ.get("LOCALCODE_THINKING_VISIBILITY", "")
    if value:
        return str(value).strip().lower() not in {"0", "false", "no", "off", "hidden"}
    mode = str(AGENT_SETTINGS.get("thinking_visibility", "show")).strip().lower()
    return mode in {"show", "visible", "on", "true"}


def _format_task_label(continue_mode: bool, request_id: Optional[str]) -> str:
    task = TASK_ID or "unknown"
    if TASK_INDEX and TASK_TOTAL:
        prefix = f"TASK {TASK_INDEX}/{TASK_TOTAL}: {task}"
    elif TASK_INDEX:
        prefix = f"TASK {TASK_INDEX}: {task}"
    else:
        prefix = f"TASK: {task}"
    attempt = "TRY2" if continue_mode else "TRY1"
    req = request_id or "-"
    return f"{prefix} | {attempt} | id: {req}"


def _print_task_header(continue_mode: bool, request_id: Optional[str]) -> None:
    if not _turn_summary_enabled():
        return
    print(f"\n{_format_task_label(continue_mode, request_id)}", flush=True)


def _extract_path_from_args(args: Any) -> Optional[str]:
    if isinstance(args, dict):
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            return path
        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            return extract_patch_file(patch_text)
        return None
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return _extract_path_from_args(parsed)
        return extract_patch_file(args)
    return None


def _format_turn_actions(turn: int, tool_calls: List[Dict[str, Any]], content: str) -> str:
    actions: List[str] = []
    if turn == 1:
        actions.append("prompt")
    if tool_calls:
        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name") or "tool"
            args = (tc.get("function") or {}).get("arguments")
            path = _extract_path_from_args(args)
            if path:
                actions.append(f"{name}[{os.path.basename(path)}]")
            else:
                actions.append(name)
    elif content:
        actions.append("final")
    else:
        actions.append("no_output")
    return " -> ".join(actions)


def _print_turn_summary(turn: int, tool_calls: List[Dict[str, Any]], content: str, thinking: Optional[str]) -> None:
    print(f"\nTURN {turn}")
    if thinking:
        header = "----- THINKING -----"
        footer = "-" * len(header)
        print(header)
        print(thinking)
        print(footer)
    print(_format_turn_actions(turn, tool_calls, content))
    sys.stdout.flush()


# ---------------------------
# Utilities
# ---------------------------

def infer_run_name_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:
        p = Path(path)
    bench_root = os.environ.get("AIDER_BENCHMARK_DIR")
    if bench_root:
        try:
            root = Path(bench_root).resolve()
            if root in p.parents:
                rel = p.relative_to(root)
                if rel.parts:
                    return rel.parts[0]
        except Exception:
            pass
    for part in p.parts:
        if "--localcode-" in part or "--nanocode-" in part:
            return part
    for part in p.parts:
        if len(part) >= 10 and part[4] == "-" and part[7] == "-" and "--" in part:
            return part
    return None


def infer_task_id_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:
        p = Path(path)
    if p.name.startswith("prompt_try"):
        return p.parent.name
    return None


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def normalize_bool_auto(value: Any, field_name: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    raise ValueError(f"Agent config '{field_name}' must be a boolean or 'auto'")


def is_tool_choice_required(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "required"
    if isinstance(value, dict):
        return value.get("type") == "function"
    return False


def _coerce_cli_value(raw: str, existing: Any, key_name: str) -> Any:
    value = raw.strip()
    lowered = value.lower()
    if lowered in {"none", "null"}:
        return None

    if existing is None:
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    if isinstance(existing, bool):
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered == "auto":
            return "auto"
        raise SystemExit(f"Invalid boolean for --{key_name}: {raw}")

    if isinstance(existing, int) and not isinstance(existing, bool):
        try:
            return int(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid integer for --{key_name}: {raw}") from exc

    if isinstance(existing, float):
        try:
            return float(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid float for --{key_name}: {raw}") from exc

    if isinstance(existing, list):
        if value.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON array for --{key_name}: {raw}") from exc
            if not isinstance(parsed, list):
                raise SystemExit(f"Expected JSON array for --{key_name}: {raw}")
            return parsed
        return [item.strip() for item in value.split(",") if item.strip()]

    if isinstance(existing, dict):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON object for --{key_name}: {raw}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit(f"Expected JSON object for --{key_name}: {raw}")
        return parsed

    return value


def apply_cli_overrides(agent_config: Dict[str, Any], extra_args: List[str]) -> Dict[str, Any]:
    if not extra_args:
        return agent_config
    overrides: Dict[str, Any] = {}
    idx = 0
    while idx < len(extra_args):
        arg = extra_args[idx]
        if not arg.startswith("--"):
            raise SystemExit(f"Unexpected argument: {arg}")
        key = arg[2:].replace("-", "_")
        if idx + 1 >= len(extra_args) or extra_args[idx + 1].startswith("--"):
            raise SystemExit(f"Missing value for {arg}")
        raw_value = extra_args[idx + 1]
        overrides[key] = _coerce_cli_value(raw_value, agent_config.get(key), key)
        idx += 2

    merged = dict(agent_config)
    merged.update(overrides)
    return merged


def split_cli_overrides(argv: List[str]) -> Tuple[List[str], List[str]]:
    known_flags = {
        "--agent", "-a",
        "--continue", "-c",
        "--model", "-m",
        "--file", "-f",
        "--url",
        "--temperature", "--top_p", "--top_k", "--min_p",
        "--max_tokens",
        "--no-sandbox",
        "--help", "-h",
    }
    flags_with_values = {
        "--agent", "-a",
        "--model", "-m",
        "--file", "-f",
        "--url",
        "--temperature", "--top_p", "--top_k", "--min_p",
        "--max_tokens",
    }

    filtered: List[str] = []
    overrides: List[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in known_flags:
            filtered.append(arg)
            if arg in flags_with_values:
                if idx + 1 >= len(argv):
                    raise SystemExit(f"Missing value for {arg}")
                filtered.append(argv[idx + 1])
                idx += 2
            else:
                idx += 1
            continue

        if arg.startswith("--"):
            if idx + 1 >= len(argv) or argv[idx + 1].startswith("--"):
                raise SystemExit(f"Missing value for {arg}")
            overrides.extend([arg, argv[idx + 1]])
            idx += 2
            continue

        filtered.append(arg)
        idx += 1

    return filtered, overrides


def summarize_messages(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    roles = [m.get("role") for m in messages]
    tool_messages = sum(1 for m in messages if m.get("role") == "tool")
    total_chars = sum(len(m.get("content") or "") for m in messages)
    reasoning_chars = sum(len(m.get("reasoning_content") or "") for m in messages)
    reasoning_messages = sum(1 for m in messages if m.get("reasoning_content"))
    return {
        "message_count": len(messages),
        "roles": roles,
        "tool_message_count": tool_messages,
        "total_chars": total_chars,
        "reasoning_message_count": reasoning_messages,
        "reasoning_chars": reasoning_chars,
    }


# ---------------------------
# Logging / Sessions
# ---------------------------

def create_new_session_path(agent_name: str) -> str:
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(SESSION_DIR, f"{timestamp}_{agent_name}.json")


def find_latest_session(agent_name: str) -> Optional[str]:
    os.makedirs(SESSION_DIR, exist_ok=True)
    pattern = os.path.join(SESSION_DIR, f"*_{agent_name}.json")
    files = globlib.glob(pattern)
    if not files:
        return None
    files.sort(reverse=True)
    return files[0]


def init_logging() -> None:
    """Initialize JSONL logging via logging_hook."""
    if logging_hook.get_log_path():
        return
    logging_hook.init_logging(LOG_DIR, AGENT_NAME)
    _sync_logging_context()
    logging_hook.log_event("session_start", {
        "model": MODEL,
        "cwd": os.getcwd(),
        "log_path": logging_hook.get_log_path(),
        "mode": "single_agent_native_tools",
        "agent": AGENT_NAME,
        "agent_settings": AGENT_SETTINGS,
    })


def _sync_logging_context() -> None:
    """Sync global state into logging_hook run context."""
    ctx: Dict[str, Any] = {}
    if RUN_NAME:
        ctx["run_name"] = RUN_NAME
    if TASK_ID:
        ctx["task_id"] = TASK_ID
    if TASK_INDEX:
        ctx["task_index"] = TASK_INDEX
    if TASK_TOTAL:
        ctx["task_total"] = TASK_TOTAL
    if AGENT_NAME:
        ctx["agent"] = AGENT_NAME
    logging_hook.update_run_context(ctx)


def save_session(agent_name: str, messages: List[Dict[str, Any]], model: str) -> None:
    global CURRENT_SESSION_PATH
    if CURRENT_SESSION_PATH is None:
        CURRENT_SESSION_PATH = create_new_session_path(agent_name)
    os.makedirs(os.path.dirname(CURRENT_SESSION_PATH), exist_ok=True)

    created = None
    if os.path.exists(CURRENT_SESSION_PATH):
        try:
            with open(CURRENT_SESSION_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
            created = existing.get("created")
        except Exception:
            created = None

    session_data = {
        "agent": agent_name,
        "model": model,
        "messages": messages,
        "created": created or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Hook: session_save (read-only notification)
    hooks.emit("session_save", {"messages": messages, "path": CURRENT_SESSION_PATH})

    with open(CURRENT_SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    logging_hook.log_event("session_saved", {"path": CURRENT_SESSION_PATH, "message_count": len(messages)})


def load_session(agent_name: str) -> List[Dict[str, Any]]:
    global CURRENT_SESSION_PATH
    latest = find_latest_session(agent_name)
    if not latest:
        return []
    try:
        with open(latest, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        msgs = session_data.get("messages", [])
        CURRENT_SESSION_PATH = latest
        logging_hook.log_event("session_loaded", {"path": latest, "message_count": len(msgs)})
        return msgs
    except Exception as e:
        logging_hook.log_event("session_load_error", {"path": latest, "error": str(e)})
        return []


def init_new_session(agent_name: str) -> None:
    global CURRENT_SESSION_PATH
    CURRENT_SESSION_PATH = create_new_session_path(agent_name)


# ---------------------------
# Tools / Agents config
# ---------------------------

ToolTuple = Tuple[str, Dict[str, Any], Any, Dict[str, Any], Dict[str, Any]]
ToolsDict = Dict[str, ToolTuple]

def load_tool_defs(tool_dir: str) -> Dict[str, Dict[str, Any]]:
    tools: Dict[str, Dict[str, Any]] = {}
    for path in sorted(globlib.glob(os.path.join(tool_dir, "*.json"))):
        data = load_json(path)
        name = data.get("name")
        if not name:
            raise ValueError(f"Tool file missing name: {path}")
        if name in tools:
            raise ValueError(f"Duplicate tool name: {name}")
        tools[name] = data
    return tools


def load_agent_defs(agent_dir: str) -> Dict[str, Dict[str, Any]]:
    agents: Dict[str, Dict[str, Any]] = {}
    base_dir = Path(agent_dir)
    for path in sorted(base_dir.rglob("*.json")):
        data = load_json(str(path))
        # Use declared name from JSON, fallback to path-based name
        rel = path.relative_to(base_dir).with_suffix("")
        path_name = rel.as_posix()
        name = data.get("name") or path_name
        data["name"] = name
        data["_path"] = str(path)  # Store path for reference
        if name in agents:
            raise ValueError(f"Duplicate agent name: {name} (files: {agents[name].get('_path')} and {path})")
        agents[name] = data
    return agents


def resolve_agent_path(agent_config: Dict[str, Any], key: str, base_dir: str) -> str:
    value = agent_config.get(key)
    if not value:
        raise ValueError(f"Agent config missing '{key}'")
    return value if os.path.isabs(value) else os.path.join(base_dir, value)


def build_tool_alias_map(tool_defs: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for name, tool_def in tool_defs.items():
        aliases = tool_def.get("aliases") or []
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            key = alias.strip().lower()
            if not key:
                continue
            if key in alias_map and alias_map[key] != name:
                raise ValueError(f"Alias '{alias}' conflicts with '{alias_map[key]}' and '{name}'")
            alias_map[key] = name
    return alias_map


def build_tool_category_map(tool_defs: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """
    Extract tool categories from tool definitions.

    Returns a map of tool_name -> category (e.g., "read" or "write").
    Also includes aliases mapped to their category.
    """
    category_map: Dict[str, str] = {}
    for name, tool_def in tool_defs.items():
        category = tool_def.get("category")
        if category and isinstance(category, str):
            category = category.strip().lower()
            # Map canonical name
            category_map[name] = category
            # Also map all aliases
            aliases = tool_def.get("aliases") or []
            for alias in aliases:
                if isinstance(alias, str):
                    category_map[alias.strip().lower()] = category
    return category_map


def resolve_tool_order(agent_config: Dict[str, Any], alias_map: Dict[str, str]) -> Tuple[List[str], List[str]]:
    tool_order_raw = agent_config.get("tools", DEFAULT_TOOL_ORDER)
    # Allow empty tools list for no-tools agents (e.g., code-architect)
    if not isinstance(tool_order_raw, list):
        raise ValueError("Agent config 'tools' must be a list")
    if not tool_order_raw:
        return [], []
    canonical_order: List[str] = []
    for name in tool_order_raw:
        key = str(name).strip().lower()
        canonical_order.append(alias_map.get(key, key))
    return canonical_order, list(tool_order_raw)


def resolve_tool_display_map(
    agent_config: Dict[str, Any],
    tool_defs: Dict[str, Dict[str, Any]],
    canonical_order: List[str],
    raw_order: List[str],
    alias_map: Dict[str, str],
) -> Dict[str, str]:
    display_map: Dict[str, str] = {}
    for raw_name in raw_order:
        key = str(raw_name).strip().lower()
        canonical = alias_map.get(key, key)
        display_map[canonical] = str(raw_name)

    if agent_config.get("tool_name_style") == "alias":
        for canonical in canonical_order:
            if display_map.get(canonical, canonical) == canonical:
                aliases = tool_defs.get(canonical, {}).get("aliases") or []
                if aliases:
                    display_map[canonical] = str(aliases[0])

    for canonical, alias in (agent_config.get("tool_aliases") or {}).items():
        if canonical in canonical_order and isinstance(alias, str) and alias.strip():
            # Validate alias resolves back to canonical via alias_map
            alias_key = alias.strip().lower()
            if alias_key != canonical:
                resolved = alias_map.get(alias_key)
                if resolved is None:
                    raise ValueError(
                        f"tool_aliases: alias '{alias}' for tool '{canonical}' is not "
                        f"registered in tool definitions; the model will call '{alias}' "
                        f"but it won't resolve back to '{canonical}'. "
                        f"Add '{alias}' to the tool's aliases list in its JSON definition."
                    )
                if resolved != canonical:
                    raise ValueError(
                        f"tool_aliases: alias '{alias}' for tool '{canonical}' resolves "
                        f"to '{resolved}' instead; this would cause a silent tool swap. "
                        f"Use an alias that maps back to '{canonical}'."
                    )
            display_map[canonical] = alias
        elif canonical not in canonical_order:
            raise ValueError(
                f"tool_aliases references unknown tool '{canonical}'; "
                f"available: {', '.join(canonical_order)}"
            )

    return display_map


def format_tool_list(
    tool_defs: Dict[str, Dict[str, Any]],
    tool_order: List[str],
    display_map: Optional[Dict[str, str]] = None,
) -> str:
    lines: List[str] = []
    for name in tool_order:
        tool_def = tool_defs.get(name)
        if not tool_def:
            continue
        params = tool_def.get("parameters", {}) or {}
        parts = []
        for pn, pt in params.items():
            optional = False
            if isinstance(pt, str):
                optional = pt.endswith("?")
            elif isinstance(pt, dict):
                type_val = pt.get("type")
                optional = bool(pt.get("optional", False))
                if isinstance(type_val, str) and type_val.endswith("?"):
                    optional = True
            parts.append(f"{pn}?" if optional else str(pn))
        display = display_map.get(name, name) if display_map else name
        sig = f"{display}({', '.join(parts)})"
        desc = render_tool_description((tool_def.get("description") or "").strip(), display_map)
        lines.append(f"- {sig}: {desc}" if desc else f"- {sig}")
    return "\n".join(lines)


def load_system_prompt(
    agent_config: Dict[str, Any],
    tool_defs: Dict[str, Dict[str, Any]],
    tool_order: List[str],
    display_map: Optional[Dict[str, str]] = None,
) -> str:
    prompt_path = resolve_agent_path(agent_config, "prompt", BASE_DIR)
    prompt = load_text(prompt_path)
    tool_list = format_tool_list(tool_defs, tool_order, display_map)
    prompt = prompt.replace("{{TOOLS}}", tool_list)
    return render_tool_description(prompt, display_map)


def build_agent_settings(agent_config: Dict[str, Any]) -> Dict[str, Any]:
    settings = {
        "request_overrides": {},
        "min_tool_calls": 0,
        "max_format_retries": 0,
        "auto_tool_call_on_failure": False,
        "require_code_change": False,
        "native_thinking": False,
        "thinking_visibility": "show",
    }

    for k in ("min_tool_calls", "max_format_retries"):
        if k in agent_config:
            settings[k] = agent_config[k]

    for k in ("auto_tool_call_on_failure", "require_code_change"):
        if k in agent_config:
            settings[k] = agent_config[k]

    if "native_thinking" in agent_config:
        settings["native_thinking"] = bool(agent_config["native_thinking"])

    if "thinking_visibility" in agent_config:
        settings["thinking_visibility"] = agent_config["thinking_visibility"]

    thinking_visibility = str(settings.get("thinking_visibility", "show")).strip().lower()
    if thinking_visibility not in {"show", "hidden"}:
        raise ValueError("Agent config 'thinking_visibility' must be 'show' or 'hidden'")
    settings["thinking_visibility"] = thinking_visibility

    overrides: Dict[str, Any] = {}
    raw_overrides = agent_config.get("request_overrides", {})
    if raw_overrides:
        if not isinstance(raw_overrides, dict):
            raise ValueError("Agent config 'request_overrides' must be an object")
        overrides.update(raw_overrides)

    if "tool_choice" in agent_config:
        overrides.setdefault("tool_choice", agent_config["tool_choice"])

    think_value = normalize_bool_auto(agent_config.get("think"), "think")
    if think_value is not None:
        overrides.setdefault("think", think_value)

    cache_value = normalize_bool_auto(agent_config.get("cache"), "cache")
    if cache_value is not None:
        overrides.setdefault("cache", cache_value)

    if "think_level" in agent_config:
        think_level_value = agent_config["think_level"]
        # Only set reasoning_effort if think_level is not null/None
        if think_level_value is not None:
            overrides.setdefault("reasoning_effort", think_level_value)
        if think_value is True:
            overrides.setdefault("think", True)

    if think_value is True or settings["native_thinking"]:
        overrides.setdefault("return_thinking", True)

    # Pass max_batch_tool_calls to API for GPT-OSS batching control
    if "max_batch_tool_calls" in agent_config:
        overrides.setdefault("max_batch_tool_calls", agent_config["max_batch_tool_calls"])

    settings["request_overrides"] = overrides
    return settings


# ---------------------------
# Model call tools (self-call / subprocess)
# ---------------------------

def _load_prompt_file(relative_path: str) -> str:
    """Load a prompt file relative to BASE_DIR."""
    full = os.path.join(BASE_DIR, relative_path)
    with open(full, "r", encoding="utf-8") as f:
        return f.read().strip()


def _self_call(
    prompt: str,
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 120,
    include_history: bool = True,
    user_prefix: str = "",
) -> str:
    """Make an API call to the same model (self-reflection / thinking)."""
    history_messages = []
    if include_history:
        for msg in CURRENT_MESSAGES:
            if msg.get("role") in ("user", "assistant", "tool"):
                history_messages.append(msg)

    user_content = f"{user_prefix}{prompt}" if user_prefix else prompt

    messages = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_content},
    ]

    request_data = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(request_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        payload = json.loads(resp.read())

        if "choices" in payload and payload["choices"]:
            content = payload["choices"][0].get("message", {}).get("content", "")
            if content:
                return content.strip()

        return "error: no response from model"

    except Exception as e:
        return f"error: API call failed: {e}"


def _self_call_batch(
    questions: List[str],
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: int = 120,
    include_history: bool = True,
    max_concurrent: int = 4,
) -> str:
    """Send multiple questions concurrently via ThreadPoolExecutor.

    Fail-all: if any question fails, the entire batch returns an error.
    """
    def call_one(idx: int, question: str) -> Tuple[int, str]:
        result = _self_call(
            prompt=question,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            include_history=include_history,
        )
        return (idx, result)

    results: List[Tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {
            executor.submit(call_one, i, q): i
            for i, q in enumerate(questions)
        }
        for future in as_completed(futures):
            idx, answer = future.result()
            if answer.startswith("error:"):
                return answer
            results.append((idx, answer))

    results.sort(key=lambda x: x[0])
    parts = []
    for idx, answer in results:
        parts.append(f"## Question {idx + 1}: {questions[idx]}\n\n{answer}")
    return "\n\n---\n\n".join(parts)


def _subprocess_call(
    prompt: str,
    agent: str,
    timeout_sec: int,
    files: List[str],
    config: Dict[str, Any],
) -> str:
    """Run a sub-agent via subprocess and return its cleaned response."""
    # If files are specified, read them and append to prompt
    if config.get("read_files") and files and isinstance(files, list):
        file_contents = []
        for file_path in files:
            if not isinstance(file_path, str):
                continue
            try:
                if _tool_state.SANDBOX_ROOT:
                    full_path = _validate_path(file_path, check_exists=True)
                else:
                    full_path = os.path.abspath(file_path)
                if _is_ignored_path(full_path):
                    file_contents.append(f"=== {file_path} ===\n(ignored path)")
                    continue
                if os.path.exists(full_path) and os.path.isfile(full_path):
                    stat = os.stat(full_path)
                    if stat.st_size > MAX_FILE_SIZE:
                        file_contents.append(f"=== {file_path} ===\n(file too large: {stat.st_size} bytes)")
                        continue
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(MAX_FILE_SIZE)
                    file_contents.append(f"=== {file_path} ===\n{content}")
                else:
                    file_contents.append(f"=== {file_path} ===\n(file not found)")
            except ValueError as e:
                file_contents.append(f"=== {file_path} ===\n(access denied: {e})")
            except Exception as e:
                file_contents.append(f"=== {file_path} ===\n(error reading: {e})")

        if file_contents:
            prompt = prompt + "\n\nFILES:\n" + "\n\n".join(file_contents)

    # Build the localcode command - pass URL from parent agent
    localcode_path = os.path.join(BASE_DIR, "localcode.py")
    cmd = [
        sys.executable,
        localcode_path,
        "--agent", agent,
        "--url", API_URL,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=os.getcwd(),
        )
        stdout = (result.stdout or "").strip()

        lines = stdout.split("\n")
        response_lines = []
        in_thinking = False

        for line in lines:
            # Remove ANSI escape codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line) if config.get("strip_ansi") else line
            # Check for thinking section markers before stripping unicode
            if config.get("strip_thinking") and "----- THINKING -----" in clean:
                in_thinking = True
                continue
            if in_thinking and ("\u23fa" in line or clean.strip().startswith("**")):
                in_thinking = False
            if in_thinking:
                continue
            # Remove other special characters (Unicode symbols)
            clean = re.sub(r'[^\x00-\x7F]+', '', clean).strip()
            if not clean:
                continue
            # Skip status/header lines from localcode output
            if config.get("strip_status_lines"):
                if clean.startswith("localcode["):
                    continue
                if clean.startswith("TURN"):
                    continue
                if clean.startswith("TASK ") and ("TRY" in clean or "id:" in clean):
                    continue
            response_lines.append(clean)

        response = "\n".join(response_lines).strip()
        if not response:
            return f"error: agent returned no output (stdout_len={len(stdout)}, returncode={result.returncode})"

        return response

    except subprocess.TimeoutExpired:
        return f"error: agent timed out after {timeout_sec} seconds"
    except Exception as e:
        return f"error: failed to call agent: {e}"


def make_model_call_handler(tool_name: str, config: Dict[str, Any]):
    """Factory that creates a tool handler from a model_call config block."""
    mode = config.get("mode", "self")

    def handler(args: Any) -> str:
        args, err = _require_args_dict(args, tool_name)
        if err:
            return err

        if mode == "self_batch":
            questions = args.get("questions")
            if not questions or not isinstance(questions, list):
                return "error: questions is required and must be an array of strings"
            questions = [q for q in questions if isinstance(q, str) and q.strip()]
            if not questions:
                return "error: questions array must contain at least one non-empty string"
            max_questions = config.get("max_questions", 10)
            if len(questions) > max_questions:
                return f"error: maximum {max_questions} questions per batch"

            system_prompt = _load_prompt_file(config["system_prompt_file"])
            return _self_call_batch(
                questions=questions,
                system_prompt=system_prompt,
                temperature=config.get("temperature", 0.3),
                max_tokens=config.get("max_tokens", 2000),
                timeout=config.get("timeout", 120),
                include_history=config.get("include_history", True),
                max_concurrent=config.get("max_concurrent", 4),
            )

        prompt = args.get("prompt") or args.get("content")
        if not prompt or not isinstance(prompt, str):
            return "error: prompt is required and must be a string"

        if mode == "subprocess":
            agent = args.get("agent", config.get("default_agent", "code-architect"))
            timeout_sec = args.get("timeout", config.get("default_timeout", 300))
            files = args.get("files", [])
            return _subprocess_call(prompt, agent, timeout_sec, files, config)

        # mode == "self"
        system_prompt = _load_prompt_file(config["system_prompt_file"])
        stage_param = config.get("stage_param")
        if stage_param:
            stage = args.get(stage_param, "").lower().strip()
            stage_files = config.get("stage_prompt_files", {})
            if stage and stage in stage_files:
                system_prompt = _load_prompt_file(stage_files[stage])
            # Log for debugging (preserves original think behavior)
            print(f"\n[{tool_name.upper()}] stage={stage or 'none'} prompt={prompt}\n", file=sys.stderr)

        return _self_call(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=config.get("temperature", 0.3),
            max_tokens=config.get("max_tokens", 4000),
            timeout=config.get("timeout", 120),
            include_history=config.get("include_history", True),
            user_prefix=config.get("user_prefix", ""),
        )

    handler.__name__ = f"model_call_{tool_name}"
    return handler


# ---------------------------
# API usage / analysis helpers
# ---------------------------

def format_usage_info(usage: Optional[Dict[str, Any]], timings: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if not usage:
        return None
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # TPS: prefer top-level timings from llama-server, fallback to usage.timing
    timing = usage.get("timing", {}) or {}
    ttft = timing.get("ttft", 0)
    prefill_tps = (timings or {}).get("prompt_per_second", 0) or timing.get("prefill_tps", 0)
    decode_tps = (timings or {}).get("predicted_per_second", 0) or timing.get("decode_tps", 0)

    parts = [f"{prompt_tokens}→{completion_tokens} tok"]
    if ttft:
        parts.append(f"TTFT {float(ttft):.2f}s")
    if prefill_tps:
        parts.append(f"prefill {float(prefill_tps):.0f} t/s")
    if decode_tps:
        parts.append(f"decode {float(decode_tps):.1f} t/s")
    return " | ".join(parts)


def is_analysis_artifact(content: Optional[str]) -> bool:
    if not content:
        return False
    text = content.lstrip()
    if not text.startswith("<|channel|>analysis"):
        return False
    if "<|channel|>final" in text:
        return False
    return any(token in text for token in ("<|start|>", "<|message|>", "<|end|>", "<|return|>"))


def normalize_analysis_only(content: Optional[str]) -> Tuple[Optional[str], bool]:
    if content is None or content == "":
        return content, False
    if not is_analysis_artifact(content):
        return content, False
    match = re.search(
        r"<\|channel\|>analysis<\|message\|>(.*?)(?:<\|end\|>|<\|channel\|>|$)",
        content,
        re.DOTALL,
    )
    if match:
        return match.group(1), True
    return content, True


# ---------------------------
# API calls
# ---------------------------

MAX_CONTEXT_CHARS = 400000  # 400k chars context limit
TRIM_KEEP_LAST_N = 30  # Always keep last N messages


def trim_messages(messages: List[Dict[str, Any]], max_chars: int = MAX_CONTEXT_CHARS, keep_last_n: int = TRIM_KEEP_LAST_N) -> List[Dict[str, Any]]:
    """Trim oldest messages to fit within max_chars, always keeping the last keep_last_n messages.

    Removes messages in coherent groups: an assistant message with tool_calls
    is removed together with its subsequent tool-result messages to avoid
    orphaned tool results that would cause API errors.
    """
    if not messages:
        return messages

    def _msg_chars(m: Dict[str, Any]) -> int:
        total = len(m.get("content") or "")
        total += len(m.get("reasoning_content") or "")
        for tc in (m.get("tool_calls") or []):
            total += len(json.dumps(tc.get("function", {}), ensure_ascii=False))
        return total

    current_total = sum(_msg_chars(m) for m in messages)
    if current_total <= max_chars:
        return messages

    # Always preserve at least the last keep_last_n messages
    protected = min(keep_last_n, len(messages))
    trimmed = list(messages)

    while len(trimmed) > protected and current_total > max_chars:
        head = trimmed[0]
        # If head is assistant with tool_calls, also remove subsequent tool results
        if head.get("role") == "assistant" and head.get("tool_calls"):
            tc_ids = {tc.get("id") for tc in head["tool_calls"] if tc.get("id")}
            group_size = 1
            while group_size < len(trimmed) - protected:
                next_msg = trimmed[group_size]
                if next_msg.get("role") == "tool" and next_msg.get("tool_call_id") in tc_ids:
                    group_size += 1
                else:
                    break
            for _ in range(group_size):
                removed = trimmed.pop(0)
                current_total -= _msg_chars(removed)
        # If head is an orphaned tool result (shouldn't happen, but defensive), skip it as a group
        elif head.get("role") == "tool":
            removed = trimmed.pop(0)
            current_total -= _msg_chars(removed)
        else:
            removed = trimmed.pop(0)
            current_total -= _msg_chars(removed)

    return trimmed


def call_api(messages: List[Dict[str, Any]], system_prompt: str, tools_dict: ToolsDict, request_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    trimmed = trim_messages(messages)
    full_messages = [{"role": "system", "content": system_prompt}] + trimmed
    request_data: Dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": full_messages,
        "stream": False,
        "tools": make_openai_tools(tools_dict, TOOL_DISPLAY_MAP),
    }

    # Add tool categories for semantic filtering (GPT-OSS batching)
    if TOOL_CATEGORIES:
        request_data["tool_categories"] = TOOL_CATEGORIES

    for k, v in INFERENCE_PARAMS.items():
        if v is not None:
            request_data[k] = v

    if request_overrides:
        for k, v in request_overrides.items():
            if k in ("messages", "tools"):
                continue
            request_data[k] = v

    # GLM models use a structured "thinking" object instead of think=true/false.
    # Keep GPT-OSS and other models on the legacy fields for compatibility.
    if "glm" in MODEL.lower():
        think_value = request_data.get("think")
        reasoning_effort = request_data.get("reasoning_effort")
        if think_value is not None or reasoning_effort is not None:
            thinking_payload: Dict[str, Any] = {
                "type": "enabled" if bool(think_value) else "disabled",
            }
            if reasoning_effort:
                thinking_payload["effort"] = reasoning_effort
            request_data["thinking"] = thinking_payload
            request_data.pop("think", None)
            request_data.pop("reasoning_effort", None)

    # Log all inference params (including nulls → "server_default") so we know what was sent
    all_inference_params = {}
    for k, v in INFERENCE_PARAMS.items():
        all_inference_params[k] = v if v is not None else "server_default"
    logging_hook.log_event("request", {
        "tools": list(tools_dict.keys()),
        "tools_display": [TOOL_DISPLAY_MAP.get(n, n) for n in tools_dict.keys()],
        "message_summary": summarize_messages(full_messages),
        "request_params": {k: v for k, v in request_data.items() if k not in ("messages", "tools")},
        "inference_params_full": all_inference_params,
    })

    # Hook: api_request (mutable — hooks can modify request_data)
    hook_data = hooks.emit("api_request", {
        "messages": full_messages,
        "request_data": request_data,
    })
    request_data = hook_data.get("request_data", request_data)

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(request_data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        raw = resp.read()
    except Exception as exc:
        logging_hook.log_event("request_error", {"error": str(exc)})
        hooks.emit("api_error", {"error": str(exc), "phase": "request"})
        return {"error": f"request failed: {exc}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:200].decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)[:200]
        logging_hook.log_event("response_error", {"error": str(exc), "raw_preview": preview})
        hooks.emit("api_error", {"error": str(exc), "phase": "parse", "raw_preview": preview})
        return {"error": f"invalid JSON response: {exc}"}

    request_id = payload.get("request_id")
    if not request_id:
        try:
            request_id = resp.headers.get("X-Request-Id") or resp.headers.get("X-Request-ID")
        except Exception:
            request_id = None
    if request_id:
        payload["request_id"] = request_id

    # Log usage, timings (TPS from llama-server), and request_id
    meta: Dict[str, Any] = {
        "usage": payload.get("usage", {}),
        "request_id": payload.get("request_id"),
    }
    timings = payload.get("timings")
    if timings:
        meta["timings"] = timings
        meta["prefill_tps"] = round(timings.get("prompt_per_second", 0), 2)
        meta["decode_tps"] = round(timings.get("predicted_per_second", 0), 2)
    logging_hook.log_event("response_meta", meta)

    # Hook: api_response (read-only notification)
    hooks.emit("api_response", {
        "response": payload,
        "usage": payload.get("usage", {}),
        "timings": timings,
        "request_id": payload.get("request_id"),
    })

    return payload


# ---------------------------
# Agent runtime
# ---------------------------

def select_forced_tool_call(prompt: str, tools_dict: ToolsDict) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    # Prefer read on an existing path mentioned in prompt
    # (simple heuristic: any token like something.ext that exists)
    if "read" in tools_dict:
        matches = re.findall(r"(?:/|\b)[\w./-]+\.[A-Za-z0-9]{1,6}\b", prompt or "")
        cwd = os.getcwd()
        for m in matches:
            candidates = [m, os.path.expanduser(m)]
            if not os.path.isabs(m):
                candidates.append(os.path.join(cwd, m))
            for c in candidates:
                if os.path.isfile(c):
                    if _tool_state.SANDBOX_ROOT and not _is_path_within_sandbox(os.path.realpath(c), _tool_state.SANDBOX_ROOT):
                        continue
                    return "read", {"path": c}
    if "ls" in tools_dict:
        return "ls", {"path": ""}
    if tools_dict:
        # deterministic: follow DEFAULT_TOOL_ORDER preference
        for t in DEFAULT_TOOL_ORDER:
            if t in tools_dict:
                return t, {}
        return next(iter(tools_dict.keys())), {}
    return None, None


def is_write_tool(tool_name: str) -> bool:
    """Check if a tool is a write/edit tool using TOOL_CATEGORIES."""
    if TOOL_CATEGORIES:
        return TOOL_CATEGORIES.get(tool_name, "").lower() == "write"
    # Fallback to hardcoded list if categories not loaded
    return tool_name in {"write", "write_file", "edit", "replace_in_file", "apply_patch", "patch_files"}


def _did_tool_make_change(tool_name: str, result: Any) -> bool:
    """Return True only if the tool result indicates a real file change on disk.

    Uses tool-specific checks to avoid false positives from generic 'ok:' prefixes.
    """
    if not isinstance(result, str):
        return False
    if result.startswith("error:"):
        return False
    r = result.lower()
    if "no changes" in r:
        return False
    if not result.startswith("ok:"):
        return False
    # Tool-specific positive signals
    if tool_name in ("apply_patch", "patch_files"):
        return "file(s) changed" in r
    if tool_name in ("write", "write_file"):
        return "created" in r or "updated" in r
    if tool_name in ("edit", "replace_in_file"):
        return "replacement(s)" in r
    # Conservative default: unknown tool with ok: prefix is not a proven change
    return False


def get_available_write_tools(tools_dict: ToolsDict) -> List[str]:
    """Get list of available write tools from tools_dict using categories."""
    return [name for name in tools_dict if is_write_tool(name)]


def select_code_change_tool(tools_dict: ToolsDict) -> Optional[str]:
    """Select best available write tool in order of preference."""
    # Preferred order for code changes
    preferred_order = ("patch_files", "apply_patch", "replace_in_file", "edit", "write_file", "write")
    for name in preferred_order:
        if name in tools_dict and is_write_tool(name):
            return name
    # Fallback: any write tool
    write_tools = get_available_write_tools(tools_dict)
    return write_tools[0] if write_tools else None


def _append_feedback(
    messages: List[Dict[str, Any]],
    turn: int,
    request_id: Optional[str],
    text: str,
    reason: str,
    attempt: Optional[int] = None,
) -> None:
    messages.append({"role": "user", "content": text})
    logging_hook.log_event("runtime_feedback", {
        "turn": turn,
        "request_id": request_id,
        "reason": reason,
        "attempt": attempt,
        "message": text[:200],
    })


def run_agent(
    prompt: str,
    system_prompt: str,
    tools_dict: ToolsDict,
    agent_settings: Dict[str, Any],
    previous_messages: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    global LAST_RUN_SUMMARY, CURRENT_MESSAGES
    LAST_RUN_SUMMARY = None

    # Install middleware hooks (idempotent — clears previous hooks first)
    hooks.clear()
    from localcode.middleware import feedback_hook, metrics_hook, conversation_dump
    logging_hook.install(log_path=logging_hook.get_log_path(), run_context={
        "run_name": RUN_NAME, "task_id": TASK_ID,
        "task_index": TASK_INDEX, "task_total": TASK_TOTAL,
        "agent": AGENT_NAME,
    })
    feedback_hook.install(tools_dict=tools_dict, display_map=TOOL_DISPLAY_MAP)
    feedback_hook.set_functions(
        build_feedback_text_fn=build_feedback_text,
        display_tool_name_fn=display_tool_name,
    )
    _metrics = metrics_hook.install()
    conversation_dump.install()

    messages = (previous_messages or []) + [{"role": "user", "content": prompt}]
    CURRENT_MESSAGES = messages  # Keep global reference for tools like plan_solution
    turns = 0
    last_request_id: Optional[str] = None
    task_header_printed = False

    # Emit agent_start hook (also resets _metrics)
    hooks.emit("agent_start", {
        "prompt": prompt,
        "settings": agent_settings,
        "messages": messages,
    })

    format_retries = 0

    min_tool_calls = int(agent_settings.get("min_tool_calls", 0) or 0)
    max_format_retries = int(agent_settings.get("max_format_retries", 0) or 0)

    request_overrides = agent_settings.get("request_overrides", {}) or {}
    base_tool_choice_required = is_tool_choice_required(request_overrides.get("tool_choice"))

    auto_tool_call_on_failure = bool(agent_settings.get("auto_tool_call_on_failure", False))
    auto_tool_calls_used = 0

    require_code_change = bool(agent_settings.get("require_code_change", False))
    code_change_made = False
    code_change_retries = 0
    forced_tool_choice: Optional[str] = None

    native_thinking = bool(agent_settings.get("native_thinking", False))

    format_retry_turns = 0  # Track turns consumed by format retries (not counted toward MAX_TURNS)

    while True:
        turns += 1
        hooks.emit("turn_start", {"turn": turns, "messages": messages})
        if turns > MAX_TURNS * 3:
            # Hard cap: prevent infinite loops even with format retries
            logging_hook.log_event("agent_abort", {"reason": "hard_turn_limit", "turns": turns})
            return "error: hard turn limit reached", messages
        if (turns - format_retry_turns) > MAX_TURNS:
            summary = _metrics.summary()
            LAST_RUN_SUMMARY = summary
            logging_hook.log_event("agent_abort", {"reason": "max_turns", "turns": turns, **summary})
            return "error: max turns reached", messages

        request_messages = messages

        current_overrides = request_overrides
        enforced_tool_choice = None
        enforced_tool_choice_display = None
        if forced_tool_choice:
            enforced_tool_choice = forced_tool_choice
            enforced_tool_choice_display = TOOL_DISPLAY_MAP.get(enforced_tool_choice, enforced_tool_choice)
            current_overrides = dict(request_overrides)
            current_overrides["tool_choice"] = {"type": "function", "function": {"name": enforced_tool_choice_display}}
            logging_hook.log_event("forced_tool_choice", {"turn": turns, "tool": enforced_tool_choice})

        tool_choice_required = is_tool_choice_required(current_overrides.get("tool_choice")) or base_tool_choice_required

        response = call_api(request_messages, system_prompt, tools_dict, current_overrides)
        last_request_id = response.get("request_id")

        if response.get("error"):
            logging_hook.log_event("api_error", {"turn": turns, "error": response["error"]})
            return f"error: {response['error']}", messages

        usage_info = format_usage_info(response.get("usage"), response.get("timings"))
        if usage_info:
            print(f"{DIM}[{usage_info}]{RESET}")

        if not task_header_printed:
            _print_task_header(CONTINUE_SESSION, last_request_id)
            task_header_printed = True

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        raw_content = message.get("content", "") or ""
        content, was_analysis = normalize_analysis_only(raw_content)
        content = content or ""
        if was_analysis:
            logging_hook.log_event("analysis_artifact_normalized", {"turn": turns, "original_len": len(raw_content)})
            _metrics.analysis_retries += 1
        tool_calls = message.get("tool_calls", []) or []
        thinking = message.get("thinking")
        if not thinking:
            thinking = message.get("reasoning_content")

        if thinking and native_thinking:
            t = str(thinking).strip()
            if t:
                logging_hook.log_event("thinking_captured", {"turn": turns, "chars": len(t)})

        tool_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
        resolved_tool_names = [resolve_tool_name(n) for n in tool_names]
        tool_call_ids = [tc.get("id", "") for tc in tool_calls]
        logging_hook.log_event("response", {
            "turn": turns,
            "tool_calls": tool_names,
            "tool_calls_resolved": resolved_tool_names,
            "tool_call_count": len(tool_calls),
            "content_len": len(content),
            "content_preview": content[:200],
            "request_id": response.get("request_id"),
            "tool_call_ids": tool_call_ids,
        })
        if _turn_summary_enabled():
            visible_thinking = thinking if _thinking_visible() else None
            _print_turn_summary(turns, tool_calls, content, visible_thinking)

        # Enforced tool choice mismatch handling
        if enforced_tool_choice:
            mismatch = [n for n in resolved_tool_names if n and n != enforced_tool_choice]
            if mismatch or not tool_calls:
                if format_retries < max_format_retries:
                    format_retries += 1
                    forced_tool_choice = enforced_tool_choice
                    display_name = enforced_tool_choice_display or enforced_tool_choice
                    messages.append({"role": "user", "content":
                        f"FORMAT ERROR (attempt {format_retries}/{max_format_retries}): "
                        f"TOOL CALL REQUIRED: {display_name}. Output ONLY that tool call (no text, no other tools)."
                    })
                    logging_hook.log_event("format_retry", {
                        "turn": turns,
                        "reason": "forced_tool_choice_mismatch",
                        "expected_tool": enforced_tool_choice,
                        "actual_tools": resolved_tool_names,
                    })
                    format_retry_turns += 1
                    continue
                return "error: forced tool choice mismatch", messages
            forced_tool_choice = None

        # No tool calls -> maybe final content / maybe retry
        if not tool_calls:
            # Analysis-only artifact: never treat as final content — retry
            if was_analysis:
                if format_retries < max_format_retries:
                    format_retries += 1
                    messages.append({"role": "user", "content":
                        f"FORMAT ERROR (attempt {format_retries}/{max_format_retries}): "
                        "analysis-only artifact detected; output final content or a tool call."
                    })
                    logging_hook.log_event("format_retry", {"turn": turns, "reason": "analysis_only_no_tool_calls"})
                    format_retry_turns += 1
                    continue
                # Exhausted retries — return error, never treat analysis as final content
                logging_hook.log_event("analysis_only_exhausted", {"turn": turns, "retries": format_retries})
                return "error: analysis-only output after retries exhausted", messages

            if min_tool_calls and _metrics.tool_calls_total < min_tool_calls:
                if format_retries < max_format_retries:
                    format_retries += 1
                    if tool_choice_required:
                        tool_list = ", ".join(sorted(TOOL_DISPLAY_MAP.get(n, n) for n in tools_dict.keys()))
                        forced_name, _ = select_forced_tool_call(prompt, tools_dict)
                        if forced_name:
                            forced_tool_choice = forced_name
                        messages.append({"role": "user", "content":
                            f"FORMAT ERROR (attempt {format_retries}/{max_format_retries}): "
                            f"TOOL CALL REQUIRED. Output ONLY a tool call (no text). Available tools: {tool_list}."
                        })
                    else:
                        messages.append({"role": "user", "content":
                            f"FORMAT ERROR (attempt {format_retries}/{max_format_retries}): "
                            "Use at least one tool call before finishing."
                        })
                    logging_hook.log_event("format_retry", {"turn": turns, "reason": "min_tool_calls_not_met"})
                    format_retry_turns += 1
                    continue

                if auto_tool_call_on_failure and auto_tool_calls_used < 1:
                    tn, ta = select_forced_tool_call(prompt, tools_dict)
                    if tn:
                        auto_tool_calls_used += 1
                        tool_call_id = f"call_forced_{auto_tool_calls_used}"
                        display_name = display_tool_name(tn)
                        tool_call = {"id": tool_call_id, "type": "function", "function": {"name": display_name, "arguments": json.dumps(ta)}}
                        resolved_name, resolved_args, result, response_name = process_tool_call(tools_dict, tool_call)
                        error_detected = is_tool_error(resolved_name, result)
                        if error_detected:
                            _metrics.tool_errors_total += 1
                            _metrics.tool_error_counts[resolved_name] = _metrics.tool_error_counts.get(resolved_name, 0) + 1
                        elif is_write_tool(resolved_name):
                            if _did_tool_make_change(resolved_name, result):
                                code_change_made = True
                        _metrics.tool_calls_total += 1
                        _metrics.tool_call_counts[resolved_name] = _metrics.tool_call_counts.get(resolved_name, 0) + 1
                        messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
                        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": display_name, "content": result})
                        logging_hook.log_event("forced_tool_call", {"turn": turns, "tool": resolved_name, "tool_display": display_name})
                        continue

                return "error: minimum tool calls not met", messages

            if require_code_change and not code_change_made:
                if code_change_retries < max_format_retries:
                    code_change_retries += 1
                    forced_name = select_code_change_tool(tools_dict)
                    if forced_name:
                        forced_tool_choice = forced_name
                    # Build dynamic list of available code change tools using categories
                    available_write_tools = get_available_write_tools(tools_dict)
                    tools_str = "/".join(available_write_tools) if available_write_tools else "write_file"
                    messages.append({"role": "user", "content":
                        f"FORMAT ERROR (attempt {code_change_retries}/{max_format_retries}): "
                        f"TOOL CALL REQUIRED: {tools_str}. Output ONLY that tool call (no text, no other tools)."
                    })
                    logging_hook.log_event("format_retry", {"turn": turns, "reason": "code_change_required"})
                    format_retry_turns += 1
                    continue
                return "error: code change required", messages

            # Final content
            if content:
                print(f"\n{CYAN}⏺{RESET} {content}")
                hooks.emit("response_content", {"content": content, "turn": turns})

            messages.append(message)
            summary = _metrics.summary()
            LAST_RUN_SUMMARY = summary
            logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})

            # Hook: agent_end — triggers conversation dump and other end-of-run hooks
            end_data = hooks.emit("agent_end", {
                "summary": summary,
                "messages": messages,
                "content": content,
                "turns": turns,
                "log_path": logging_hook.get_log_path(),
                "system_prompt": system_prompt,
            })
            # Log conversation dump result if the hook produced one
            dump_info = end_data.get("conversation_dump")
            if dump_info:
                logging_hook.log_event("conversation_saved", dump_info)

            return content, messages

        # Tool calls path
        assistant_message: Dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        # For "native" mode, include thinking directly in assistant message
        if native_thinking and thinking:
            # GLM uses reasoning_content field, Harmony uses thinking field
            if "glm" in MODEL.lower():
                assistant_message["reasoning_content"] = thinking
            else:
                assistant_message["thinking"] = thinking
        tool_results: List[Dict[str, Any]] = []

        feedback_text = None
        feedback_reason = None
        for tc in tool_calls:
            # Hook: tool_before (mutable — hooks can modify tool_args)
            tc_display_name = (tc.get("function") or {}).get("name") or ""
            before_data = hooks.emit("tool_before", {
                "tool_name": resolve_tool_name(tc_display_name),
                "tool_args": (tc.get("function") or {}).get("arguments", "{}"),
                "tool_call": tc,
            })

            resolved_name, tool_args, result, response_name = process_tool_call(tools_dict, tc)

            # Pretty print
            arg_preview = ""
            if isinstance(tool_args, dict) and tool_args:
                arg_preview = str(next(iter(tool_args.values())))[:50]
            print(f"\n{GREEN}⏺ tool {tc_display_name or response_name or resolved_name}{RESET}({DIM}{arg_preview}{RESET})")
            first_line = (result.splitlines()[0] if isinstance(result, str) and result else str(result))[:60]
            print(f"  {DIM}⎿  {first_line}{RESET}")

            path_value = tool_args.get("path") if isinstance(tool_args, dict) else None
            if not path_value and resolved_name == "apply_patch" and isinstance(tool_args, dict):
                patch_text = tool_args.get("patch")
                if isinstance(patch_text, str):
                    path_value = extract_patch_file(patch_text)

            error_detected = is_tool_error(resolved_name, result)

            # Hook: tool_after — metrics_hook counts calls/errors, feedback_hook sets feedback
            after_data = hooks.emit("tool_after", {
                "tool_name": resolved_name,
                "tool_args": tool_args,
                "result": result,
                "is_error": error_detected,
                "path_value": path_value,
                "patch_fail_count": _metrics.get_patch_fail_count(path_value) if path_value else 0,
                "tool_call": tc,
                "turn": turns,
            })

            # Check if feedback hook set feedback text
            if after_data.get("feedback_text") and not feedback_text:
                feedback_text = after_data["feedback_text"]
                feedback_reason = after_data.get("feedback_reason")

            if not error_detected:
                if resolved_name in ("apply_patch", "patch_files") and path_value:
                    _metrics.clear_patch_fail(path_value)
                    if _did_tool_make_change(resolved_name, result):
                        code_change_made = True
                elif is_write_tool(resolved_name):
                    if _did_tool_make_change(resolved_name, result):
                        code_change_made = True

            logging_hook.log_event("tool_result", {
                "turn": turns,
                "tool": tc_display_name or response_name or resolved_name,
                "tool_resolved": resolved_name,
                "tool_display": tc_display_name or None,
                "tool_call_id": tc.get("id", ""),
                "request_id": last_request_id,
                "path": path_value,
                "args_preview": arg_preview,
                "result_len": len(result or "") if isinstance(result, str) else len(str(result)),
                "result_preview": (result or "")[:200] if isinstance(result, str) else str(result)[:200],
            })

            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": tc_display_name or response_name or resolved_name,
                "content": result,
            })

            if feedback_text:
                break

        messages.append(assistant_message)
        messages.extend(tool_results)

        if feedback_text:
            attempt_num = None
            if feedback_reason:
                _metrics.feedback_counts[feedback_reason] += 1
                attempt_num = _metrics.feedback_counts[feedback_reason]
                lines = feedback_text.splitlines()
                if lines:
                    lines[0] = f"{lines[0]} (attempt {attempt_num})"
                    feedback_text = "\n".join(lines)
            # Hook: tool_feedback (mutable — hooks can modify feedback_text)
            fb_data = hooks.emit("tool_feedback", {
                "tool_name": feedback_reason or "tool_error_feedback",
                "reason": feedback_reason,
                "feedback_text": feedback_text,
                "turn": turns,
                "attempt": attempt_num,
            })
            feedback_text = fb_data.get("feedback_text", feedback_text)
            _append_feedback(
                messages,
                turns,
                last_request_id,
                feedback_text,
                feedback_reason or "tool_error_feedback",
                attempt=attempt_num,
            )
            # Hook: turn_end
            hooks.emit("turn_end", {"turn": turns, "tool_count": len(tool_calls), "errors": _metrics.tool_errors_total})
            continue


def run_once(prompt: str, system_prompt: str, tools_dict: ToolsDict) -> None:
    init_logging()
    continue_mode = CONTINUE_SESSION
    print(f"{BOLD}localcode[{AGENT_NAME}]{RESET} {'(continue)' if continue_mode else ''} | {DIM}{MODEL} | {os.getcwd()}{RESET}\n")

    previous = None
    if continue_mode:
        previous = load_session(AGENT_NAME or "agent")
        if previous:
            print(f"{DIM}Loaded session: {CURRENT_SESSION_PATH} ({len(previous)} messages){RESET}")
        else:
            print(f"{DIM}No previous session found, starting fresh{RESET}")
            init_new_session(AGENT_NAME or "agent")
    else:
        init_new_session(AGENT_NAME or "agent")

    logging_hook.log_event("run_start", {
        "prompt_len": len(prompt or ""),
        "prompt_preview": (prompt or "")[:200],
        "continue_session": continue_mode,
        "previous_message_count": len(previous) if previous else 0,
    })

    content, messages = run_agent(prompt, system_prompt, tools_dict, AGENT_SETTINGS, previous)

    save_session(AGENT_NAME or "agent", messages, MODEL)

    global LAST_RUN_SUMMARY
    logging_hook.log_event("run_end", LAST_RUN_SUMMARY or {})


# ---------------------------
# Interactive commands
# ---------------------------

def clear_server_cache() -> Dict[str, Any]:
    try:
        req = urllib.request.Request(
            API_URL.replace("/v1/chat/completions", "/cache/clear"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def cmd_clear() -> None:
    init_new_session(AGENT_NAME or "agent")
    print(f"{GREEN}✓{RESET} New session: {DIM}{CURRENT_SESSION_PATH}{RESET}")
    result = clear_server_cache()
    if "error" in result:
        print(f"{RED}✗{RESET} Cache clear failed: {result['error']}")
    else:
        freed = result.get("memory_freed_mb", 0)
        print(f"{GREEN}✓{RESET} Server cache cleared ({freed}MB freed)")
    FILE_VERSIONS.clear()
    _reset_noop_tracking()
    print(f"{GREEN}✓{RESET} Local file cache cleared")


def cmd_status() -> None:
    print(f"\n{BOLD}Session{RESET}")
    print(f"  Agent: {AGENT_NAME}")
    print(f"  Model: {MODEL}")
    print(f"  Session: {CURRENT_SESSION_PATH or '(none)'}")
    print(f"  Local files tracked: {len(FILE_VERSIONS)}")
    print()


def cmd_help() -> None:
    print(f"\n{BOLD}Commands{RESET}")
    print("  /clear   - Clear conversation & server cache, start new session")
    print("  /status  - Show session and cache status")
    print("  /help    - Show this help")
    print("  /q, exit - Quit")
    print()


COMMANDS = {
    "/clear": cmd_clear,
    "/status": cmd_status,
    "/help": cmd_help,
    "/h": cmd_help,
    "/?": cmd_help,
}


def main(system_prompt: str, tools_dict: ToolsDict) -> None:
    global INTERACTIVE_MODE
    INTERACTIVE_MODE = True
    print(LOGO)
    print(f"{BOLD}localcode[{AGENT_NAME}]{RESET} | {DIM}{MODEL} | {os.getcwd()}{RESET}")
    print(f"{DIM}Type /help for commands{RESET}\n")

    while True:
        try:
            user_input = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
            if not user_input:
                continue
            if user_input in ("/q", "/quit", "exit"):
                break
            if user_input.startswith("/"):
                cmd = COMMANDS.get(user_input.split()[0])
                if cmd:
                    cmd()
                    continue
                print(f"{RED}Unknown command:{RESET} {user_input}. Type /help for available commands.")
                continue
            run_once(user_input, system_prompt, tools_dict)
        except (KeyboardInterrupt, EOFError):
            break


# ---------------------------
# Entrypoint
# ---------------------------

# Session path
CURRENT_SESSION_PATH: Optional[str] = None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="localcode - agent runner with native tool calls")
    parser.add_argument("--agent", "-a", required=True, help="Agent name (from agents/*.json)")
    parser.add_argument("prompt", nargs="?", help="Prompt to run (non-interactive mode)")
    parser.add_argument("--continue", "-c", dest="continue_session", action="store_true", help="Continue from previous session")
    parser.add_argument("--model", "-m", help="Model to use (overrides agent config)")
    parser.add_argument("--file", "-f", action="append", help="Read prompt from file (first --file is used as prompt)")
    parser.add_argument("--url", help="API URL (overrides default)")
    parser.add_argument("--temperature", type=float, help="Temperature")
    parser.add_argument("--top_p", type=float, help="Top-p")
    parser.add_argument("--top_k", type=int, help="Top-k")
    parser.add_argument("--min_p", type=float, help="Min-p")
    parser.add_argument("--max_tokens", type=int, help="Max tokens")
    parser.add_argument("--no-sandbox", dest="no_sandbox", action="store_true", help="Disable sandbox protection")

    filtered_args, extra_args = split_cli_overrides(sys.argv[1:])
    args = parser.parse_args(filtered_args)

    AGENT_NAME = args.agent
    CONTINUE_SESSION = bool(args.continue_session)

    if not args.no_sandbox:
        SANDBOX_ROOT = os.getcwd()
        _tool_state.SANDBOX_ROOT = SANDBOX_ROOT

    # Apply CLI overrides & config
    tool_defs = load_tool_defs(TOOL_DIR)
    alias_map = build_tool_alias_map(tool_defs)
    agent_defs = load_agent_defs(AGENT_DIR)
    if AGENT_NAME not in agent_defs:
        available = ", ".join(sorted(agent_defs.keys()))
        raise SystemExit(f"Unknown agent '{AGENT_NAME}'. Available agents: {available}")

    agent_config = agent_defs[AGENT_NAME]
    agent_config = apply_cli_overrides(agent_config, extra_args)

    MODEL = args.model or agent_config.get("model") or DEFAULT_MODEL
    API_URL = args.url or agent_config.get("url") or API_URL
    MAX_TOKENS = args.max_tokens or agent_config.get("max_tokens") or MAX_TOKENS

    INFERENCE_PARAMS["temperature"] = args.temperature if args.temperature is not None else agent_config.get("temperature", 0)
    INFERENCE_PARAMS["top_p"] = args.top_p if args.top_p is not None else agent_config.get("top_p")
    INFERENCE_PARAMS["top_k"] = args.top_k if args.top_k is not None else agent_config.get("top_k")
    INFERENCE_PARAMS["min_p"] = args.min_p if args.min_p is not None else agent_config.get("min_p")
    INFERENCE_PARAMS["presence_penalty"] = agent_config.get("presence_penalty")
    INFERENCE_PARAMS["frequency_penalty"] = agent_config.get("frequency_penalty")

    tool_order, tool_order_raw = resolve_tool_order(agent_config, alias_map)
    display_map = resolve_tool_display_map(agent_config, tool_defs, tool_order, tool_order_raw, alias_map)
    TOOL_ALIAS_MAP.clear()
    TOOL_ALIAS_MAP.update(alias_map)
    _tool_state.TOOL_ALIAS_MAP.update(alias_map)
    TOOL_DISPLAY_MAP.clear()
    TOOL_DISPLAY_MAP.update(display_map)
    _tool_state.TOOL_DISPLAY_MAP.update(display_map)
    TOOL_CATEGORIES.clear()
    TOOL_CATEGORIES.update(build_tool_category_map(tool_defs))
    system_prompt = load_system_prompt(agent_config, tool_defs, tool_order, display_map)
    AGENT_SETTINGS = build_agent_settings(agent_config)

    TOOL_HANDLERS = {
        "read": read,
        "batch_read": batch_read,
        "write": write,
        "edit": edit,
        "apply_patch": apply_patch_fn,
        "glob": glob_fn,
        "grep": grep_fn,
        "search": search_fn,
        "shell": shell,
        "ls": ls_fn,
    }

    # Dynamically register model_call handlers from tool JSON configs
    for _tn, _td in tool_defs.items():
        _mc = _td.get("model_call")
        if _mc and isinstance(_mc, dict):
            _hk = _td.get("handler", _tn)
            TOOL_HANDLERS[_hk] = make_model_call_handler(_tn, _mc)
    tools_dict = build_tools(tool_defs, TOOL_HANDLERS, tool_order)

    # Determine prompt
    prompt = None
    if args.file:
        with open(args.file[0], "r", encoding="utf-8") as f:
            prompt = f.read().strip()
    elif args.prompt:
        prompt = args.prompt

    if RUN_NAME is None:
        RUN_NAME = os.environ.get("LOCALCODE_RUN_NAME") or os.environ.get("RUN_NAME") or os.environ.get("NAME")
    if TASK_ID is None:
        TASK_ID = os.environ.get("LOCALCODE_TASK_ID")
    if TASK_INDEX is None:
        value = os.environ.get("LOCALCODE_TASK_INDEX")
        if value:
            try:
                TASK_INDEX = int(value)
            except ValueError:
                TASK_INDEX = None
    if TASK_TOTAL is None:
        value = os.environ.get("LOCALCODE_TASK_TOTAL")
        if value:
            try:
                TASK_TOTAL = int(value)
            except ValueError:
                TASK_TOTAL = None
    if args.file and args.file[0]:
        if not RUN_NAME:
            RUN_NAME = infer_run_name_from_path(args.file[0])
        if not TASK_ID:
            TASK_ID = infer_task_id_from_path(args.file[0])

    if prompt:
        run_once(prompt, system_prompt, tools_dict)
    else:
        main(system_prompt, tools_dict)


# Module __setattr__ trick: intercept assignments to SANDBOX_ROOT on this
# module and automatically sync to _tool_state.SANDBOX_ROOT, which is the
# single source of truth used by all tool handler modules.
# This ensures tests doing `_inner.SANDBOX_ROOT = val` work correctly.
class _SyncModule(type(sys.modules[__name__])):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name == "SANDBOX_ROOT":
            _tool_state.SANDBOX_ROOT = value

sys.modules[__name__].__class__ = _SyncModule
