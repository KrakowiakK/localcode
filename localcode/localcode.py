#!/usr/bin/env python3
"""
localcode - agent runner with native tool calls.

- Sandbox-safe filesystem tools.
- Robust tool arg repair (number words for read line ranges).
- Tool handlers extracted to localcode/tool_handlers/ package.
"""

import argparse
import copy
import glob as globlib
import json
import os
import re
import sys
import time
import urllib.request
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

# Phase 3: config, session, model_calls extracted
from localcode.config import (
    load_json,
    load_text,
    normalize_bool_auto,
    is_tool_choice_required,
    _coerce_cli_value,
    apply_cli_overrides,
    split_cli_overrides,
)
from localcode.session import (
    summarize_messages,
    infer_run_name_from_path,
    infer_task_id_from_path,
)
from localcode.session import (
    create_new_session_path as _session_create_new_session_path,
    find_latest_session as _session_find_latest_session,
    init_logging as _session_init_logging,
    sync_logging_context as _session_sync_logging_context,
    save_session as _session_save_session,
    load_session as _session_load_session,
    init_new_session as _session_init_new_session,
)
from localcode.model_calls import (
    _load_prompt_file as _load_prompt_file_impl,
    _self_call as _self_call_impl,
    _self_call_batch as _self_call_batch_impl,
    _subprocess_call as _subprocess_call_impl,
    make_model_call_handler as _make_model_call_handler_impl,
)

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
FINISH_SIGNAL: Optional[Dict[str, Any]] = None
LAST_REQUEST_SNAPSHOT: Optional[Dict[str, Any]] = None

# ANSI colors
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, RED, YELLOW = "\033[34m", "\033[36m", "\033[32m", "\033[31m", "\033[33m"

LOGO = f"""{CYAN}██╗      ██████╗  ██████╗  █████╗ ██╗      ██████╗ ██████╗ ██████╗ ███████╗
██║     ██╔═══██╗██╔════╝ ██╔══██╗██║     ██╔════╝██╔═══██╗██╔══██╗██╔════╝
██║     ██║   ██║██║      ███████║██║     ██║     ██║   ██║██║  ██║█████╗
██║     ██║   ██║██║      ██╔══██║██║     ██║     ██║   ██║██║  ██║██╔══╝
███████╗╚██████╔╝╚██████╗ ██║  ██║███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚══════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
                                                         by webdroid.co.uk{RESET}"""

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


def _is_benchmark_mode() -> bool:
    value = os.environ.get("LOCALCODE_BENCHMARK", "")
    if str(value).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return bool(os.environ.get("BENCHMARK_DIR") or os.environ.get("AIDER_DOCKER"))


def _benchmark_final_output(continue_mode: bool) -> str:
    return "Finished Try2" if continue_mode else "Finished Try1"


def _infer_task_label() -> str:
    if TASK_ID:
        return TASK_ID
    try:
        cwd_base = os.path.basename(os.getcwd())
    except Exception:
        cwd_base = ""
    if cwd_base:
        return cwd_base
    if RUN_NAME:
        return RUN_NAME
    return "unknown"


def _format_task_label(continue_mode: bool, request_id: Optional[str]) -> str:
    task = _infer_task_label()
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


# infer_run_name_from_path and infer_task_id_from_path imported from session.py
# load_json, load_text, normalize_bool_auto, is_tool_choice_required imported from config.py
# _coerce_cli_value, apply_cli_overrides, split_cli_overrides imported from config.py
# summarize_messages imported from session.py


# ---------------------------
# Logging / Sessions
# ---------------------------

# ---------------------------
# Session wrappers (delegate to session.py, bridge globals)
# ---------------------------

def create_new_session_path(agent_name: str) -> str:
    return _session_create_new_session_path(agent_name, SESSION_DIR)


def find_latest_session(agent_name: str) -> Optional[str]:
    return _session_find_latest_session(agent_name, SESSION_DIR)


def init_logging() -> None:
    """Initialize JSONL logging via logging_hook."""
    _session_init_logging(
        log_dir=LOG_DIR,
        agent_name=AGENT_NAME,
        model=MODEL,
        agent_settings=AGENT_SETTINGS,
        run_name=RUN_NAME,
        task_id=TASK_ID,
        task_index=TASK_INDEX,
        task_total=TASK_TOTAL,
    )


def _sync_logging_context() -> None:
    """Sync global state into logging_hook run context."""
    _session_sync_logging_context(
        agent_name=AGENT_NAME,
        run_name=RUN_NAME,
        task_id=TASK_ID,
        task_index=TASK_INDEX,
        task_total=TASK_TOTAL,
    )


def save_session(agent_name: str, messages: List[Dict[str, Any]], model: str) -> None:
    global CURRENT_SESSION_PATH
    if CURRENT_SESSION_PATH is None:
        CURRENT_SESSION_PATH = create_new_session_path(agent_name)
    _session_save_session(agent_name, messages, model, CURRENT_SESSION_PATH)


def load_session(agent_name: str) -> List[Dict[str, Any]]:
    global CURRENT_SESSION_PATH
    msgs, path = _session_load_session(agent_name, SESSION_DIR)
    if path:
        CURRENT_SESSION_PATH = path
    return msgs


def init_new_session(agent_name: str) -> None:
    global CURRENT_SESSION_PATH
    CURRENT_SESSION_PATH = _session_init_new_session(agent_name, SESSION_DIR)


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


def build_tool_category_map(
    tool_defs: Dict[str, Dict[str, Any]],
    active_tools: Optional[List[str]] = None,
    display_map: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Extract tool categories for the active tool set.

    Returns a map of tool_name -> category (e.g., "read"/"write"), including:
    - canonical tool names
    - aliases declared in tool definitions
    - display aliases exposed to the model (if provided)

    If active_tools is None, all tool definitions are included.
    """
    category_map: Dict[str, str] = {}
    selected_tools = active_tools if active_tools is not None else list(tool_defs.keys())

    for name in selected_tools:
        tool_def = tool_defs.get(name)
        if not isinstance(tool_def, dict):
            continue
        category = tool_def.get("category")
        if not isinstance(category, str):
            continue
        category = category.strip().lower()
        if not category:
            continue

        canonical = str(name).strip().lower()
        if canonical:
            category_map[canonical] = category

        aliases = tool_def.get("aliases") or []
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            alias_key = alias.strip().lower()
            if alias_key:
                category_map[alias_key] = category

        if isinstance(display_map, dict):
            display_name = display_map.get(name)
            if isinstance(display_name, str):
                display_key = display_name.strip().lower()
                if display_key:
                    category_map[display_key] = category
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


def _normalize_overlay_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _select_prompt_overlay(agent_config: Dict[str, Any]) -> Optional[str]:
    overlays = agent_config.get("prompt_overlays")
    if not isinstance(overlays, dict) or not overlays:
        return None

    valid_keys: List[str] = []
    for key, value in overlays.items():
        if isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip():
            valid_keys.append(key)
    if not valid_keys:
        return None

    forced = str(os.environ.get("LOCALCODE_PROMPT_OVERLAY") or "").strip().lower()
    if forced:
        for key in valid_keys:
            if forced == str(key).strip().lower():
                return key

    probes: List[str] = []
    env_task_id = str(os.environ.get("LOCALCODE_TASK_ID") or "").strip()
    if env_task_id:
        probes.append(env_task_id)
    if TASK_ID:
        probes.append(str(TASK_ID))
    cwd = os.getcwd()
    if cwd:
        probes.append(cwd)
        probes.append(os.path.basename(cwd))

    normalized_probes = [_normalize_overlay_token(p) for p in probes if p]
    normalized_probes = [p for p in normalized_probes if p]
    if not normalized_probes:
        return None

    for key in valid_keys:
        token = _normalize_overlay_token(key)
        if not token:
            continue
        if any(token in probe for probe in normalized_probes):
            return key
    return None


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
    prompt = render_tool_description(prompt, display_map)

    overlay_key = _select_prompt_overlay(agent_config)
    if overlay_key:
        overlays = agent_config.get("prompt_overlays") or {}
        overlay_rel = overlays.get(overlay_key)
        if isinstance(overlay_rel, str) and overlay_rel.strip():
            overlay_path = overlay_rel if os.path.isabs(overlay_rel) else os.path.join(BASE_DIR, overlay_rel)
            overlay_text = load_text(overlay_path)
            overlay_text = overlay_text.replace("{{TOOLS}}", tool_list)
            overlay_text = render_tool_description(overlay_text, display_map).strip()
            if overlay_text:
                prompt = f"{prompt.rstrip()}\n\n{overlay_text}\n"
    return prompt


DEPRECATED_AGENT_SETTING_KEYS = {
    "task_branching",
    "task_flow_mode",
    "task_replan_max",
    "task_plan_mode",
    "task_skip_readonly",
    "task_output_mode",
    "benchmark_output_mode",
    "flow",
    "flow_stage_retries",
    "flow_stage_required",
    "history_mode",
    "history_keep_first",
    "history_strip_thinking",
    "history_tool_truncate_chars",
    "history_tool_truncate_keep_last",
    "history_tool_call_args_truncate_chars",
    "history_tool_call_args_keep_last",
    "flow_history_mode",
    "flow_history_max_messages",
    "flow_history_keep_first",
    "flow_history_strip_thinking",
    "flow_history_tool_truncate_chars",
    "flow_history_tool_truncate_keep_last",
    "flow_context_window",
    "flow_retry_hints",
    "phase_control",
    "phase_log",
    "progress_injection",
    "progress_max_tokens",
    "read_loop_nudge_threshold",
    "finish_nudge_remaining_turns",
}


def build_agent_settings(agent_config: Dict[str, Any]) -> Dict[str, Any]:
    settings = {
        "request_overrides": {},
        "min_tool_calls": 0,
        "max_format_retries": 0,
        "auto_tool_call_on_failure": False,
        "require_code_change": False,
        "native_thinking": False,
        "thinking_visibility": "show",
        "history_max_messages": 0,
        "max_turns": MAX_TURNS,
        "send_tool_categories": True,
        "deprecated_ignored_keys": [],
    }

    if "min_tool_calls" in agent_config:
        settings["min_tool_calls"] = max(0, int(agent_config["min_tool_calls"] or 0))
    if "max_format_retries" in agent_config:
        settings["max_format_retries"] = max(0, int(agent_config["max_format_retries"] or 0))
    if "max_turns" in agent_config:
        settings["max_turns"] = max(1, int(agent_config["max_turns"] or 0))

    for k in ("auto_tool_call_on_failure", "require_code_change"):
        if k in agent_config:
            settings[k] = bool(agent_config[k])
    if "history_max_messages" in agent_config:
        settings["history_max_messages"] = max(0, int(agent_config["history_max_messages"] or 0))
    env_history_max = os.environ.get("LOCALCODE_HISTORY_MAX_MESSAGES", "")
    if env_history_max:
        try:
            settings["history_max_messages"] = max(0, int(env_history_max))
        except ValueError:
            pass

    if "send_tool_categories" in agent_config:
        settings["send_tool_categories"] = bool(agent_config["send_tool_categories"])
    env_send_tool_categories = os.environ.get("LOCALCODE_SEND_TOOL_CATEGORIES", "")
    if env_send_tool_categories:
        settings["send_tool_categories"] = str(env_send_tool_categories).strip().lower() in {"1", "true", "yes", "on"}
    if "native_thinking" in agent_config:
        settings["native_thinking"] = bool(agent_config["native_thinking"])
    if "thinking_visibility" in agent_config:
        settings["thinking_visibility"] = str(agent_config["thinking_visibility"] or "show").strip().lower()
    if settings["thinking_visibility"] not in {"show", "hidden"}:
        settings["thinking_visibility"] = "show"

    overrides: Dict[str, Any] = {}
    raw_overrides = agent_config.get("request_overrides", {})
    if raw_overrides:
        if not isinstance(raw_overrides, dict):
            raise ValueError("Agent config 'request_overrides' must be an object")
        overrides.update(raw_overrides)

    if "tool_choice" in agent_config:
        overrides.setdefault("tool_choice", agent_config["tool_choice"])

    cache_value = normalize_bool_auto(agent_config.get("cache"), "cache")
    if cache_value is not None:
        overrides.setdefault("cache", cache_value)
    think_value = normalize_bool_auto(agent_config.get("think"), "think")
    if think_value is not None:
        overrides.setdefault("think", think_value)
    think_level_value = agent_config.get("think_level")
    if think_level_value is None and "thinking_level" in agent_config:
        think_level_value = agent_config.get("thinking_level")
    if think_level_value is not None:
        overrides.setdefault("reasoning_effort", think_level_value)
    if think_value is True or settings["native_thinking"]:
        overrides.setdefault("return_thinking", True)

    # Pass max_batch_tool_calls to API for GPT-OSS batching control
    if "max_batch_tool_calls" in agent_config:
        overrides.setdefault("max_batch_tool_calls", agent_config["max_batch_tool_calls"])

    settings["request_overrides"] = overrides
    settings["deprecated_ignored_keys"] = sorted(
        key for key in DEPRECATED_AGENT_SETTING_KEYS if key in agent_config
    )
    return settings


# ---------------------------
# Model call tools (self-call / subprocess)
# ---------------------------

# ---------------------------
# Model call wrappers (delegate to model_calls.py, bridge globals)
# ---------------------------

def _load_prompt_file(relative_path: str) -> str:
    """Load a prompt file relative to BASE_DIR."""
    return _load_prompt_file_impl(relative_path, BASE_DIR)


def _self_call(
    prompt: str,
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: int = 120,
    include_history: bool = True,
    user_prefix: str = "",
    include_tool_messages: bool = True,
    include_tool_call_summaries: bool = False,
    history_sanitize: bool = False,
    history_tool_result_chars: int = 500,
    history_tool_call_args_chars: int = 180,
    tool_choice: Optional[str] = "none",
) -> str:
    """Make an API call to the same model (self-reflection / thinking)."""
    return _self_call_impl(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        include_history=include_history,
        user_prefix=user_prefix,
        include_tool_messages=include_tool_messages,
        include_tool_call_summaries=include_tool_call_summaries,
        history_sanitize=history_sanitize,
        history_tool_result_chars=history_tool_result_chars,
        history_tool_call_args_chars=history_tool_call_args_chars,
        tool_choice=tool_choice,
        api_url=API_URL,
        model=MODEL,
        current_messages=CURRENT_MESSAGES,
    )


def _self_call_batch(
    questions: List[str],
    system_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout: int = 120,
    include_history: bool = True,
    max_concurrent: int = 4,
    include_tool_messages: bool = True,
    include_tool_call_summaries: bool = False,
    history_sanitize: bool = False,
    history_tool_result_chars: int = 500,
    history_tool_call_args_chars: int = 180,
    tool_choice: Optional[str] = "none",
) -> str:
    """Send multiple questions concurrently via ThreadPoolExecutor."""
    return _self_call_batch_impl(
        questions=questions,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        include_history=include_history,
        max_concurrent=max_concurrent,
        include_tool_messages=include_tool_messages,
        include_tool_call_summaries=include_tool_call_summaries,
        history_sanitize=history_sanitize,
        history_tool_result_chars=history_tool_result_chars,
        history_tool_call_args_chars=history_tool_call_args_chars,
        tool_choice=tool_choice,
        api_url=API_URL,
        model=MODEL,
        current_messages=CURRENT_MESSAGES,
    )


def _subprocess_call(
    prompt: str,
    agent: str,
    timeout_sec: int,
    files: List[str],
    config: Dict[str, Any],
) -> str:
    """Run a sub-agent via subprocess and return its cleaned response."""
    return _subprocess_call_impl(
        prompt=prompt,
        agent=agent,
        timeout_sec=timeout_sec,
        files=files,
        config=config,
        base_dir=BASE_DIR,
        api_url=API_URL,
    )


def make_model_call_handler(tool_name: str, config: Dict[str, Any]):
    """Factory that creates a tool handler from a model_call config block."""
    return _make_model_call_handler_impl(
        tool_name=tool_name,
        config=config,
        get_api_url=lambda: API_URL,
        get_model=lambda: MODEL,
        get_current_messages=lambda: CURRENT_MESSAGES,
        get_base_dir=lambda: BASE_DIR,
    )


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
    total_tps = (timings or {}).get("total_per_second", 0) or timing.get("total_tps", 0)
    elapsed_s = (timings or {}).get("elapsed_seconds", 0) or timing.get("elapsed_seconds", 0)
    tps_estimated = bool((timings or {}).get("estimated", False) or timing.get("estimated", False))

    parts = [f"{prompt_tokens}→{completion_tokens} tok"]
    if ttft:
        parts.append(f"TTFT {float(ttft):.2f}s")
    if tps_estimated:
        # Only total throughput is meaningful when estimated from wall time
        if total_tps:
            parts.append(f"~{float(total_tps):.0f} tok/s total")
        if elapsed_s:
            parts.append(f"{float(elapsed_s):.1f}s")
    else:
        # Server provided real per-phase timings
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


def extract_assistant_thinking(message: Dict[str, Any], raw_content: str) -> Optional[str]:
    """Extract assistant reasoning text from structured fields or Harmony content."""
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()

    thinking = message.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()
    if isinstance(thinking, dict):
        for key in ("content", "text", "reasoning", "analysis"):
            val = thinking.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    if isinstance(raw_content, str) and "<|channel|>analysis<|message|>" in raw_content:
        match = re.search(
            r"<\|channel\|>analysis<\|message\|>(.*?)(?:<\|end\|>|<\|channel\|>|$)",
            raw_content,
            re.DOTALL,
        )
        if match:
            extracted = match.group(1).strip()
            if extracted:
                return extracted
    return None


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
    global LAST_REQUEST_SNAPSHOT
    trimmed = trim_messages(messages)
    full_messages = [{"role": "system", "content": system_prompt}] + trimmed
    request_data: Dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": full_messages,
        "stream": False,
        "tools": make_openai_tools(tools_dict, TOOL_DISPLAY_MAP),
    }

    # Optional: include tool categories for semantic filtering.
    # Some models (e.g. smaller FC models) perform better with only canonical OpenAI fields.
    send_tool_categories = bool(AGENT_SETTINGS.get("send_tool_categories", True))
    if send_tool_categories and TOOL_CATEGORIES:
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

    request_data_before_hooks = copy.deepcopy(request_data)

    # Hook: api_request (mutable — hooks can modify request_data)
    hook_data = hooks.emit("api_request", {
        "messages": full_messages,
        "request_data": request_data,
    })
    request_data = hook_data.get("request_data", request_data)
    if tools_dict and (not isinstance(request_data.get("tools"), list) or not request_data.get("tools")):
        # Keep request complete for tool-capable agents even if a hook dropped tools by mistake.
        request_data["tools"] = request_data_before_hooks.get("tools", [])
        logging_hook.log_event("api_request_tools_restored", {
            "reason": "missing_or_empty_tools_after_hooks",
            "tool_count": len(request_data.get("tools") or []),
        })
    LAST_REQUEST_SNAPSHOT = copy.deepcopy(request_data)

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(request_data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    def _is_transient_request_error(exc: Exception) -> bool:
        text = str(exc).lower()
        transient_markers = (
            "remote end closed connection without response",
            "connection reset by peer",
            "broken pipe",
            "timed out",
            "temporarily unavailable",
        )
        return any(marker in text for marker in transient_markers)

    raw = b""
    request_elapsed_s: Optional[float] = None
    resp = None
    request_attempts = 2
    for attempt in range(1, request_attempts + 1):
        attempt_started_at = time.perf_counter()
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            raw = resp.read()
            request_elapsed_s = max(time.perf_counter() - attempt_started_at, 1e-6)
            break
        except Exception as exc:
            should_retry = attempt < request_attempts and _is_transient_request_error(exc)
            logging_hook.log_event("request_error", {
                "error": str(exc),
                "attempt": attempt,
                "retrying": should_retry,
            })
            hooks.emit("api_error", {"error": str(exc), "phase": "request", "attempt": attempt})
            if should_retry:
                time.sleep(0.2 * attempt)
                continue
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

    if request_elapsed_s is None:
        request_elapsed_s = 1e-6

    # Log usage, timings (TPS from llama-server), and request_id
    meta: Dict[str, Any] = {
        "usage": payload.get("usage", {}),
        "request_id": payload.get("request_id"),
    }
    timings = payload.get("timings")
    usage = payload.get("usage") or {}
    if not isinstance(timings, dict):
        timings = None
    if not timings and isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        # Check for real TPS from patched mlx-lm server (usage.prompt_tps / usage.generation_tps)
        mlx_prompt_tps = usage.get("prompt_tps")
        mlx_generation_tps = usage.get("generation_tps")
        if mlx_prompt_tps or mlx_generation_tps:
            total_tokens = float(prompt_tokens or 0) + float(completion_tokens or 0)
            timings = {
                "prompt_per_second": float(mlx_prompt_tps or 0),
                "predicted_per_second": float(mlx_generation_tps or 0),
                "total_per_second": (total_tokens / request_elapsed_s) if total_tokens > 0 else 0.0,
                "elapsed_seconds": request_elapsed_s,
            }
            payload["timings"] = timings
        elif isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
            total_tokens = float(prompt_tokens) + float(completion_tokens)
            timings = {
                "total_per_second": (total_tokens / request_elapsed_s) if total_tokens > 0 else 0.0,
                "elapsed_seconds": request_elapsed_s,
                "estimated": True,
            }
            payload["timings"] = timings
    if timings:
        meta["timings"] = timings
        meta["prefill_tps"] = round(timings.get("prompt_per_second", 0), 2)
        meta["decode_tps"] = round(timings.get("predicted_per_second", 0), 2)
        meta["total_tps"] = round(timings.get("total_per_second", 0), 2)
        if timings.get("estimated"):
            meta["timings_estimated"] = True
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


def _is_noop_write_result(result: str) -> bool:
    """Check if a write/edit tool result indicates no actual change was made."""
    if not isinstance(result, str):
        return False
    r = result.lower()
    if "no changes needed" in r:
        return True
    if "no changes made" in r:
        return True
    if "already correct" in r:
        return True
    if "already has this content" in r:
        return True
    if "old and new are identical" in r:
        return True
    if "old and new are the same" in r:
        return True
    if "edit not applied" in r:
        return True
    if "no change \u2014" in r:
        return True
    if result.strip() == "ok":
        return True
    return False


def _truncate_words(text: str, max_words: int) -> str:
    """Truncate text to a max number of whitespace-delimited words."""
    if not isinstance(text, str):
        return ""
    limit = int(max_words or 0)
    if limit <= 0:
        return text
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip() + " ..."


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


def finish_run(args: Dict[str, Any]) -> str:
    """Record a finish signal so the runtime can terminate cleanly."""
    global FINISH_SIGNAL
    data, err = _require_args_dict(args, "finish")
    if err:
        return err

    raw_status = str(data.get("status") or "").strip().lower()
    if not raw_status:
        status = "done"
        normalized_note = ""
    else:
        status_map = {
            "done": "done",
            "complete": "done",
            "completed": "done",
            "finished": "done",
            "success": "done",
            "ok": "done",
            "blocked": "blocked",
            "block": "blocked",
            "incomplete": "incomplete",
            "partial": "incomplete",
            "not_done": "incomplete",
            "needs_work": "incomplete",
        }
        status = status_map.get(raw_status, "")
        if not status:
            if any(tok in raw_status for tok in ("done", "complete", "finish", "success")):
                status = "done"
            elif "block" in raw_status:
                status = "blocked"
            elif any(tok in raw_status for tok in ("incomplete", "partial", "todo", "remaining", "not done")):
                status = "incomplete"
            else:
                status = "done"
        normalized_note = "" if status == raw_status else f" (normalized from '{raw_status}')"

    summary_raw = data.get("summary")
    summary = ""
    if summary_raw is not None:
        if not isinstance(summary_raw, str):
            return "error: finish.summary must be a string"
        summary = summary_raw.strip()

    FINISH_SIGNAL = {
        "status": status,
        "summary": summary,
    }
    logging_hook.log_event("finish_called", {
        "status": status,
        "status_raw": raw_status or None,
        "status_normalized": bool(normalized_note),
        "summary_preview": summary[:200],
    })
    return f"ok: finish recorded ({status}){normalized_note}"


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
    task_depth: int = 0,
) -> Tuple[str, List[Dict[str, Any]]]:
    global LAST_RUN_SUMMARY, CURRENT_MESSAGES, FINISH_SIGNAL, LAST_REQUEST_SNAPSHOT
    LAST_RUN_SUMMARY = None
    FINISH_SIGNAL = None
    LAST_REQUEST_SNAPSHOT = None

    benchmark_output_mode = "model"
    history_max_messages = max(0, int(agent_settings.get("history_max_messages", 0) or 0))
    phase_log_mode = "off"

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
    native_thinking = bool(agent_settings.get("native_thinking", False))
    thinking_visibility = str(agent_settings.get("thinking_visibility", "show") or "show").strip().lower()
    if thinking_visibility not in {"show", "hidden"}:
        thinking_visibility = "show"

    auto_tool_call_on_failure = bool(agent_settings.get("auto_tool_call_on_failure", False))
    auto_tool_calls_used = 0

    require_code_change = bool(agent_settings.get("require_code_change", False))
    code_change_made = False
    code_change_retries = 0
    forced_tool_choice: Optional[str] = None
    consecutive_noop_turns = 0

    agent_max_turns = int(agent_settings.get("max_turns", MAX_TURNS) or MAX_TURNS)

    format_retry_turns = 0  # Track turns consumed by format retries (not counted toward MAX_TURNS)

    READ_ONLY_TOOLS = {"list_dir", "read_file", "read_files", "search_text", "find_files"}
    WRITE_TOOLS = {"write_file", "replace_in_file", "patch_files"}

    def _select_request_messages(all_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if history_max_messages <= 0:
            return list(all_messages)
        total = len(all_messages)
        if total <= history_max_messages:
            return list(all_messages)
        selected = list(all_messages[-history_max_messages:])
        logging_hook.log_event("history_trim", {
            "history_max_messages": history_max_messages,
            "before": total,
            "after": len(selected),
        })
        return selected

    def _emit_agent_end_on_error(error_msg):
        """Emit agent_end hook on error paths so .log and .raw.json are always created."""
        summary = _metrics.summary()
        end_data = hooks.emit("agent_end", {
            "summary": summary,
            "messages": messages,
            "content": error_msg,
            "turns": turns,
            "log_path": logging_hook.get_log_path(),
            "system_prompt": system_prompt,
            "phase_log_mode": phase_log_mode,
            "last_request_snapshot": LAST_REQUEST_SNAPSHOT,
        })
        dump_info = end_data.get("conversation_dump")
        if dump_info:
            logging_hook.log_event("conversation_saved", dump_info)

    while True:
        turns += 1
        hooks.emit("turn_start", {"turn": turns, "messages": messages})
        if turns > agent_max_turns * 3:
            # Hard cap: prevent infinite loops even with format retries
            logging_hook.log_event("agent_abort", {"reason": "hard_turn_limit", "turns": turns})
            _emit_agent_end_on_error("error: hard turn limit reached")
            return "error: hard turn limit reached", messages
        if (turns - format_retry_turns) > agent_max_turns:
            summary = _metrics.summary()
            LAST_RUN_SUMMARY = summary
            logging_hook.log_event("agent_abort", {"reason": "max_turns", "turns": turns, **summary})
            _emit_agent_end_on_error("error: max turns reached")
            return "error: max turns reached", messages

        request_messages = _select_request_messages(messages)

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

        system_prompt_effective = system_prompt
        response = call_api(request_messages, system_prompt_effective, tools_dict, current_overrides)
        last_request_id = response.get("request_id")

        if response.get("error"):
            logging_hook.log_event("api_error", {"turn": turns, "error": response["error"]})
            _emit_agent_end_on_error(f"error: {response['error']}")
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
        # Enforce max_batch_tool_calls — truncate excess tool calls
        max_batch = int(agent_settings.get("request_overrides", {}).get("max_batch_tool_calls", 0) or 0)
        if max_batch > 0 and len(tool_calls) > max_batch:
            logging_hook.log_event("batch_truncated", {
                "turn": turns,
                "original": len(tool_calls),
                "kept": max_batch,
            })
            tool_calls = tool_calls[:max_batch]
        thinking = extract_assistant_thinking(message, raw_content) if native_thinking else None

        tool_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
        resolved_tool_names = [resolve_tool_name(n) for n in tool_names]
        tool_call_ids = [tc.get("id", "") for tc in tool_calls]
        logging_hook.log_event("response", {
            "turn": turns,
            "tool_calls": tool_names,
            "tool_calls_resolved": resolved_tool_names,
            "tool_call_count": len(tool_calls),
            "content_len": len(content),
            "thinking_len": len(thinking) if thinking else 0,
            "content_preview": content[:200],
            "request_id": response.get("request_id"),
            "tool_call_ids": tool_call_ids,
        })
        if _turn_summary_enabled():
            _print_turn_summary(
                turns,
                tool_calls,
                content,
                thinking if thinking and thinking_visibility == "show" else None,
            )

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
                _emit_agent_end_on_error("error: forced tool choice mismatch")
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
                _emit_agent_end_on_error("error: analysis-only output after retries exhausted")
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

                _emit_agent_end_on_error("error: minimum tool calls not met")
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
                _emit_agent_end_on_error("error: code change required")
                return "error: code change required", messages

            # Final content
            if content:
                if _is_benchmark_mode() and benchmark_output_mode == "runtime" and task_depth == 0:
                    content = _benchmark_final_output(CONTINUE_SESSION)
                print(f"\n{CYAN}⏺{RESET} {content}")
                hooks.emit("response_content", {"content": content, "turn": turns})

            assistant_final: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if native_thinking and thinking:
                assistant_final["reasoning_content"] = thinking
                raw_thinking = message.get("thinking")
                if raw_thinking is not None:
                    assistant_final["thinking"] = raw_thinking
            messages.append(assistant_final)
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
                "phase_log_mode": phase_log_mode,
                "last_request_snapshot": LAST_REQUEST_SNAPSHOT,
            })
            # Log conversation dump result if the hook produced one
            dump_info = end_data.get("conversation_dump")
            if dump_info:
                logging_hook.log_event("conversation_saved", dump_info)

            return content, messages

        # Tool calls path
        assistant_message: Dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        if native_thinking and thinking:
            assistant_message["reasoning_content"] = thinking
            raw_thinking = message.get("thinking")
            if raw_thinking is not None:
                assistant_message["thinking"] = raw_thinking
        tool_results: List[Dict[str, Any]] = []

        feedback_text = None
        feedback_reason = None
        finish_requested = False
        finish_payload: Dict[str, Any] = {}
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

            if resolved_name == "finish":
                finish_requested = True
                if isinstance(FINISH_SIGNAL, dict):
                    finish_payload = dict(FINISH_SIGNAL)
                break

            if feedback_text:
                break

        messages.append(assistant_message)
        messages.extend(tool_results)

        if finish_requested and not feedback_text:
            if require_code_change and not code_change_made:
                feedback_text = (
                    "FORMAT ERROR: finish called before any confirmed code change. "
                    "Use write/edit/apply_patch to make the required change, then call finish."
                )
                feedback_reason = "code_change_required_before_finish"
            else:
                content = str(finish_payload.get("summary") or "").strip() or "done"
                if _is_benchmark_mode() and benchmark_output_mode == "runtime" and task_depth == 0:
                    content = _benchmark_final_output(CONTINUE_SESSION)
                print(f"\n{CYAN}⏺{RESET} {content}")
                hooks.emit("response_content", {"content": content, "turn": turns})

                summary = _metrics.summary()
                LAST_RUN_SUMMARY = summary
                logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})

                end_data = hooks.emit("agent_end", {
                    "summary": summary,
                    "messages": messages,
                    "content": content,
                    "turns": turns,
                    "log_path": logging_hook.get_log_path(),
                    "system_prompt": system_prompt,
                    "phase_log_mode": phase_log_mode,
                    "last_request_snapshot": LAST_REQUEST_SNAPSHOT,
                })
                dump_info = end_data.get("conversation_dump")
                if dump_info:
                    logging_hook.log_event("conversation_saved", dump_info)

                return content, messages

        # phase/task orchestration removed

        if feedback_text:
            attempt_num = None
            if feedback_reason:
                _metrics.feedback_counts[feedback_reason] += 1
                attempt_num = _metrics.feedback_counts[feedback_reason]
                lines = feedback_text.splitlines()
                if lines:
                    lines[0] = f"{lines[0]} (attempt {attempt_num})"
                    feedback_text = "\n".join(lines)

            if feedback_reason == "unknown_tool_name" and attempt_num and attempt_num >= 2:
                available_tools = ", ".join(sorted(TOOL_DISPLAY_MAP.get(name, name) for name in tools_dict.keys()))
                feedback_text = (
                    f"{feedback_text}\n"
                    "STRICT ACTION: you repeated an unknown tool name. "
                    "Next response must be exactly one valid tool call.\n"
                    f"Allowed tools: {available_tools}"
                )

            # Hard stop guard: repeated no-op writes can trap weaker models forever.
            # End the run after several repeated-noop feedback cycles.
            if feedback_reason == "write_repeated_noop" and attempt_num and attempt_num >= 3:
                summary = _metrics.summary()
                LAST_RUN_SUMMARY = summary
                logging_hook.log_event("write_noop_guard_stop", {
                    "turn": turns,
                    "attempt": attempt_num,
                    **summary,
                })
                final_content = ""
                if _is_benchmark_mode() and benchmark_output_mode == "runtime" and task_depth == 0:
                    final_content = _benchmark_final_output(CONTINUE_SESSION)
                    print(f"\n{CYAN}⏺{RESET} {final_content}")
                    hooks.emit("response_content", {"content": final_content, "turn": turns})
                _emit_agent_end_on_error("ok: completed (write noop guard)")
                return final_content, messages

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

        # Noop force-stop: end early if model is stuck in noop edit loop
        if code_change_made and not feedback_text and tool_calls:
            turn_all_noop = (
                len(tool_results) > 0
                and all(
                    _is_noop_write_result(tr.get("content", ""))
                    for tr in tool_results
                    if is_write_tool(resolve_tool_name(tr.get("name", "")))
                )
                and any(
                    is_write_tool(resolve_tool_name(tr.get("name", "")))
                    for tr in tool_results
                )
            )
            if turn_all_noop:
                consecutive_noop_turns += 1
            else:
                consecutive_noop_turns = 0
            if consecutive_noop_turns >= 3:
                summary = _metrics.summary()
                LAST_RUN_SUMMARY = summary
                logging_hook.log_event("noop_force_stop", {
                    "turn": turns,
                    "consecutive": consecutive_noop_turns,
                    **summary,
                })
                final_content = ""
                if _is_benchmark_mode() and benchmark_output_mode == "runtime" and task_depth == 0:
                    final_content = _benchmark_final_output(CONTINUE_SESSION)
                    print(f"\n{CYAN}⏺{RESET} {final_content}")
                    hooks.emit("response_content", {"content": final_content, "turn": turns})
                _emit_agent_end_on_error("ok: completed (noop force stop)")
                return final_content, messages


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
    ignored_keys = AGENT_SETTINGS.get("deprecated_ignored_keys") or []
    if ignored_keys:
        joined = ", ".join(str(k) for k in ignored_keys)
        print(f"{YELLOW}warning:{RESET} ignoring deprecated agent settings: {joined}")
        logging_hook.log_event("config_warning", {
            "kind": "deprecated_agent_settings_ignored",
            "keys": list(ignored_keys),
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
    TOOL_CATEGORIES.update(build_tool_category_map(tool_defs, tool_order, display_map))
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
        "finish": finish_run,
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
