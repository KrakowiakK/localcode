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
from localcode.task_manager import TaskManager

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
TASK_MANAGER: Optional[TaskManager] = None
TASKS_CAPTURE_MODE: bool = False
TASKS_CAPTURED: List[Dict[str, Any]] = []
FLOW_STAGE_SIGNAL: Optional[Dict[str, Any]] = None
FLOW_STAGE_EXPECTED: Optional[str] = None
FINISH_SIGNAL: Optional[Dict[str, Any]] = None

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


def _thinking_visible() -> bool:
    value = os.environ.get("LOCALCODE_THINKING_VISIBILITY", "")
    if value:
        return str(value).strip().lower() not in {"0", "false", "no", "off", "hidden"}
    mode = str(AGENT_SETTINGS.get("thinking_visibility", "show")).strip().lower()
    return mode in {"show", "visible", "on", "true"}


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


def _format_phase_signals(signals: Optional[Dict[str, Any]]) -> str:
    if not signals:
        return ""
    parts: List[str] = []
    for key in ("files_read", "files_changed", "read_tools", "write_tools", "plan_tools", "code_change", "plan_detected"):
        if key not in signals:
            continue
        val = signals.get(key)
        if isinstance(val, (set, list, tuple)):
            val = len(val)
        parts.append(f"{key}={val}")
    return " ".join(parts)


def _print_phase_event(kind: str, payload: Dict[str, Any]) -> None:
    if not payload:
        return
    turn = payload.get("turn")
    if kind == "transition":
        label = f"{payload.get('from', '?')} -> {payload.get('to', '?')}"
    elif kind == "state":
        label = str(payload.get("phase", "?"))
    else:
        label = str(payload.get("phase") or payload.get("event") or kind)
    sig_str = _format_phase_signals(payload.get("signals") if isinstance(payload.get("signals"), dict) else None)
    parts = [f"{DIM}[phase]{RESET}", label]
    if turn:
        parts.append(f"(turn {turn})")
    line = " ".join(parts)
    if sig_str:
        line = f"{line} {DIM}{sig_str}{RESET}"
    err = payload.get("error")
    if err and kind not in {"state", "transition"}:
        line = f"{line} {DIM}error={err}{RESET}"
    print(line)
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
        "task_branching": False,
        "task_flow_mode": "branched",
        "task_replan_max": 0,
        "task_plan_mode": "explore",
        "task_skip_readonly": False,
        "task_output_mode": "human",
        "benchmark_output_mode": "model",
        "flow": None,
        "flow_stage_retries": 0,
        "flow_stage_required": True,
        "history_mode": "full",
        "history_max_messages": 0,
        "history_keep_first": False,
        "flow_history_mode": None,
        "flow_history_max_messages": None,
        "flow_history_keep_first": True,
        "history_strip_thinking": False,
        "flow_history_strip_thinking": None,
        "history_tool_truncate_chars": 0,
        "history_tool_truncate_keep_last": 0,
        "history_tool_call_args_truncate_chars": 0,
        "history_tool_call_args_keep_last": 0,
        "flow_history_tool_truncate_chars": None,
        "flow_history_tool_truncate_keep_last": None,
        "flow_context_window": 6,
        "flow_retry_hints": True,
        "phase_control": None,
        "phase_log": "off",
    }

    for k in ("min_tool_calls", "max_format_retries", "max_turns"):
        if k in agent_config:
            settings[k] = agent_config[k]

    for k in ("auto_tool_call_on_failure", "require_code_change"):
        if k in agent_config:
            settings[k] = agent_config[k]

    if "task_branching" in agent_config:
        settings["task_branching"] = bool(agent_config["task_branching"])
    if "task_flow_mode" in agent_config:
        settings["task_flow_mode"] = str(agent_config["task_flow_mode"] or "branched").strip().lower()
    env_flow_mode = os.environ.get("LOCALCODE_TASK_FLOW_MODE")
    if env_flow_mode:
        settings["task_flow_mode"] = str(env_flow_mode).strip().lower()
    if settings["task_flow_mode"] not in {"branched", "staged3"}:
        raise ValueError("Agent config 'task_flow_mode' must be 'branched' or 'staged3'")
    if "flow" in agent_config:
        flow = agent_config.get("flow")
        if flow is not None and not isinstance(flow, list):
            raise ValueError("Agent config 'flow' must be a list of stages or null")
        settings["flow"] = flow
    if "flow_stage_retries" in agent_config:
        settings["flow_stage_retries"] = int(agent_config["flow_stage_retries"] or 0)
    if "flow_stage_required" in agent_config:
        settings["flow_stage_required"] = bool(agent_config["flow_stage_required"])
    if "history_mode" in agent_config:
        settings["history_mode"] = str(agent_config["history_mode"] or "full").strip().lower()
    if "history_max_messages" in agent_config:
        settings["history_max_messages"] = int(agent_config["history_max_messages"] or 0)
    if "history_keep_first" in agent_config:
        settings["history_keep_first"] = bool(agent_config["history_keep_first"])
    if "flow_history_mode" in agent_config:
        raw = agent_config.get("flow_history_mode")
        settings["flow_history_mode"] = str(raw).strip().lower() if raw is not None else None
    if "flow_history_max_messages" in agent_config:
        raw = agent_config.get("flow_history_max_messages")
        settings["flow_history_max_messages"] = int(raw) if raw is not None else None
    if "flow_history_keep_first" in agent_config:
        settings["flow_history_keep_first"] = bool(agent_config["flow_history_keep_first"])
    if "history_strip_thinking" in agent_config:
        settings["history_strip_thinking"] = bool(agent_config["history_strip_thinking"])
    if "flow_history_strip_thinking" in agent_config:
        raw = agent_config.get("flow_history_strip_thinking")
        settings["flow_history_strip_thinking"] = bool(raw) if raw is not None else None
    if "history_tool_truncate_chars" in agent_config:
        settings["history_tool_truncate_chars"] = int(agent_config["history_tool_truncate_chars"] or 0)
    if "history_tool_truncate_keep_last" in agent_config:
        settings["history_tool_truncate_keep_last"] = int(agent_config["history_tool_truncate_keep_last"] or 0)
    if "history_tool_call_args_truncate_chars" in agent_config:
        settings["history_tool_call_args_truncate_chars"] = int(agent_config["history_tool_call_args_truncate_chars"] or 0)
    if "history_tool_call_args_keep_last" in agent_config:
        settings["history_tool_call_args_keep_last"] = int(agent_config["history_tool_call_args_keep_last"] or 0)
    if "flow_history_tool_truncate_chars" in agent_config:
        raw = agent_config.get("flow_history_tool_truncate_chars")
        settings["flow_history_tool_truncate_chars"] = int(raw) if raw is not None else None
    if "flow_history_tool_truncate_keep_last" in agent_config:
        raw = agent_config.get("flow_history_tool_truncate_keep_last")
        settings["flow_history_tool_truncate_keep_last"] = int(raw) if raw is not None else None
    if "flow_context_window" in agent_config:
        settings["flow_context_window"] = int(agent_config["flow_context_window"] or 0)
    if "flow_retry_hints" in agent_config:
        settings["flow_retry_hints"] = bool(agent_config["flow_retry_hints"])
    if "phase_control" in agent_config:
        settings["phase_control"] = agent_config.get("phase_control")
    if "task_replan_max" in agent_config:
        settings["task_replan_max"] = int(agent_config["task_replan_max"] or 0)
    if "task_plan_mode" in agent_config:
        settings["task_plan_mode"] = str(agent_config["task_plan_mode"] or "explore").strip().lower()
        if settings["task_plan_mode"] not in {"explore", "first", "none"}:
            raise ValueError("Agent config 'task_plan_mode' must be one of: explore, first, none")
    if "task_skip_readonly" in agent_config:
        settings["task_skip_readonly"] = bool(agent_config["task_skip_readonly"])
    env_skip_readonly = os.environ.get("LOCALCODE_TASK_SKIP_READONLY", "")
    if env_skip_readonly:
        settings["task_skip_readonly"] = str(env_skip_readonly).strip().lower() in {"1", "true", "yes", "on"}

    if "task_output_mode" in agent_config:
        settings["task_output_mode"] = str(agent_config["task_output_mode"] or "human").strip().lower()
    env_output_mode = os.environ.get("LOCALCODE_TASK_OUTPUT_MODE")
    if env_output_mode:
        settings["task_output_mode"] = str(env_output_mode).strip().lower()
    if settings["task_output_mode"] not in {"human", "runtime"}:
        raise ValueError("Agent config 'task_output_mode' must be 'human' or 'runtime'")

    if "benchmark_output_mode" in agent_config:
        settings["benchmark_output_mode"] = str(agent_config["benchmark_output_mode"] or "model").strip().lower()
    env_benchmark_mode = os.environ.get("LOCALCODE_BENCHMARK_OUTPUT_MODE")
    if env_benchmark_mode:
        settings["benchmark_output_mode"] = str(env_benchmark_mode).strip().lower()
    if settings["benchmark_output_mode"] not in {"model", "runtime"}:
        raise ValueError("Agent config 'benchmark_output_mode' must be 'model' or 'runtime'")

    if "phase_log" in agent_config:
        settings["phase_log"] = agent_config.get("phase_log")
    env_phase_log = os.environ.get("LOCALCODE_PHASE_LOG")
    if env_phase_log:
        settings["phase_log"] = env_phase_log
    raw_phase_log = settings.get("phase_log")
    if isinstance(raw_phase_log, bool):
        phase_log_mode = "both" if raw_phase_log else "off"
    elif raw_phase_log is None:
        phase_log_mode = "off"
    else:
        phase_log_mode = str(raw_phase_log).strip().lower()
        if phase_log_mode in {"1", "true", "yes", "on"}:
            phase_log_mode = "both"
        elif phase_log_mode in {"0", "false", "no", "off", "none"}:
            phase_log_mode = "off"
    if phase_log_mode not in {"off", "stdout", "log", "both"}:
        phase_log_mode = "off"
    settings["phase_log"] = phase_log_mode

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
    request_attempts = 2
    for attempt in range(1, request_attempts + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            raw = resp.read()
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


def plan_tasks(args: Dict[str, Any]) -> str:
    """Create/update/list tasks for task-branching mode."""
    global TASK_MANAGER, TASKS_CAPTURE_MODE, TASKS_CAPTURED
    data, err = _require_args_dict(args, "plan_tasks")
    if err:
        return err
    if TASK_MANAGER is None:
        TASK_MANAGER = TaskManager()

    action = str(data.get("action") or "").strip().lower()
    if not action:
        return "error: missing required parameter 'action'"

    if action == "create":
        tasks = data.get("tasks") or []
        if not isinstance(tasks, list):
            return "error: 'tasks' must be an array"
        if TASKS_CAPTURE_MODE:
            temp_manager = TaskManager()
            created = temp_manager.create_tasks(tasks)
            TASKS_CAPTURED = [
                {"id": t.task_id, "description": t.description}
                for t in created
            ]
            if not created:
                return "error: no valid tasks created (each task needs a description)"
            return f"ok: captured {len(created)} task(s)"
        created = TASK_MANAGER.create_tasks(tasks)
        if not created:
            return "error: no valid tasks created (each task needs a description)"
        return f"ok: created {len(created)} task(s)"

    if action == "update":
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            return "error: missing required parameter 'task_id' for update"
        fields: Dict[str, Any] = {}
        if data.get("status"):
            fields["status"] = str(data.get("status"))
        if "summary" in data:
            fields["summary"] = data.get("summary")
        if "files_changed" in data and isinstance(data.get("files_changed"), list):
            fields["files_changed"] = data.get("files_changed")
        if not fields:
            return "error: no update fields provided"
        task = TASK_MANAGER.update_task(task_id, **fields)
        if not task:
            return f"error: unknown task_id '{task_id}'"
        return f"ok: updated {task_id}"

    if action == "list":
        tasks = TASK_MANAGER.list_tasks()
        payload = [
            {
                "id": t.task_id,
                "description": t.description,
                "status": t.status,
                "priority": t.priority,
                "summary": t.summary,
                "files_changed": list(t.files_changed),
            }
            for t in tasks
        ]
        return json.dumps(payload)

    return f"error: invalid action '{action}' (expected create|update|list)"


def flow_stage_done(args: Dict[str, Any]) -> str:
    """Record completion of a flow stage and capture structured metadata."""
    global FLOW_STAGE_SIGNAL, FLOW_STAGE_EXPECTED
    data, err = _require_args_dict(args, "flow_stage_done")
    if err:
        return err
    stage_raw = data.get("stage")
    stage = str(stage_raw or "").strip().lower()
    if not stage:
        return "error: missing required parameter 'stage'"
    payload = dict(data)
    payload["stage"] = stage
    FLOW_STAGE_SIGNAL = {
        "stage": stage,
        "payload": payload,
    }
    expected = str(FLOW_STAGE_EXPECTED or "").strip().lower()
    if expected and stage != expected:
        logging_hook.log_event("flow_stage_mismatch", {
            "expected": expected,
            "actual": stage,
            "payload_preview": str(payload)[:200],
        })
        return f"ok: flow_stage_done recorded (expected {expected})"
    logging_hook.log_event("flow_stage_done", {
        "stage": stage,
        "payload_preview": str(payload)[:200],
    })
    return "ok: flow_stage_done recorded"


def finish_run(args: Dict[str, Any]) -> str:
    """Record a finish signal so the runtime can terminate cleanly."""
    global FINISH_SIGNAL
    data, err = _require_args_dict(args, "finish")
    if err:
        return err

    status = str(data.get("status") or "done").strip().lower()
    if status not in {"done", "blocked", "incomplete"}:
        return "error: invalid status for finish (expected done|blocked|incomplete)"

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
        "summary_preview": summary[:200],
    })
    return f"ok: finish recorded ({status})"


def _collect_parent_summary(messages: List[Dict[str, Any]], max_messages: int = 6, max_chars: int = 1200) -> str:
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content:
            parts.append(f"{role}: {str(content)}")
    if max_messages and len(parts) > max_messages:
        parts = parts[-max_messages:]
    summary = "\n".join(parts).strip()
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return summary


def _extract_task_files(task_messages: List[Dict[str, Any]]) -> List[str]:
    files = set()
    for msg in task_messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {}) or {}
            name = resolve_tool_name(func.get("name", ""))
            if name not in ("write", "write_file", "edit", "replace_in_file", "apply_patch", "patch_files"):
                continue
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    args = {}
            if isinstance(args, dict):
                path = args.get("path")
                if isinstance(path, str) and path:
                    files.add(path)
                paths = args.get("paths")
                if isinstance(paths, list):
                    for p in paths:
                        if isinstance(p, str) and p:
                            files.add(p)
                if name in ("apply_patch", "patch_files"):
                    patch = args.get("patch")
                    if isinstance(patch, str):
                        patch_file = extract_patch_file(patch)
                        if patch_file:
                            files.add(patch_file)
                    patches = args.get("patches")
                    if isinstance(patches, list):
                        for p in patches:
                            if isinstance(p, str):
                                patch_file = extract_patch_file(p)
                                if patch_file:
                                    files.add(patch_file)
    return sorted(files)


def _extract_task_reads(task_messages: List[Dict[str, Any]]) -> List[str]:
    files = set()
    for msg in task_messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {}) or {}
            name = resolve_tool_name(func.get("name", ""))
            if name not in ("read", "read_file", "read_files", "batch_read"):
                continue
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    args = {}
            if isinstance(args, dict):
                path = args.get("path")
                if isinstance(path, str) and path:
                    files.add(path)
                paths = args.get("paths")
                if isinstance(paths, list):
                    for p in paths:
                        if isinstance(p, str) and p:
                            files.add(p)
    return sorted(files)


def _extract_plan_steps(description: str) -> List[str]:
    steps: List[str] = []
    if not description:
        return steps
    for line in str(description).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            steps.append(stripped[2:].strip())
            continue
        if stripped[0].isdigit() and "." in stripped[:3]:
            parts = stripped.split(".", 1)
            if len(parts) == 2:
                step = parts[1].strip()
                if step:
                    steps.append(step)
    return steps


_READONLY_TASK_RE = re.compile(
    r"\b(read|review|inspect|analy[sz]e|examine|check|find|search|list|locate|open|"
    r"summar(?:ize|ise)|document|scan|explore|understand|plan)\b"
)
_WRITE_TASK_RE = re.compile(
    r"\b(write|edit|patch|change|modify|implement|fix|update|add|remove|delete|"
    r"refactor|create|rewrite|rename|move)\b"
)


def _should_skip_task(description: str) -> bool:
    if not description:
        return False
    text = str(description).lower()
    if _WRITE_TASK_RE.search(text):
        return False
    return bool(_READONLY_TASK_RE.search(text))


def _validate_task_report(report: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = {
        "task_id": str,
        "description": str,
        "status": str,
        "attempts": int,
        "replan_max": int,
        "files_changed": list,
        "files_read": list,
        "plan_steps": list,
    }
    optional = {
        "tool_calls_total": int,
        "tool_errors_total": int,
        "tool_call_counts": dict,
        "tool_error_counts": dict,
        "analysis_retries": int,
        "feedback_counts": dict,
        "error": str,
        "skip_reason": str,
    }
    for key, expected in required.items():
        if key not in report:
            errors.append(f"missing:{key}")
            continue
        value = report.get(key)
        if not isinstance(value, expected):
            errors.append(f"type:{key}")
            continue
        if expected is list:
            if not all(isinstance(item, str) for item in value):
                errors.append(f"type:{key}_items")
    for key, expected in optional.items():
        if key not in report:
            continue
        value = report.get(key)
        if value is None:
            continue
        if not isinstance(value, expected):
            errors.append(f"type:{key}")
    return errors


def _build_task_summary(task_id: str, description: str, status: str, content: str, files_changed: List[str]) -> str:
    parts = [f"Task {task_id}: {description}", f"status={status}"]
    if files_changed:
        parts.append(f"files={', '.join(files_changed)}")
    if content:
        snippet = content.strip().replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:300].rstrip() + "..."
        parts.append(f"notes={snippet}")
    return " | ".join(parts)


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
    global LAST_RUN_SUMMARY, CURRENT_MESSAGES, FINISH_SIGNAL
    LAST_RUN_SUMMARY = None
    FINISH_SIGNAL = None

    task_branching = bool(agent_settings.get("task_branching", False)) and task_depth == 0
    task_flow_mode = str(agent_settings.get("task_flow_mode", "branched") or "branched").strip().lower()
    task_plan_mode = str(agent_settings.get("task_plan_mode", "explore") or "explore").strip().lower()
    task_skip_readonly = bool(agent_settings.get("task_skip_readonly", False))
    task_output_mode = str(agent_settings.get("task_output_mode", "human") or "human").strip().lower()
    benchmark_output_mode = str(agent_settings.get("benchmark_output_mode", "model") or "model").strip().lower()
    flow_config = agent_settings.get("flow")
    flow_enabled = bool(flow_config) and task_depth == 0
    if flow_enabled:
        task_branching = False
    flow_stage_retries = int(agent_settings.get("flow_stage_retries", 0) or 0)
    flow_stage_required = bool(agent_settings.get("flow_stage_required", True))
    history_mode = str(agent_settings.get("history_mode", "full") or "full").strip().lower()
    history_max_messages = int(agent_settings.get("history_max_messages", 0) or 0)
    history_keep_first = bool(agent_settings.get("history_keep_first", False))
    history_strip_thinking = bool(agent_settings.get("history_strip_thinking", False))
    history_tool_truncate_chars = int(agent_settings.get("history_tool_truncate_chars", 0) or 0)
    history_tool_truncate_keep_last = int(agent_settings.get("history_tool_truncate_keep_last", 0) or 0)
    history_tool_call_args_truncate_chars = int(agent_settings.get("history_tool_call_args_truncate_chars", 0) or 0)
    history_tool_call_args_keep_last = int(agent_settings.get("history_tool_call_args_keep_last", 0) or 0)
    flow_retry_hints = bool(agent_settings.get("flow_retry_hints", True))
    flow_context_window = int(agent_settings.get("flow_context_window", 6) or 0)
    phase_control = agent_settings.get("phase_control") or {}
    phase_log_mode = str(agent_settings.get("phase_log", "off") or "off").strip().lower()
    if phase_log_mode in {"1", "true", "yes", "on"}:
        phase_log_mode = "both"
    elif phase_log_mode in {"0", "false", "no", "off", "none"}:
        phase_log_mode = "off"
    if phase_log_mode not in {"off", "stdout", "log", "both"}:
        phase_log_mode = "off"
    phase_log_stdout = phase_log_mode in {"stdout", "both"}
    task_manager: Optional[TaskManager] = None
    if task_branching:
        global TASK_MANAGER
        TASK_MANAGER = TaskManager()
        task_manager = TASK_MANAGER

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
    write_nudge_sent = False
    finish_nudge_sent = False
    consecutive_noop_turns = 0

    native_thinking = bool(agent_settings.get("native_thinking", False))

    agent_max_turns = int(agent_settings.get("max_turns", MAX_TURNS) or MAX_TURNS)

    format_retry_turns = 0  # Track turns consumed by format retries (not counted toward MAX_TURNS)

    READ_ONLY_TOOLS = {"list_dir", "read_file", "read_files", "search_text", "find_files"}
    WRITE_TOOLS = {"write_file", "replace_in_file", "patch_files"}

    def _filter_tools_for_stage(stage: str) -> ToolsDict:
        stage = (stage or "").strip().lower()
        if stage == "plan":
            allowed = READ_ONLY_TOOLS | {"plan_tasks"}
        elif stage == "context":
            allowed = READ_ONLY_TOOLS
        else:
            allowed = READ_ONLY_TOOLS | WRITE_TOOLS
        return {k: v for k, v in tools_dict.items() if k in allowed}

    def _compact_stage_summary(content: str, files_read: List[str]) -> str:
        parts: List[str] = []
        if files_read:
            parts.append(f"files_read: {', '.join(files_read[:8])}")
        if content:
            snippet = content.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:240].rstrip() + "..."
            if snippet:
                parts.append(f"notes: {snippet}")
        return "\n".join(parts).strip()

    def _select_request_messages(all_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if history_mode in {"full", "all"} or history_max_messages <= 0:
            selected = all_messages
        else:
            total = len(all_messages)
            if total <= history_max_messages:
                selected = all_messages
            else:
                if history_keep_first and all_messages:
                    first = all_messages[0]
                    tail = all_messages[-history_max_messages:]
                    if first in tail:
                        selected = tail
                    else:
                        selected = [first] + tail
                else:
                    selected = all_messages[-history_max_messages:]
                if len(selected) < total:
                    logging_hook.log_event("history_trim", {
                        "history_mode": history_mode,
                        "history_max_messages": history_max_messages,
                        "history_keep_first": history_keep_first,
                        "before": total,
                        "after": len(selected),
                    })
        if not history_strip_thinking:
            sanitized = list(selected)
        else:
            sanitized = []
            for msg in selected:
                if not isinstance(msg, dict):
                    sanitized.append(msg)
                    continue
                if "thinking" not in msg and "reasoning_content" not in msg:
                    sanitized.append(msg)
                    continue
                cleaned = dict(msg)
                cleaned.pop("thinking", None)
                cleaned.pop("reasoning_content", None)
                sanitized.append(cleaned)
            if len(sanitized) != len(selected):
                logging_hook.log_event("history_strip_thinking", {
                    "removed": len(selected) - len(sanitized),
                })
        if history_tool_call_args_truncate_chars > 0:
            assistant_indices = [
                i for i, msg in enumerate(sanitized)
                if isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and isinstance(msg.get("tool_calls"), list)
                and msg.get("tool_calls")
            ]
            protected_assistant = set()
            if history_tool_call_args_keep_last > 0 and assistant_indices:
                protected_assistant = set(assistant_indices[-history_tool_call_args_keep_last:])

            def _truncate_arg_value(value: Any) -> Any:
                if isinstance(value, str):
                    if len(value) <= history_tool_call_args_truncate_chars:
                        return value
                    snippet = value[:history_tool_call_args_truncate_chars].rstrip()
                    return f"{snippet}\n...[truncated {len(value) - len(snippet)} chars]"
                if isinstance(value, list):
                    return [_truncate_arg_value(v) for v in value]
                if isinstance(value, dict):
                    return {k: _truncate_arg_value(v) for k, v in value.items()}
                return value

            compacted_messages: List[Dict[str, Any]] = []
            for idx, msg in enumerate(sanitized):
                if idx not in assistant_indices or idx in protected_assistant:
                    compacted_messages.append(msg)
                    continue

                tool_calls = msg.get("tool_calls")
                if not isinstance(tool_calls, list):
                    compacted_messages.append(msg)
                    continue

                changed = False
                new_tool_calls: List[Dict[str, Any]] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        new_tool_calls.append(tc)
                        continue
                    tc_new = dict(tc)
                    fn = tc_new.get("function")
                    if not isinstance(fn, dict):
                        new_tool_calls.append(tc_new)
                        continue
                    fn_new = dict(fn)
                    arg_raw = fn_new.get("arguments")
                    if isinstance(arg_raw, str) and len(arg_raw) > history_tool_call_args_truncate_chars:
                        before_len = len(arg_raw)
                        try:
                            parsed = json.loads(arg_raw)
                        except Exception:
                            snippet = arg_raw[:history_tool_call_args_truncate_chars].rstrip()
                            fn_new["arguments"] = json.dumps({
                                "_truncated": True,
                                "_chars": before_len,
                                "_preview": snippet,
                            }, ensure_ascii=False)
                        else:
                            compact = _truncate_arg_value(parsed)
                            fn_new["arguments"] = json.dumps(compact, ensure_ascii=False)
                        after_len = len(str(fn_new.get("arguments") or ""))
                        changed = True
                        logging_hook.log_event("history_tool_call_truncate", {
                            "chars_before": before_len,
                            "chars_after": after_len,
                        })
                    tc_new["function"] = fn_new
                    new_tool_calls.append(tc_new)

                if changed:
                    new_msg = dict(msg)
                    new_msg["tool_calls"] = new_tool_calls
                    compacted_messages.append(new_msg)
                else:
                    compacted_messages.append(msg)
            sanitized = compacted_messages
        if history_tool_truncate_chars <= 0:
            return sanitized
        tool_indices = [i for i, msg in enumerate(sanitized) if isinstance(msg, dict) and msg.get("role") == "tool" and isinstance(msg.get("content"), str)]
        protected = set()
        if history_tool_truncate_keep_last > 0 and tool_indices:
            protected = set(tool_indices[-history_tool_truncate_keep_last:])
        truncated = []
        for idx, msg in enumerate(sanitized):
            if idx not in tool_indices or idx in protected:
                truncated.append(msg)
                continue
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, str) or len(content) <= history_tool_truncate_chars:
                truncated.append(msg)
                continue
            new_msg = dict(msg)
            snippet = content[:history_tool_truncate_chars].rstrip()
            new_msg["content"] = f"{snippet}\n...[truncated {len(content) - len(snippet)} chars]"
            truncated.append(new_msg)
            logging_hook.log_event("history_tool_truncate", {
                "chars_before": len(content),
                "chars_after": len(new_msg.get("content") or ""),
            })
        return truncated

    phase_enabled = False
    phase_state = ""
    phase_signals = {
        "files_read": set(),
        "files_changed": set(),
        "read_tools": 0,
        "write_tools": 0,
        "plan_tools": 0,
        "code_change": False,
        "plan_detected": False,
    }
    phase_cfg = {}
    phase_rules = {}
    phase_prompts = {}
    phase_states: List[str] = []
    phase_mode = "off"
    phase_last_probe: Optional[Dict[str, Any]] = None

    if phase_control and task_depth == 0 and not flow_enabled and not task_branching and task_flow_mode != "staged3":
        phase_cfg = phase_control if isinstance(phase_control, dict) else {}
        phase_mode = str(phase_cfg.get("mode") or "off").strip().lower()
        phase_states = [str(s).strip().lower() for s in (phase_cfg.get("states") or ["context", "plan", "implement"]) if str(s).strip()]
        if not phase_states:
            phase_states = ["context", "plan", "implement"]
        phase_state = str(phase_cfg.get("default") or phase_states[0]).strip().lower()
        if phase_state not in phase_states:
            phase_state = phase_states[0]
        phase_rules = phase_cfg.get("rules") if isinstance(phase_cfg.get("rules"), dict) else {}
        phase_prompts = phase_cfg.get("prompts") if isinstance(phase_cfg.get("prompts"), dict) else {}
        phase_enabled = phase_mode in {"rules", "llm", "hybrid"}

    def _phase_from_rules(current: str) -> str:
        if not phase_enabled or phase_mode not in {"rules", "hybrid"}:
            return current
        min_files = int(phase_rules.get("min_files_read_for_plan", 1) or 0)
        auto_plan = bool(phase_rules.get("auto_plan_after_read", True))
        write_to_implement = bool(phase_rules.get("write_to_implement", True))
        if write_to_implement and (phase_signals["write_tools"] > 0 or phase_signals["code_change"]):
            return "implement"
        if phase_signals["plan_tools"] > 0 or phase_signals["plan_detected"]:
            return "plan"
        if auto_plan and current == "context" and len(phase_signals["files_read"]) >= min_files:
            return "plan"
        return current

    def _parse_phase_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        raw = text.strip()
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = raw[start:end + 1]
            try:
                data = json.loads(snippet)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _phase_probe_llm(current: str, turn: int, tool_names: List[str]) -> Optional[Dict[str, Any]]:
        if not phase_enabled or phase_mode not in {"llm", "hybrid"}:
            return None
        probe_cfg = phase_cfg.get("probe") if isinstance(phase_cfg.get("probe"), dict) else {}
        if probe_cfg is False:
            return None
        temp = probe_cfg.get("temperature", 0)
        max_tokens = int(probe_cfg.get("max_tokens", 128) or 128)
        system_text = probe_cfg.get("system_prompt") or (
            "You are a classifier. Return JSON only with keys: phase, confidence. "
            f"Valid phases: {', '.join(phase_states)}."
        )
        user_text = probe_cfg.get("user_prompt")
        if not user_text:
            user_text = (
                "Classify the current phase.\n"
                f"prev_phase: {current}\n"
                f"read_tools: {phase_signals['read_tools']}\n"
                f"write_tools: {phase_signals['write_tools']}\n"
                f"plan_tools: {phase_signals['plan_tools']}\n"
                f"files_read: {len(phase_signals['files_read'])}\n"
                f"files_changed: {len(phase_signals['files_changed'])}\n"
                f"code_change: {phase_signals['code_change']}\n"
                f"plan_detected: {phase_signals['plan_detected']}\n"
                f"turn: {turn}\n"
                f"tools_used: {', '.join([t for t in tool_names if t])}\n"
            )
        probe_overrides = {
            "temperature": temp,
            "max_tokens": max_tokens,
            "think": False,
        }
        response = call_api(
            [{"role": "user", "content": user_text}],
            system_text,
            {},
            probe_overrides,
        )
        if response.get("error"):
            logging_hook.log_event("phase_probe_error", {"turn": turn, "error": response["error"]})
            if phase_log_stdout:
                _print_phase_event("probe_error", {"turn": turn, "error": response["error"]})
            return None
        choice = (response.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content", "") or ""
        parsed = _parse_phase_json(content)
        if not parsed:
            logging_hook.log_event("phase_probe_parse_error", {"turn": turn, "raw": content[:200]})
            if phase_log_stdout:
                _print_phase_event("probe_parse_error", {"turn": turn, "error": "parse_error"})
            return None
        return parsed

    def _phase_update(current: str, turn: int, tool_names: List[str]) -> str:
        new_phase = _phase_from_rules(current)
        if phase_mode == "hybrid" and new_phase == current:
            probe = _phase_probe_llm(current, turn, tool_names)
            if isinstance(probe, dict):
                phase = str(probe.get("phase") or "").strip().lower()
                if phase in phase_states:
                    return phase
        if phase_mode == "llm":
            probe = _phase_probe_llm(current, turn, tool_names)
            if isinstance(probe, dict):
                phase = str(probe.get("phase") or "").strip().lower()
                if phase in phase_states:
                    return phase
        return new_phase

    def _normalize_flow_stages(raw_flow: Any) -> List[Dict[str, Any]]:
        stages: List[Dict[str, Any]] = []
        if not isinstance(raw_flow, list):
            return stages
        for idx, item in enumerate(raw_flow, start=1):
            if isinstance(item, str):
                label = item.strip()
                stage_id = label.lower().replace(" ", "_") if label else f"stage_{idx}"
                default_require_change = stage_id in {"implement", "implementation", "execute", "edit", "build"}
                stages.append({
                    "id": stage_id,
                    "label": label or stage_id,
                    "prompt": "",
                    "done_hint": "",
                    "require_code_change": default_require_change,
                    "raw": item,
                })
                continue
            if isinstance(item, dict):
                raw_id = item.get("id") or item.get("stage") or item.get("name") or f"stage_{idx}"
                stage_id = str(raw_id).strip().lower() if raw_id else f"stage_{idx}"
                label = str(item.get("label") or item.get("name") or item.get("id") or stage_id).strip()
                prompt_text = str(item.get("prompt") or item.get("instructions") or "").strip()
                done_hint = str(item.get("done_hint") or item.get("done_prompt") or "").strip()
                require_change = item.get("require_code_change")
                if require_change is None:
                    require_change = stage_id in {"implement", "implementation", "execute", "edit", "build"}
                stages.append({
                    "id": stage_id or f"stage_{idx}",
                    "label": label or stage_id,
                    "prompt": prompt_text,
                    "done_hint": done_hint,
                    "require_code_change": bool(require_change),
                    "history_mode": item.get("history_mode"),
                    "history_max_messages": item.get("history_max_messages"),
                    "history_keep_first": item.get("history_keep_first"),
                    "history_strip_thinking": item.get("history_strip_thinking"),
                    "history_tool_truncate_chars": item.get("history_tool_truncate_chars"),
                    "history_tool_truncate_keep_last": item.get("history_tool_truncate_keep_last"),
                    "context_window": item.get("context_window"),
                    "tools_allow": item.get("tools_allow") or item.get("tool_allow"),
                    "tools_block": item.get("tools_block") or item.get("tool_block"),
                    "require_files_read": item.get("require_files_read"),
                    "require_non_flow_tool": item.get("require_non_flow_tool"),
                    "allow_missing_done": item.get("allow_missing_done"),
                    "raw": item,
                })
        return stages

    def _format_flow_context(notes: List[str], window: int) -> str:
        if not notes or window == 0:
            return ""
        if window < 0:
            window = len(notes)
        tail = notes[-window:] if len(notes) > window else notes
        return "\n".join(tail).strip()

    def _build_flow_stage_note(
        stage_label: str,
        content: str,
        files_read: List[str],
        files_changed: List[str],
        signal: Optional[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, Any]]:
        payload = signal.get("payload") if isinstance(signal, dict) else {}
        summary = ""
        if isinstance(payload, dict):
            raw_summary = payload.get("summary")
            summary = str(raw_summary).strip() if raw_summary else ""
        decisions = payload.get("decisions") if isinstance(payload, dict) else None
        next_actions = payload.get("next_actions") if isinstance(payload, dict) else None
        risks = payload.get("risks") if isinstance(payload, dict) else None

        parts: List[str] = []
        if summary:
            parts.append(summary)
        if isinstance(decisions, list) and decisions:
            parts.append("decisions: " + "; ".join(str(d).strip() for d in decisions[:3] if str(d).strip()))
        if isinstance(next_actions, list) and next_actions:
            parts.append("next: " + "; ".join(str(a).strip() for a in next_actions[:3] if str(a).strip()))
        if isinstance(risks, list) and risks:
            parts.append("risks: " + "; ".join(str(r).strip() for r in risks[:2] if str(r).strip()))
        if not parts:
            compact = _compact_stage_summary(content, files_read)
            if compact:
                parts.append(compact)
        if files_changed:
            parts.append("changed: " + ", ".join(files_changed[:6]))

        note = f"{stage_label}: " + " | ".join(parts) if parts else f"{stage_label}: done"
        meta = payload if isinstance(payload, dict) else {}
        return note.strip(), meta

    def _stage_made_change(messages_in: List[Dict[str, Any]]) -> bool:
        for msg in messages_in:
            if msg.get("role") != "tool":
                continue
            tool_name = resolve_tool_name(msg.get("name", ""))
            if not is_write_tool(tool_name):
                continue
            content = msg.get("content") or ""
            if _did_tool_make_change(tool_name, content):
                return True
        return False

    def _build_flow_retry_hint(
        stage_id: str,
        used_write: bool,
        made_change: bool,
        sub_summary: Optional[Dict[str, Any]],
    ) -> str:
        hints: List[str] = []
        tool_errors = (sub_summary or {}).get("tool_error_counts") or {}
        tool_errors_total = (sub_summary or {}).get("tool_errors_total") or 0
        if stage_id == "implement":
            if not used_write:
                hints.append("Stop exploring. You must modify files now (patch_files/replace_in_file/write_file).")
            elif used_write and not made_change:
                hints.append("Your edits made no changes. Re-read the target file and apply a concrete change.")
            if tool_errors.get("apply_patch") or tool_errors.get("patch_files"):
                hints.append("Patch failed. Re-read the file and use replace_in_file or write_file as fallback.")
        else:
            if used_write:
                hints.append("Do not modify files in this stage. Only read/analyze.")
        if tool_errors_total:
            hints.append("Fix tool errors before continuing.")
        return " ".join(hints).strip()

    def _execute_task_branches() -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        global TASK_MANAGER, LAST_RUN_SUMMARY, CURRENT_MESSAGES, TASK_ID, TASK_INDEX, TASK_TOTAL
        if not task_branching or not task_manager:
            return None
        parent_summary = _collect_parent_summary(messages)
        task_context_notes: List[str] = []
        replan_max = int(agent_settings.get("task_replan_max", 0) or 0)
        summaries: List[str] = []
        tasks_list = task_manager.list_tasks()
        for idx, task in enumerate(tasks_list, start=1):
            task_context = parent_summary
            if task_context_notes:
                task_context = (
                    f"{task_context}\n\nTASK CONTEXT:\n"
                    + "\n".join(task_context_notes[-6:])
                ).strip()
            saved_task_id = TASK_ID
            saved_task_index = TASK_INDEX
            saved_task_total = TASK_TOTAL
            TASK_ID = task.task_id
            TASK_INDEX = idx
            TASK_TOTAL = len(tasks_list)
            attempt = 0
            content = ""
            task_messages: List[Dict[str, Any]] = []
            status = "failed"
            sub_summary: Dict[str, Any] = {}
            try:
                if task_skip_readonly and _should_skip_task(task.description):
                    task_manager.start_task(task.task_id)
                    status = "skipped"
                    files_changed: List[str] = []
                    files_read: List[str] = []
                    plan_steps = _extract_plan_steps(task.description)
                    report: Dict[str, Any] = {
                        "task_id": task.task_id,
                        "status": status,
                        "description": task.description,
                        "attempts": 0,
                        "replan_max": replan_max,
                        "files_changed": files_changed,
                        "files_read": files_read,
                        "plan_steps": plan_steps,
                        "skip_reason": "read_only",
                    }
                    report_errors = _validate_task_report(report)
                    if report_errors:
                        logging_hook.log_event("task_report_invalid", {
                            "task_id": task.task_id,
                            "errors": report_errors,
                            "status": status,
                        })
                    logging_hook.log_event("task_report", report)
                    context_line = f"{task.task_id}: status={status}; skip_reason=read_only"
                    if plan_steps:
                        context_line += f"; plan_steps={len(plan_steps)}"
                    task_context_notes.append(context_line)
                    summary = _build_task_summary(
                        task.task_id,
                        task.description,
                        status,
                        "skipped read-only task",
                        files_changed,
                    )
                    task_manager.end_task(
                        task.task_id,
                        status=status,
                        summary=summary,
                        files_changed=files_changed,
                        files_read=files_read,
                    )
                    summaries.append(summary)
                    if task_output_mode == "human":
                        messages.append({"role": "system", "content": f"[TASK SUMMARY] {summary}"})
                    continue
                task_manager.start_task(task.task_id)
                while True:
                    task_prompt = f"[TASK {task.task_id}] {task.description}"
                    if attempt > 0:
                        task_prompt = (
                            f"{task_prompt}\n\nREPLAN attempt {attempt}/{replan_max}: "
                            f"previous attempt failed with: {content}"
                        )
                    if task_context:
                        task_prompt = f"{task_prompt}\n\nCONTEXT:\n{task_context}"
                    sub_settings = dict(agent_settings)
                    sub_settings["task_branching"] = False
                    sub_tools = {k: v for k, v in tools_dict.items() if k != "plan_tasks"}
                    saved_hooks = {ev: list(cbs) for ev, cbs in hooks._hooks.items()}
                    saved_current_messages = CURRENT_MESSAGES
                    content, task_messages = run_agent(
                        task_prompt,
                        system_prompt,
                        sub_tools,
                        sub_settings,
                        previous_messages=None,
                        task_depth=task_depth + 1,
                    )
                    if isinstance(LAST_RUN_SUMMARY, dict):
                        sub_summary = dict(LAST_RUN_SUMMARY)
                    hooks._hooks = saved_hooks
                    CURRENT_MESSAGES = saved_current_messages
                    status = "failed" if str(content).startswith("error:") else "completed"
                    if status == "completed" or attempt >= replan_max:
                        break
                    attempt += 1
            finally:
                TASK_ID = saved_task_id
                TASK_INDEX = saved_task_index
                TASK_TOTAL = saved_task_total
            files_changed = _extract_task_files(task_messages)
            files_read = _extract_task_reads(task_messages)
            plan_steps = _extract_plan_steps(task.description)
            attempts_used = attempt + 1
            report: Dict[str, Any] = {
                "task_id": task.task_id,
                "status": status,
                "description": task.description,
                "attempts": attempts_used,
                "replan_max": replan_max,
                "files_changed": files_changed,
                "files_read": files_read,
                "plan_steps": plan_steps,
            }
            if sub_summary:
                report.update({
                    "tool_calls_total": sub_summary.get("tool_calls_total"),
                    "tool_errors_total": sub_summary.get("tool_errors_total"),
                    "tool_call_counts": sub_summary.get("tool_call_counts"),
                    "tool_error_counts": sub_summary.get("tool_error_counts"),
                    "analysis_retries": sub_summary.get("analysis_retries"),
                    "feedback_counts": sub_summary.get("feedback_counts"),
                })
            if status != "completed":
                report["error"] = (content or "")[:200]
            report_errors = _validate_task_report(report)
            if report_errors:
                logging_hook.log_event("task_report_invalid", {
                    "task_id": task.task_id,
                    "errors": report_errors,
                    "status": status,
                })
            logging_hook.log_event("task_report", report)
            context_line = f"{task.task_id}: status={status}"
            if files_read:
                context_line += f"; read={', '.join(files_read)}"
            if files_changed:
                context_line += f"; changed={', '.join(files_changed)}"
            if plan_steps:
                context_line += f"; plan_steps={len(plan_steps)}"
            task_context_notes.append(context_line)
            summary = _build_task_summary(
                task.task_id,
                task.description,
                status,
                content or "",
                files_changed,
            )
            task_manager.end_task(
                task.task_id,
                status=status,
                summary=summary,
                files_changed=files_changed,
                files_read=files_read,
                error=(content if status == "failed" else None),
            )
            summaries.append(summary)
            if task_output_mode == "human":
                messages.append({"role": "system", "content": f"[TASK SUMMARY] {summary}"})
        TASK_MANAGER = None
        if task_output_mode == "runtime":
            if _is_benchmark_mode():
                final_content = _benchmark_final_output(CONTINUE_SESSION)
            else:
                final_content = "ok: completed"
        else:
            final_content = "\n".join(summaries) if summaries else "ok: no tasks executed"
        if _is_benchmark_mode() and benchmark_output_mode == "runtime":
            final_content = _benchmark_final_output(CONTINUE_SESSION)
        if final_content:
            hooks.emit("response_content", {"content": final_content, "turn": turns})
        messages.append({"role": "assistant", "content": final_content})
        summary = _metrics.summary()
        LAST_RUN_SUMMARY = summary
        logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})
        end_data = hooks.emit("agent_end", {
            "summary": summary,
            "messages": messages,
            "content": final_content,
            "turns": turns,
            "log_path": logging_hook.get_log_path(),
            "system_prompt": system_prompt,
            "phase_log_mode": phase_log_mode,
        })
        dump_info = end_data.get("conversation_dump")
        if dump_info:
            logging_hook.log_event("conversation_saved", dump_info)
        return final_content, messages

    def _execute_flow() -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        global LAST_RUN_SUMMARY, CURRENT_MESSAGES, FLOW_STAGE_SIGNAL, FLOW_STAGE_EXPECTED, TASK_ID, TASK_INDEX, TASK_TOTAL
        if not flow_enabled:
            return None

        stages = _normalize_flow_stages(flow_config)
        if not stages:
            return None
        if "flow_stage_done" not in tools_dict:
            final_content = "error: flow_stage_done tool unavailable"
            messages.append({"role": "assistant", "content": final_content})
            summary = _metrics.summary()
            LAST_RUN_SUMMARY = summary
            logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})
            end_data = hooks.emit("agent_end", {
                "summary": summary,
                "messages": messages,
                "content": final_content,
                "turns": turns,
                "log_path": logging_hook.get_log_path(),
                "system_prompt": system_prompt,
                "phase_log_mode": phase_log_mode,
            })
            dump_info = end_data.get("conversation_dump")
            if dump_info:
                logging_hook.log_event("conversation_saved", dump_info)
            return final_content, messages

        flow_context_notes: List[str] = []
        flow_reports: List[Dict[str, Any]] = []

        for idx, stage in enumerate(stages, start=1):
            stage_id = str(stage.get("id") or f"stage_{idx}").strip().lower()
            stage_label = str(stage.get("label") or stage_id).strip() or stage_id
            stage_prompt_text = str(stage.get("prompt") or "").strip()
            done_hint = str(stage.get("done_hint") or "").strip()
            require_change = bool(stage.get("require_code_change", False))
            stage_context_window = stage.get("context_window")
            stage_tools_allow = stage.get("tools_allow")
            stage_tools_block = stage.get("tools_block")
            stage_require_files_read = stage.get("require_files_read")
            stage_require_non_flow_tool = stage.get("require_non_flow_tool")
            stage_allow_missing_done = stage.get("allow_missing_done")

            saved_task_id = TASK_ID
            saved_task_index = TASK_INDEX
            saved_task_total = TASK_TOTAL
            TASK_ID = stage_id
            TASK_INDEX = idx
            TASK_TOTAL = len(stages)

            attempt = 0
            content = ""
            stage_messages: List[Dict[str, Any]] = []
            status = "failed"
            sub_summary: Dict[str, Any] = {}
            stage_signal: Optional[Dict[str, Any]] = None
            retry_hint = ""
            try:
                while True:
                    FLOW_STAGE_SIGNAL = None
                    FLOW_STAGE_EXPECTED = stage_id

                    stage_prompt = (
                        f"[FLOW STAGE: {stage_label.upper()}]\n"
                        f"TASK:\n{prompt}\n"
                    )
                    if stage_prompt_text:
                        stage_prompt += f"\nSTAGE INSTRUCTIONS:\n{stage_prompt_text}\n"
                    context_window = flow_context_window if stage_context_window is None else int(stage_context_window or 0)
                    context_block = _format_flow_context(flow_context_notes, context_window)
                    if context_block:
                        stage_prompt += f"\nFLOW CONTEXT:\n{context_block}\n"

                    stage_prompt += (
                        "\nWhen this stage is complete, call flow_stage_done(stage=..., summary=..., "
                        "decisions=[...], next_actions=[...], files_read=[...], files_changed=[...], metadata={...}).\n"
                        "Keep any final text brief.\n"
                    )
                    if done_hint:
                        stage_prompt += f"\nDONE HINT:\n{done_hint}\n"
                    if attempt > 0:
                        stage_prompt += (
                            f"\nRETRY {attempt}/{flow_stage_retries}: "
                            "previous run did not call flow_stage_done. "
                            "You must call flow_stage_done to advance.\n"
                        )
                        if retry_hint:
                            stage_prompt += f"\nRETRY GUIDANCE:\n{retry_hint}\n"

                    sub_settings = dict(agent_settings)
                    sub_settings["task_branching"] = False
                    sub_settings["flow"] = None
                    sub_settings["require_code_change"] = require_change
                    sub_settings["min_tool_calls"] = max(1, int(sub_settings.get("min_tool_calls", 0) or 0))
                    flow_history_mode = agent_settings.get("flow_history_mode")
                    flow_history_max = agent_settings.get("flow_history_max_messages")
                    flow_history_keep = agent_settings.get("flow_history_keep_first")
                    flow_history_strip = agent_settings.get("flow_history_strip_thinking")
                    flow_history_trunc = agent_settings.get("flow_history_tool_truncate_chars")
                    flow_history_trunc_keep = agent_settings.get("flow_history_tool_truncate_keep_last")
                    if flow_history_mode is None:
                        flow_history_mode = agent_settings.get("history_mode", "full")
                    if flow_history_max is None:
                        flow_history_max = agent_settings.get("history_max_messages", 0)
                    if flow_history_keep is None:
                        flow_history_keep = agent_settings.get("history_keep_first", False)
                    if flow_history_strip is None:
                        flow_history_strip = agent_settings.get("history_strip_thinking", False)
                    if flow_history_trunc is None:
                        flow_history_trunc = agent_settings.get("history_tool_truncate_chars", 0)
                    if flow_history_trunc_keep is None:
                        flow_history_trunc_keep = agent_settings.get("history_tool_truncate_keep_last", 0)
                    stage_history_mode = stage.get("history_mode")
                    stage_history_max = stage.get("history_max_messages")
                    stage_history_keep = stage.get("history_keep_first")
                    stage_history_strip = stage.get("history_strip_thinking")
                    stage_history_trunc = stage.get("history_tool_truncate_chars")
                    stage_history_trunc_keep = stage.get("history_tool_truncate_keep_last")
                    if stage_history_mode is not None:
                        flow_history_mode = str(stage_history_mode).strip().lower()
                    if stage_history_max is not None:
                        flow_history_max = int(stage_history_max or 0)
                    if stage_history_keep is not None:
                        flow_history_keep = bool(stage_history_keep)
                    if stage_history_strip is not None:
                        flow_history_strip = bool(stage_history_strip)
                    if stage_history_trunc is not None:
                        flow_history_trunc = int(stage_history_trunc or 0)
                    if stage_history_trunc_keep is not None:
                        flow_history_trunc_keep = int(stage_history_trunc_keep or 0)
                    sub_settings["history_mode"] = flow_history_mode
                    sub_settings["history_max_messages"] = flow_history_max
                    sub_settings["history_keep_first"] = flow_history_keep
                    sub_settings["history_strip_thinking"] = flow_history_strip
                    sub_settings["history_tool_truncate_chars"] = flow_history_trunc
                    sub_settings["history_tool_truncate_keep_last"] = flow_history_trunc_keep

                    stage_tools = tools_dict
                    if isinstance(stage_tools_allow, (list, tuple, set)):
                        allow_set = {resolve_tool_name(str(name).strip()) for name in stage_tools_allow if str(name).strip()}
                        if not stage_allow_missing_done and "flow_stage_done" in tools_dict:
                            allow_set.add("flow_stage_done")
                        stage_tools = {name: tool for name, tool in tools_dict.items() if name in allow_set}
                    if isinstance(stage_tools_block, (list, tuple, set)) and stage_tools:
                        block_set = {resolve_tool_name(str(name).strip()) for name in stage_tools_block if str(name).strip()}
                        stage_tools = {name: tool for name, tool in stage_tools.items() if name not in block_set}
                    if not stage_allow_missing_done and "flow_stage_done" in tools_dict and "flow_stage_done" not in stage_tools:
                        stage_tools["flow_stage_done"] = tools_dict["flow_stage_done"]
                    saved_hooks = {ev: list(cbs) for ev, cbs in hooks._hooks.items()}
                    saved_current_messages = CURRENT_MESSAGES
                    content, stage_messages = run_agent(
                        stage_prompt,
                        system_prompt,
                        stage_tools,
                        sub_settings,
                        previous_messages=None,
                        task_depth=task_depth + 1,
                    )
                    if isinstance(LAST_RUN_SUMMARY, dict):
                        sub_summary = dict(LAST_RUN_SUMMARY)
                    hooks._hooks = saved_hooks
                    CURRENT_MESSAGES = saved_current_messages
                    FLOW_STAGE_EXPECTED = None

                    stage_signal = FLOW_STAGE_SIGNAL if isinstance(FLOW_STAGE_SIGNAL, dict) else None
                    status = "completed" if stage_signal else "failed"
                    if stage_signal and str(stage_signal.get("stage") or "").strip().lower() != stage_id:
                        status = "failed"
                        stage_signal = None
                    stage_non_flow_tools = 0
                    for msg in stage_messages:
                        if msg.get("role") != "assistant":
                            continue
                        for tc in msg.get("tool_calls") or []:
                            func = tc.get("function", {}) or {}
                            tool_name = resolve_tool_name(func.get("name", ""))
                            if tool_name and tool_name != "flow_stage_done":
                                stage_non_flow_tools += 1
                    stage_files_read = _extract_task_reads(stage_messages)
                    missing_hint = ""
                    if stage_require_non_flow_tool is not None and stage_non_flow_tools < int(stage_require_non_flow_tool or 0):
                        status = "failed"
                        if stage_signal:
                            stage_signal = None
                        missing_hint = "Use at least one non-flow tool before calling flow_stage_done."
                    if stage_require_files_read is not None and len(stage_files_read) < int(stage_require_files_read or 0):
                        status = "failed"
                        if stage_signal:
                            stage_signal = None
                        missing_hint = "Read at least one file before calling flow_stage_done."
                    if not stage_signal and stage_allow_missing_done:
                        require_non_flow = stage_require_non_flow_tool is None or stage_non_flow_tools >= int(stage_require_non_flow_tool or 0)
                        require_reads = stage_require_files_read is None or len(stage_files_read) >= int(stage_require_files_read or 0)
                        if require_non_flow and require_reads:
                            status = "completed"
                    stage_used_write = bool(_extract_task_files(stage_messages))
                    stage_made_change = _stage_made_change(stage_messages)
                    if status != "completed" and flow_retry_hints:
                        retry_hint = _build_flow_retry_hint(
                            stage_id,
                            stage_used_write,
                            stage_made_change,
                            sub_summary if sub_summary else None,
                        )
                        if missing_hint:
                            retry_hint = f"{retry_hint} {missing_hint}".strip()
                    if status == "completed" or not flow_stage_required or attempt >= flow_stage_retries:
                        break
                    attempt += 1
            finally:
                TASK_ID = saved_task_id
                TASK_INDEX = saved_task_index
                TASK_TOTAL = saved_task_total

            files_changed = _extract_task_files(stage_messages)
            files_read = _extract_task_reads(stage_messages)
            stage_used_write = bool(files_changed)
            stage_made_change = _stage_made_change(stage_messages)

            note, meta_payload = _build_flow_stage_note(stage_label, content, files_read, files_changed, stage_signal)
            flow_context_notes.append(note)

            report: Dict[str, Any] = {
                "stage": stage_id,
                "label": stage_label,
                "status": status,
                "attempts": attempt + 1,
                "retries": flow_stage_retries,
                "files_changed": files_changed,
                "files_read": files_read,
                "summary": meta_payload.get("summary") if isinstance(meta_payload, dict) else None,
                "payload": meta_payload,
                "used_write_tools": stage_used_write,
                "made_code_change": stage_made_change,
            }
            if sub_summary:
                report.update({
                    "tool_calls_total": sub_summary.get("tool_calls_total"),
                    "tool_errors_total": sub_summary.get("tool_errors_total"),
                    "tool_call_counts": sub_summary.get("tool_call_counts"),
                    "tool_error_counts": sub_summary.get("tool_error_counts"),
                    "analysis_retries": sub_summary.get("analysis_retries"),
                    "feedback_counts": sub_summary.get("feedback_counts"),
                })
            if status != "completed":
                report["error"] = (content or "")[:200]
            flow_reports.append(report)
            logging_hook.log_event("flow_stage_report", report)

            if status != "completed" and flow_stage_required:
                break

        if flow_stage_required and any(r.get("status") != "completed" for r in flow_reports):
            final_content = "error: flow stage did not complete"
        else:
            final_content = "ok: flow completed"

        if task_output_mode == "runtime":
            if _is_benchmark_mode():
                final_content = _benchmark_final_output(CONTINUE_SESSION)
        if _is_benchmark_mode() and benchmark_output_mode == "runtime":
            final_content = _benchmark_final_output(CONTINUE_SESSION)

        if final_content:
            hooks.emit("response_content", {"content": final_content, "turn": turns})
        messages.append({"role": "assistant", "content": final_content})

        summary = _metrics.summary()
        LAST_RUN_SUMMARY = summary
        logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})
        end_data = hooks.emit("agent_end", {
            "summary": summary,
            "messages": messages,
            "content": final_content,
            "turns": turns,
            "log_path": logging_hook.get_log_path(),
            "system_prompt": system_prompt,
            "phase_log_mode": phase_log_mode,
        })
        dump_info = end_data.get("conversation_dump")
        if dump_info:
            logging_hook.log_event("conversation_saved", dump_info)
        return final_content, messages

    def _execute_staged_flow() -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        global TASK_MANAGER, LAST_RUN_SUMMARY, CURRENT_MESSAGES, TASK_ID, TASK_INDEX, TASK_TOTAL
        if not task_branching or not task_manager:
            return None
        if task_flow_mode != "staged3":
            return None

        if not task_manager.has_tasks():
            task_manager.create_tasks([
                {"id": "context", "description": "Build context for the task using read-only tools."},
                {"id": "plan", "description": "Produce a concise plan for the task."},
                {"id": "implement", "description": "Implement the task."},
            ])

        tasks_list = task_manager.list_tasks()
        summaries: List[str] = []
        task_context_notes: List[str] = []
        context_summary = ""
        plan_summary = ""
        plan_steps: List[str] = []
        replan_max = int(agent_settings.get("task_replan_max", 0) or 0)

        for idx, task in enumerate(tasks_list, start=1):
            stage = str(task.task_id).strip().lower()
            saved_task_id = TASK_ID
            saved_task_index = TASK_INDEX
            saved_task_total = TASK_TOTAL
            TASK_ID = task.task_id
            TASK_INDEX = idx
            TASK_TOTAL = len(tasks_list)
            task_manager.start_task(task.task_id)
            attempt = 0
            content = ""
            task_messages: List[Dict[str, Any]] = []
            status = "failed"
            sub_summary: Dict[str, Any] = {}
            try:
                while True:
                    stage_prompt = (
                        f"[STAGE: {stage.upper()}]\n"
                        f"TASK:\n{prompt}\n"
                    )
                    if stage == "context":
                        stage_prompt += "Use read-only tools only. Your FIRST response MUST be a tool call.\n"
                    elif stage == "plan":
                        stage_prompt += (
                            "Call plan_tasks(action=\"create\", tasks=[...]) with a short list of plan steps. "
                            "No code changes. Your FIRST response MUST be that tool call.\n"
                        )
                    else:
                        stage_prompt += "Implement the task. Your FIRST response MUST be a tool call.\n"

                    if context_summary:
                        stage_prompt += f"\nCONTEXT:\n{context_summary}\n"
                    if plan_summary and stage == "implement":
                        stage_prompt += f"\nPLAN:\n{plan_summary}\n"

                    if attempt > 0:
                        stage_prompt = (
                            f"{stage_prompt}\n"
                            f"REPLAN attempt {attempt}/{replan_max}: previous attempt failed with: {content}"
                        )

                    sub_settings = dict(agent_settings)
                    sub_settings["task_branching"] = False
                    sub_settings["require_code_change"] = bool(agent_settings.get("require_code_change", False)) if stage == "implement" else False
                    sub_settings["min_tool_calls"] = 1

                    sub_tools = _filter_tools_for_stage(stage)
                    tool_names = [TOOL_DISPLAY_MAP.get(n, n) for n in sub_tools.keys()]
                    stage_system_prompt = (
                        f"{system_prompt}\n\n[STAGE TOOL LIST]\n"
                        f"Available tools for this stage: {', '.join(tool_names)}\n"
                        "Use ONLY these tools in this stage."
                    )
                    global TASKS_CAPTURE_MODE, TASKS_CAPTURED
                    if stage == "plan":
                        TASKS_CAPTURE_MODE = True
                        TASKS_CAPTURED = []
                    saved_hooks = {ev: list(cbs) for ev, cbs in hooks._hooks.items()}
                    saved_current_messages = CURRENT_MESSAGES
                    content, task_messages = run_agent(
                        stage_prompt,
                        stage_system_prompt,
                        sub_tools,
                        sub_settings,
                        previous_messages=None,
                        task_depth=task_depth + 1,
                    )
                    if isinstance(LAST_RUN_SUMMARY, dict):
                        sub_summary = dict(LAST_RUN_SUMMARY)
                    hooks._hooks = saved_hooks
                    CURRENT_MESSAGES = saved_current_messages
                    if stage == "plan":
                        TASKS_CAPTURE_MODE = False
                    status = "failed" if str(content).startswith("error:") else "completed"
                    if status == "completed" or attempt >= replan_max:
                        break
                    attempt += 1
            finally:
                TASK_ID = saved_task_id
                TASK_INDEX = saved_task_index
                TASK_TOTAL = saved_task_total

            files_changed = _extract_task_files(task_messages)
            files_read = _extract_task_reads(task_messages)
            attempts_used = attempt + 1

            if stage == "context":
                context_summary = _compact_stage_summary(content, files_read)
            elif stage == "plan":
                captured_steps = [t.get("description") for t in (TASKS_CAPTURED or []) if isinstance(t, dict)]
                plan_steps = [s for s in captured_steps if isinstance(s, str) and s.strip()]
                if plan_steps:
                    plan_summary = "\n".join(f"- {step.strip()}" for step in plan_steps)
                else:
                    plan_steps = _extract_plan_steps(content)
                    if plan_steps:
                        plan_summary = "\n".join(f"- {step}" for step in plan_steps)
                    else:
                        plan_summary = _compact_stage_summary(content, files_read)

            report: Dict[str, Any] = {
                "task_id": task.task_id,
                "status": status,
                "description": task.description,
                "attempts": attempts_used,
                "replan_max": replan_max,
                "files_changed": files_changed,
                "files_read": files_read,
                "plan_steps": plan_steps if stage == "plan" else [],
                "phase": stage,
            }
            if sub_summary:
                report.update({
                    "tool_calls_total": sub_summary.get("tool_calls_total"),
                    "tool_errors_total": sub_summary.get("tool_errors_total"),
                    "tool_call_counts": sub_summary.get("tool_call_counts"),
                    "tool_error_counts": sub_summary.get("tool_error_counts"),
                    "analysis_retries": sub_summary.get("analysis_retries"),
                    "feedback_counts": sub_summary.get("feedback_counts"),
                })
            if status != "completed":
                report["error"] = (content or "")[:200]

            report_errors = _validate_task_report(report)
            if report_errors:
                logging_hook.log_event("task_report_invalid", {
                    "task_id": task.task_id,
                    "errors": report_errors,
                    "status": status,
                })
            logging_hook.log_event("task_report", report)

            context_line = f"{task.task_id}: status={status}"
            if files_read:
                context_line += f"; read={', '.join(files_read)}"
            if files_changed:
                context_line += f"; changed={', '.join(files_changed)}"
            task_context_notes.append(context_line)

            summary = _build_task_summary(
                task.task_id,
                task.description,
                status,
                content or "",
                files_changed,
            )
            task_manager.end_task(
                task.task_id,
                status=status,
                summary=summary,
                files_changed=files_changed,
                files_read=files_read,
                error=(content if status == "failed" else None),
            )
            summaries.append(summary)
            if task_output_mode == "human":
                messages.append({"role": "system", "content": f"[TASK SUMMARY] {summary}"})

        TASK_MANAGER = None
        if task_output_mode == "runtime":
            if _is_benchmark_mode():
                final_content = _benchmark_final_output(CONTINUE_SESSION)
            else:
                final_content = "ok: completed"
        else:
            final_content = "\n".join(summaries) if summaries else "ok: no tasks executed"
        if _is_benchmark_mode() and benchmark_output_mode == "runtime":
            final_content = _benchmark_final_output(CONTINUE_SESSION)
        if final_content:
            hooks.emit("response_content", {"content": final_content, "turn": turns})
        messages.append({"role": "assistant", "content": final_content})
        summary = _metrics.summary()
        LAST_RUN_SUMMARY = summary
        logging_hook.log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})
        end_data = hooks.emit("agent_end", {
            "summary": summary,
            "messages": messages,
            "content": final_content,
            "turns": turns,
            "log_path": logging_hook.get_log_path(),
            "system_prompt": system_prompt,
            "phase_log_mode": phase_log_mode,
        })
        dump_info = end_data.get("conversation_dump")
        if dump_info:
            logging_hook.log_event("conversation_saved", dump_info)
        return final_content, messages

    if flow_enabled:
        flow_result = _execute_flow()
        if flow_result is not None:
            return flow_result

    if task_branching and task_flow_mode == "staged3":
        staged_result = _execute_staged_flow()
        if staged_result is not None:
            return staged_result

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
        if task_branching and task_manager:
            if task_manager.has_tasks():
                system_prompt_effective = f"{system_prompt}\n[TASKS]\n{task_manager.format_tasks()}"
            else:
                if task_plan_mode == "first":
                    system_prompt_effective = (
                        f"{system_prompt}\n"
                        "TASK MODE: Call plan_tasks(action=\"create\", tasks=[...]) as the FIRST tool call. "
                        "Do not use any other tools before planning."
                    )
                elif task_plan_mode == "explore":
                    system_prompt_effective = (
                        f"{system_prompt}\n"
                        "TASK MODE: You may use read-only tools to explore, then call "
                        "plan_tasks(action=\"create\", tasks=[...]) before any write/edit/patch."
                    )
        if phase_enabled and phase_state and phase_prompts:
            phase_hint = phase_prompts.get(phase_state)
            if isinstance(phase_hint, str) and phase_hint.strip():
                system_prompt_effective = f"{system_prompt_effective}\n{phase_hint.strip()}"
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
        thinking = message.get("thinking")
        if not thinking:
            thinking = message.get("reasoning_content")
        # Fallback: parse <think> from content when server returns thinking inline
        if not thinking and content and "</think>" in content:
            think_end = content.find("</think>")
            thinking = content[:think_end].strip()
            content = content[think_end + 8:].strip()

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
                "phase_log_mode": phase_log_mode,
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
            # Most templates expect reasoning_content (Kimi K2.5, DeepSeek, GLM)
            assistant_message["reasoning_content"] = thinking
            # Also set thinking field for compatibility (Harmony, etc.)
            if "glm" not in MODEL.lower():
                assistant_message["thinking"] = thinking
        tool_results: List[Dict[str, Any]] = []

        feedback_text = None
        feedback_reason = None
        plan_tasks_created = False
        finish_requested = False
        finish_payload: Dict[str, Any] = {}
        for tc in tool_calls:
            # Hook: tool_before (mutable — hooks can modify tool_args)
            tc_display_name = (tc.get("function") or {}).get("name") or ""
            resolved_name_pre = resolve_tool_name(tc_display_name)
            if task_branching and task_manager and not task_manager.has_tasks():
                if task_plan_mode == "first":
                    if resolved_name_pre != "plan_tasks":
                        feedback_text = (
                            "FORMAT ERROR: task planning required. "
                            "Call plan_tasks(action=\"create\", tasks=[...]) "
                            "as the FIRST tool call."
                        )
                        feedback_reason = "task_plan_required_first"
                        break
                elif task_plan_mode == "explore":
                    category = TOOL_CATEGORIES.get(resolved_name_pre)
                    if resolved_name_pre != "plan_tasks" and category not in ("read", "reasoning"):
                        feedback_text = (
                            "FORMAT ERROR: task planning required. "
                            "Use read-only tools to explore, then call "
                            "plan_tasks(action=\"create\", tasks=[...]) "
                            "before any write/edit/patch."
                        )
                        feedback_reason = "task_plan_required"
                        break
            before_data = hooks.emit("tool_before", {
                "tool_name": resolve_tool_name(tc_display_name),
                "tool_args": (tc.get("function") or {}).get("arguments", "{}"),
                "tool_call": tc,
            })

            resolved_name, tool_args, result, response_name = process_tool_call(tools_dict, tc)
            if resolved_name == "plan_tasks" and isinstance(tool_args, dict):
                action = str(tool_args.get("action") or "").strip().lower()
                if action == "create":
                    plan_tasks_created = True

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
                })
                dump_info = end_data.get("conversation_dump")
                if dump_info:
                    logging_hook.log_event("conversation_saved", dump_info)

                return content, messages

        if phase_enabled:
            turn_reads = _extract_task_reads([assistant_message])
            turn_changes = _extract_task_files([assistant_message])
            if turn_reads:
                phase_signals["files_read"].update(turn_reads)
            if turn_changes:
                phase_signals["files_changed"].update(turn_changes)
            for tool_name in resolved_tool_names:
                category = TOOL_CATEGORIES.get(tool_name)
                if category == "read":
                    phase_signals["read_tools"] += 1
                elif category == "write":
                    phase_signals["write_tools"] += 1
                if tool_name == "plan_tasks":
                    phase_signals["plan_tools"] += 1
            if content:
                if _extract_plan_steps(content):
                    phase_signals["plan_detected"] = True
            if code_change_made:
                phase_signals["code_change"] = True
            phase_snapshot = {
                "files_read": len(phase_signals["files_read"]),
                "files_changed": len(phase_signals["files_changed"]),
                "read_tools": phase_signals["read_tools"],
                "write_tools": phase_signals["write_tools"],
                "plan_tools": phase_signals["plan_tools"],
                "code_change": phase_signals["code_change"],
                "plan_detected": phase_signals["plan_detected"],
            }
            new_phase = _phase_update(phase_state, turns, resolved_tool_names)
            if new_phase != phase_state:
                phase_transition_payload = {
                    "turn": turns,
                    "from": phase_state,
                    "to": new_phase,
                    "signals": dict(phase_snapshot),
                }
                logging_hook.log_event("phase_transition", phase_transition_payload)
                if phase_log_stdout:
                    _print_phase_event("transition", phase_transition_payload)
                phase_state = new_phase
            phase_state_payload = {
                "turn": turns,
                "phase": phase_state,
                "signals": dict(phase_snapshot),
            }
            logging_hook.log_event("phase_state", phase_state_payload)
            if phase_log_stdout:
                _print_phase_event("state", phase_state_payload)

        if task_branching and plan_tasks_created and not feedback_text:
            task_result = _execute_task_branches()
            if task_result:
                return task_result

        if feedback_text:
            attempt_num = None
            if feedback_reason:
                _metrics.feedback_counts[feedback_reason] += 1
                attempt_num = _metrics.feedback_counts[feedback_reason]
                lines = feedback_text.splitlines()
                if lines:
                    lines[0] = f"{lines[0]} (attempt {attempt_num})"
                    feedback_text = "\n".join(lines)

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

        # Write nudge: remind model to write if running low on turns without code change
        if (require_code_change and not code_change_made and not write_nudge_sent):
            effective_turns = turns - format_retry_turns
            nudge_threshold = max(5, agent_max_turns // 3)
            if effective_turns >= nudge_threshold:
                write_nudge_sent = True
                remaining = agent_max_turns - effective_turns
                available_write_tools = get_available_write_tools(tools_dict)
                tools_str = "/".join(available_write_tools) if available_write_tools else "write_file"
                nudge_msg = (
                    f"IMPORTANT: You have used {effective_turns} of {agent_max_turns} turns without writing code. "
                    f"You have {remaining} turns left. Use {tools_str} NOW to implement your solution."
                )
                messages.append({"role": "user", "content": nudge_msg})
                logging_hook.log_event("write_nudge", {
                    "turn": turns,
                    "effective_turn": effective_turns,
                    "remaining": remaining,
                })

        # Finish nudge: avoid looping near turn limit when code has already changed.
        if code_change_made and not finish_nudge_sent:
            effective_turns = turns - format_retry_turns
            remaining = agent_max_turns - effective_turns
            if remaining <= 2:
                finish_nudge_sent = True
                finish_tool = TOOL_DISPLAY_MAP.get("finish", "finish")
                finish_msg = (
                    f"IMPORTANT: {remaining} turns left. "
                    f"If your implementation is complete, call {finish_tool} now. "
                    "If not complete, make one final targeted change."
                )
                messages.append({"role": "user", "content": finish_msg})
                logging_hook.log_event("finish_nudge", {
                    "turn": turns,
                    "effective_turn": effective_turns,
                    "remaining": remaining,
                })

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
        "plan_tasks": plan_tasks,
        "flow_stage_done": flow_stage_done,
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
