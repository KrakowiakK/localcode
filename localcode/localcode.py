#!/usr/bin/env python3
"""
localcode - agent runner with native tool calls.

- Single-file, no duplicate defs.
- Sandbox-safe filesystem tools.
- Robust tool arg repair (number words for read line ranges).
"""

import argparse
import difflib
import fnmatch
import glob as globlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

LOG_PATH: Optional[str] = None
AGENT_NAME: Optional[str] = None
AGENT_SETTINGS: Dict[str, Any] = {}
CONTINUE_SESSION = False
INTERACTIVE_MODE = False
LAST_RUN_SUMMARY: Optional[Dict[str, Any]] = None
RUN_NAME: Optional[str] = None
TASK_ID: Optional[str] = None
TASK_INDEX: Optional[int] = None
TASK_TOTAL: Optional[int] = None

# Sandbox root (cwd by default unless --no-sandbox)
SANDBOX_ROOT: Optional[str] = None

# Current conversation messages (for tools that need history access like plan_solution)
CURRENT_MESSAGES: List[Dict[str, Any]] = []

# Extract the first file path from a unified patch block.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File):\s+(.+)$", re.MULTILINE)

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

UNSUPPORTED_TOOLS: Dict[str, str] = {}

# Alias resolution (alias -> canonical) and display name overrides (canonical -> display)
TOOL_ALIAS_MAP: Dict[str, str] = {}

# Tool categories for semantic filtering (tool_name -> "read"/"write")
TOOL_CATEGORIES: Dict[str, str] = {}


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

TOOL_DISPLAY_MAP: Dict[str, str] = {}

TEST_MENTION_RE = re.compile(
    r"\b(run tests?|tests?|npm test|jest|pytest|go test|cargo test|ctest|yarn test|pnpm test)\b",
    re.IGNORECASE,
)

# Dangerous command patterns (soft sandbox)
DANGEROUS_PATTERNS = [
    r"rm\s+(-[rf]+\s+)*(/|~|\$HOME|/\*)",
    r"rm\s+.*\s+(/etc|/usr|/bin|/lib|/boot|/var|/sys|/proc)",
    r"(mv|cp)\s+.*\s+(/etc|/usr|/bin|/lib|/boot)/",
    r"dd\s+.*of=/dev/",
    r"mkfs\.",
    r"^sudo\s+",
    r"^su\s+",
    r"chmod\s+(-R\s+)?(777|666)\s+/",
    r";\s*(rm|mv|dd|mkfs|sudo|su)\s+",
    r"\|\s*(rm|mv|dd|mkfs|sudo|su)\s+",
    r":\(\)\s*\{",
    r"(curl|wget).*\|\s*(ba)?sh",
    r"(?:\d\s*)?>{1,2}\s*/(?:etc|usr|bin|lib|boot|var|sys|proc)/",
    r"tee\b.*\s+/(?:etc|usr|bin|lib|boot)/",
]
DANGEROUS_COMMAND_RES = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]

# Shell chaining operators blocked in sandbox mode
# NOTE: pipe (|) is checked token-level in _check_sandbox_allowlist to avoid
# false positives on "|" inside quoted args (e.g. rg "a|b").
_SHELL_CHAINING_RE = re.compile(r';|&&|\|\||`|\n|\r|\$\(|(^|\s)\.\./')
_SHELL_CD_RE = re.compile(r'^\s*cd\b')

# Allowlist of binaries permitted in sandbox mode.
# Only the basename of the first token (the command) is checked.
_SANDBOX_ALLOWED_CMDS = frozenset({
    # Language runtimes (without inline code flags — checked separately)
    "python", "python3", "python3.8", "python3.9", "python3.10", "python3.11",
    "python3.12", "python3.13", "python3.14",
    "node",
    # Core utilities
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "tr", "cut", "tee",
    "echo", "printf", "true", "false", "test", "expr",
    "cp", "mv", "mkdir", "touch", "chmod", "dirname", "basename", "realpath",
    "find", "xargs",
    # Search / diff
    "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "diff", "patch",
    # Build / package (read-only or project-scoped)
    "git", "npm", "npx", "yarn", "pnpm", "pip", "pip3", "cargo", "make",
    "go", "rustc", "javac", "java", "gcc", "g++", "clang", "clang++",
    # Other common
    "env", "which", "file", "stat", "du", "df", "uname", "date", "whoami",
})

# Flags that allow arbitrary code execution in interpreters.
# Blocked in sandbox to prevent escapes like: python -c "import os; ..."
_SANDBOX_INLINE_CODE_RE = re.compile(
    r"(?:^|\s)(?:"
    r"python[0-9.]*\s+(?:-[a-zA-Z]*c|-c)"      # python -c, python3 -c, python -Sc etc.
    r"|node\s+(?:-e|--eval|-p|--print)"          # node -e / --eval / -p / --print
    r"|perl\s+-[a-zA-Z]*e"                       # perl -e, perl -ne, etc.
    r"|ruby\s+-[a-zA-Z]*e"                       # ruby -e
    r"|sh\s+-c|bash\s+-c|zsh\s+-c"              # sh -c, bash -c, zsh -c (defense-in-depth)
    r")",
    re.IGNORECASE,
)

_ENV_VAR_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

DEFAULT_SHELL_TIMEOUT_MS = 30000
MAX_SHELL_TIMEOUT_MS = 10 * 60 * 1000
MAX_SHELL_OUTPUT_CHARS = 30000

MAX_FILE_SIZE = 250 * 1024  # 250KB
MAX_SINGLE_FILE_SCAN = 2 * 1024 * 1024  # 2MB
DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_GLOB_RESULTS = 100
MAX_GREP_RESULTS = 100
DEFAULT_IGNORE_DIRS = {".git", "node_modules", ".localcode", ".nanocode", "__pycache__"}

# Track last-read versions (LRU cache, max 200 entries)
MAX_FILE_VERSIONS = 200
FILE_VERSIONS: OrderedDict = OrderedDict()

# Session path
CURRENT_SESSION_PATH: Optional[str] = None

# Track last patch hash per file to detect repeated identical patches
_LAST_PATCH_HASH: Dict[str, str] = {}

# Track consecutive no-op counts per file per tool
_NOOP_COUNTS: Dict[str, Dict[str, int]] = {}  # {path: {"apply_patch": N, "write": N}}


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

    if SANDBOX_ROOT:
        real_path = os.path.realpath(abs_path)
        if not _is_path_within_sandbox(real_path, SANDBOX_ROOT):
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


def _truncate_shell_output(text: str) -> str:
    if len(text) <= MAX_SHELL_OUTPUT_CHARS:
        return text
    head_len = MAX_SHELL_OUTPUT_CHARS // 2
    tail_len = MAX_SHELL_OUTPUT_CHARS - head_len
    removed = len(text) - MAX_SHELL_OUTPUT_CHARS
    return f"{text[:head_len]}\n...[truncated {removed} chars]...\n{text[-tail_len:]}"


def _shell_payload(output: str, exit_code: int, duration_seconds: float, timed_out: bool = False) -> str:
    meta = {"exit_code": exit_code, "duration_seconds": duration_seconds}
    if timed_out:
        meta["timed_out"] = True
    return json.dumps({"output": output, "metadata": meta}, ensure_ascii=False)


def _check_dangerous_command(command: str) -> Optional[str]:
    for pattern_re in DANGEROUS_COMMAND_RES:
        if pattern_re.search(command):
            return pattern_re.pattern
    return None


def _check_sandbox_allowlist(command: str) -> Optional[str]:
    """Return an error string if command's binary is not in the sandbox allowlist, else None."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Malformed quoting — shlex.split in shell() will catch this too
        tokens = command.split()
    if not tokens:
        return None
    # Skip leading env-var assignments (e.g. VAR=1 python script.py)
    cmd_idx = 0
    while cmd_idx < len(tokens) and _ENV_VAR_ASSIGN_RE.match(tokens[cmd_idx]):
        cmd_idx += 1
    if cmd_idx >= len(tokens):
        return "error: command contains only variable assignments, no actual command"
    cmd_token = tokens[cmd_idx]
    if "/" in cmd_token:
        return (
            f"error: command paths ('{cmd_token}') are not allowed in sandbox; "
            f"use the bare command name instead (e.g. 'ls' not '/bin/ls')."
        )
    binary = os.path.basename(cmd_token)
    if binary not in _SANDBOX_ALLOWED_CMDS:
        return (
            f"error: command '{binary}' is not in the sandbox allowlist; "
            f"allowed: python, python3, node, ls, cat, grep, rg, git, "
            f"npm, make, echo, etc. Use an allowed command or request sandbox changes."
        )
    # Block inline-code flags for interpreters (python -c, node -e, perl -e, sh -c, etc.)
    if _SANDBOX_INLINE_CODE_RE.search(command):
        return (
            f"error: inline code execution (e.g. -c / -e flags) is not allowed in sandbox; "
            f"write a script file and run it instead."
        )
    # Token-level pipe check: block "|" as a standalone token (shell pipe operator).
    # This catches "ls|cat" (shlex splits to ["ls|cat"] — no match) and "ls | cat"
    # (shlex splits to ["ls", "|", "cat"] — match) without false-positiving on
    # "|" inside quoted arguments like rg "a|b".
    if "|" in tokens:
        return (
            "error: pipe operator (|) is not allowed in sandbox; "
            "run commands separately instead."
        )
    return None


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


def log_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    if not LOG_PATH:
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event_type}
    if RUN_NAME:
        rec["run_name"] = RUN_NAME
    if TASK_ID:
        rec["task_id"] = TASK_ID
    if TASK_INDEX:
        rec["task_index"] = TASK_INDEX
    if TASK_TOTAL:
        rec["task_total"] = TASK_TOTAL
    if AGENT_NAME:
        rec["agent"] = AGENT_NAME
    if payload:
        rec.update(payload)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def init_logging() -> None:
    global LOG_PATH
    if LOG_PATH:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    safe_agent = re.sub(r"[^A-Za-z0-9_.-]", "_", AGENT_NAME or "agent")
    LOG_PATH = os.path.join(LOG_DIR, f"localcode_{safe_agent}_{timestamp}.jsonl")
    log_event("session_start", {
        "model": MODEL,
        "cwd": os.getcwd(),
        "log_path": LOG_PATH,
        "mode": "single_agent_native_tools",
        "agent": AGENT_NAME,
        "agent_settings": AGENT_SETTINGS,
    })


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
    with open(CURRENT_SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    log_event("session_saved", {"path": CURRENT_SESSION_PATH, "message_count": len(messages)})


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
        log_event("session_loaded", {"path": latest, "message_count": len(msgs)})
        return msgs
    except Exception as e:
        log_event("session_load_error", {"path": latest, "error": str(e)})
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
# Tool schema and handlers
# ---------------------------

def normalize_args(args: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(args, dict):
        return None
    return dict(args)


def _require_args_dict(args: Any, tool_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    normalized = normalize_args(args)
    if normalized is None:
        return None, f"error: invalid arguments for tool '{tool_name}': expected object"
    return normalized, None


def read(args: Any) -> str:
    args, err = _require_args_dict(args, "read")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=True)
    except ValueError as e:
        return f"error: {e}"

    try:
        stat = os.stat(path)
        if stat.st_size > MAX_FILE_SIZE:
            return f"error: file too large ({stat.st_size} bytes, max {MAX_FILE_SIZE})"
    except OSError as e:
        return f"error: cannot stat file: {path} ({e})"

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read(MAX_FILE_SIZE + 1)
            if len(content) > MAX_FILE_SIZE:
                return f"error: file content exceeds {MAX_FILE_SIZE} bytes during read"
    except OSError as e:
        return f"error: cannot read file: {path} ({e})"

    diff_mode = bool(args.get("diff", False))
    if diff_mode and (args.get("line_start") is not None or args.get("line_end") is not None):
        return "error: diff cannot be combined with line_start/line_end"

    if diff_mode and path in FILE_VERSIONS:
        previous = FILE_VERSIONS[path]
        if content == previous:
            return "(no changes since last read)"
        diff_lines = list(difflib.unified_diff(
            previous.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"{path} (previous)",
            tofile=f"{path} (current)",
        ))
        _track_file_version(path, content)
        _LAST_PATCH_HASH.pop(path, None)
        return "".join(diff_lines) if diff_lines else "(no changes since last read)"

    _track_file_version(path, content)
    _LAST_PATCH_HASH.pop(path, None)

    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    line_start = args.get("line_start")
    line_end = args.get("line_end")

    if line_start is not None or line_end is not None:
        try:
            start_line = int(line_start) if line_start is not None else 1
        except (TypeError, ValueError):
            return "error: line_start must be an integer"
        try:
            end_line = int(line_end) if line_end is not None else None
        except (TypeError, ValueError):
            return "error: line_end must be an integer"

        if start_line < 1:
            start_line = 1
        offset = start_line - 1

        if offset >= total_lines:
            return f"error: line_start {start_line} is out of range (file has {total_lines} lines)"

        if end_line is not None:
            if end_line < start_line:
                return "error: line_end must be >= line_start"
            requested = end_line - start_line + 1
            limit = min(requested, total_lines - offset)
        else:
            try:
                limit = int(args.get("limit", DEFAULT_READ_LIMIT))
            except (TypeError, ValueError):
                return "error: limit must be an integer"
            if limit < 1:
                return "error: limit must be >= 1"
            limit = min(limit, total_lines - offset)
    else:
        try:
            offset = int(args.get("offset", 0))
        except (TypeError, ValueError):
            return "error: offset must be an integer"
        try:
            limit = int(args.get("limit", DEFAULT_READ_LIMIT))
        except (TypeError, ValueError):
            return "error: limit must be an integer"
        if offset < 0:
            return "error: offset must be >= 0"
        if offset >= total_lines:
            return f"error: offset {offset} is out of range (file has {total_lines} lines)"
        if limit < 1:
            return "error: limit must be >= 1"
        limit = min(limit, total_lines - offset)

    selected = lines[offset: offset + limit]

    out_parts: List[str] = []
    for i, line in enumerate(selected):
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + "...\n"
        out_parts.append(f"{offset + i + 1:4}| {line}")

    output = "".join(out_parts)

    lines_shown = offset + len(selected)
    if lines_shown < total_lines:
        output += f"\n(... {total_lines - lines_shown} more lines, use offset={lines_shown} to continue)"

    return output


def batch_read(args: Any) -> str:
    """Read multiple files in a single call."""
    args, err = _require_args_dict(args, "batch_read")
    if err:
        return err

    paths = args.get("paths")
    if not paths or not isinstance(paths, list):
        return "error: paths must be a non-empty array of file paths"

    if len(paths) > 10:
        return "error: maximum 10 files per batch_read call"

    results = []
    for p in paths:
        results.append(f"\n=== FILE: {p} ===")
        # Reuse read logic for each file
        file_result = read({"path": p})
        results.append(file_result)

    return "\n".join(results)


def write(args: Any) -> str:
    args, err = _require_args_dict(args, "write")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=False)
    except ValueError as e:
        return f"error: {e}"

    content = args.get("content")
    if content is None:
        return "error: content is required"
    if not isinstance(content, str):
        return "error: content must be a string"

    old_content = ""
    is_new_file = True
    if os.path.exists(path):
        is_new_file = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                old_content = f.read()
        except Exception:
            old_content = ""
        if old_content == content:
            _NOOP_COUNTS.setdefault(path, {})
            _NOOP_COUNTS[path]["write"] = _NOOP_COUNTS[path].get("write", 0) + 1
            noop_n = _NOOP_COUNTS[path]["write"]
            if noop_n == 1:
                # First no-op: benign (model may have solved it already).
                # Return ok so benchmark doesn't penalise, but include
                # "no changes" so _did_tool_make_change() returns False.
                return (
                    f"ok: no changes (file already has identical content) for {path}. "
                    "If the file is already correct you may stop."
                )
            hint = ""
            if noop_n >= 2:
                hint = " You have written identical content multiple times. Read the file and write DIFFERENT content."
            return (
                f"error: write made no changes for {path} "
                f"(file already has identical content).{hint}\n"
                "ACTION: read the file, then write content that actually differs, "
                "or stop if the file is already correct."
            )

    parent_dir = os.path.dirname(path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    _track_file_version(path, content)

    # Clear noop count on real change
    if path in _NOOP_COUNTS:
        _NOOP_COUNTS[path].pop("write", None)

    if is_new_file:
        additions = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"ok: created {path}, +{additions} lines"

    old_lines = old_content.count("\n")
    new_lines = content.count("\n")
    additions = max(0, new_lines - old_lines)
    removals = max(0, old_lines - new_lines)
    return f"ok: updated {path}, +{additions} -{removals} lines"


def edit(args: Any) -> str:
    args, err = _require_args_dict(args, "edit")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=True)
    except ValueError as e:
        return f"error: {e}"

    old = args.get("old")
    new = args.get("new")
    if old is None:
        return "error: old is required"
    if new is None:
        return "error: new is required"
    if not isinstance(old, str) or not isinstance(new, str):
        return "error: old and new must be strings"

    # Early check: if old == new, return error (prevents model loops)
    if old == new:
        return "error: no changes (old_string equals new_string)"

    if path not in FILE_VERSIONS:
        return f"error: must read {path} before editing (use read tool first)"

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return f"error: file not found: {path}"

    if old not in text:
        return "error: old_string not found in file. Make sure it matches exactly, including whitespace"

    count = text.count(old)
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true to replace all)"

    replacement = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    if replacement == text:
        return "error: no changes made (old_string equals new_string)"

    with open(path, "w", encoding="utf-8") as f:
        f.write(replacement)

    _track_file_version(path, replacement)
    return f"ok: {count if args.get('all') else 1} replacement(s)"


def _normalize_indent(line: str) -> str:
    """Strip leading and trailing whitespace for fuzzy patch matching."""
    return line.strip()


def _find_sublist(haystack: List[str], needle: List[str]) -> int:
    """Find needle in haystack. Returns index or -1. Raises if not unique."""
    if not needle:
        return 0
    limit = len(haystack) - len(needle) + 1
    match_index = -1
    # Try exact match first
    for idx in range(limit):
        if haystack[idx: idx + len(needle)] == needle:
            if match_index >= 0:
                raise ValueError("patch context not unique")
            match_index = idx
    if match_index >= 0:
        return match_index
    # Fallback: fuzzy match ignoring leading whitespace differences
    needle_norm = [_normalize_indent(ln) for ln in needle]
    for idx in range(limit):
        hay_slice = [_normalize_indent(haystack[idx + i]) for i in range(len(needle))]
        if hay_slice == needle_norm:
            if match_index >= 0:
                raise ValueError("patch context not unique")
            match_index = idx
    return match_index


def _log_fuzzy_match(file_path: str, haystack: List[str], needle: List[str], match_idx: int) -> None:
    """Log when fuzzy matching is used (whitespace differences in patch context)."""
    diffs = []
    for i, (h, n) in enumerate(zip(haystack[match_idx:match_idx+len(needle)], needle)):
        if h != n:
            h_spaces = len(h) - len(h.lstrip(' '))
            n_spaces = len(n) - len(n.lstrip(' '))
            diffs.append(f"line {i}: file={h_spaces}sp patch={n_spaces}sp")
    if diffs:
        print(f"\n[FUZZY_MATCH] {file_path} @ line {match_idx}: {'; '.join(diffs)}", file=sys.stderr)
        print(f"  First diff - FILE:  {repr(haystack[match_idx][:60])}", file=sys.stderr)
        print(f"  First diff - PATCH: {repr(needle[0][:60])}", file=sys.stderr)


def _parse_hunks(change_lines: List[str]) -> List[List[str]]:
    hunks: List[List[str]] = []
    current: List[str] = []
    for line in change_lines:
        if line.startswith("@@"):
            if current:
                hunks.append(current)
                current = []
            continue
        if line.startswith("*** End of File"):
            continue
        if line[:1] in (" ", "+", "-"):
            current.append(line)
            continue
        if line == "":
            # Auto-repair: treat empty line as context (space prefix)
            current.append(" ")
            continue
        raise ValueError(f"invalid patch line: {line}")
    if current:
        hunks.append(current)
    if not hunks:
        raise ValueError("no changes found in patch")
    return hunks


def _get_indent(line: str) -> int:
    """Count leading spaces in a line."""
    return len(line) - len(line.lstrip(' '))


def _adjust_indent(lines: List[str], delta: int) -> List[str]:
    """Adjust indentation of all lines by delta spaces."""
    if delta == 0:
        return lines
    result = []
    for ln in lines:
        if delta > 0:
            result.append(' ' * delta + ln)
        else:
            # Remove spaces but don't go negative
            remove = min(-delta, _get_indent(ln))
            result.append(ln[remove:])
    return result


def _apply_hunks(text_lines: List[str], hunks: List[List[str]], file_path: str = "") -> List[str]:
    for hunk in hunks:
        before = [ln[1:] for ln in hunk if ln[:1] in (" ", "-")]
        after = [ln[1:] for ln in hunk if ln[:1] in (" ", "+")]
        if not before:
            raise ValueError("patch hunk has no context lines; include at least one ' ' or '-' line for context")
        start = _find_sublist(text_lines, before)
        if start < 0:
            raise ValueError("patch context not found")
        # Check if we need indent correction (fuzzy match was used)
        actual_first = text_lines[start] if start < len(text_lines) else ""
        patch_first = before[0] if before else ""
        if actual_first != patch_first and _normalize_indent(actual_first) == _normalize_indent(patch_first):
            # Fuzzy match was used - log it and correct indentation
            _log_fuzzy_match(file_path, text_lines, before, start)
            indent_delta = _get_indent(actual_first) - _get_indent(patch_first)
            after = _adjust_indent(after, indent_delta)
        text_lines = text_lines[:start] + after + text_lines[start + len(before):]
    return text_lines


def _apply_update_patch(path: str, change_lines: List[str], move_to: Optional[str] = None) -> None:
    if not os.path.exists(path):
        raise ValueError(f"file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    had_trailing_newline = text.endswith("\n") or text.endswith("\r")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    text_lines = text.splitlines()
    hunks = _parse_hunks(change_lines)
    text_lines = _apply_hunks(text_lines, hunks, file_path=path)
    new_text = newline.join(text_lines)
    if had_trailing_newline:
        new_text += newline
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    if move_to:
        os.replace(path, move_to)


def _apply_add_patch(path: str, change_lines: List[str]) -> None:
    if os.path.exists(path):
        raise ValueError(f"file already exists: {path}")
    content: List[str] = []
    for line in change_lines:
        if line.startswith("*** End of File"):
            continue
        if not line.startswith("+"):
            raise ValueError(f"invalid add line: {line}")
        content.append(line[1:])
    new_text = "\n".join(content) + ("\n" if content else "")
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def _apply_delete_patch(path: str) -> None:
    if not os.path.exists(path):
        raise ValueError(f"file not found: {path}")
    os.remove(path)


def apply_patch_fn(args: Any) -> str:
    args, err = _require_args_dict(args, "apply_patch")
    if err:
        return err
    patch_text = args.get("patch") or args.get("diff")
    if not patch_text or not isinstance(patch_text, str):
        return "error: patch is required"

    try:
        lines = patch_text.splitlines()
        if not lines or lines[0].strip() != "*** Begin Patch":
            return "error: invalid patch format (missing Begin Patch)"
        if not any(line.strip() == "*** End Patch" for line in lines):
            return "error: invalid patch format (missing End Patch)"

        # Repeat detection: per-file block hashing
        # Split patch into per-file blocks and hash each separately
        patch_file_hashes: Dict[str, str] = {}
        current_path: Optional[str] = None
        current_block_lines: List[str] = []
        for _line in lines:
            raw = None
            if _line.startswith("*** Update File: "):
                raw = _line[len("*** Update File: "):].strip()
            elif _line.startswith("*** Add File: "):
                raw = _line[len("*** Add File: "):].strip()
            elif _line.startswith("*** Delete File: "):
                raw = _line[len("*** Delete File: "):].strip()
            if raw:
                # Flush previous block
                if current_path is not None and current_block_lines:
                    block_text = "\n".join(current_block_lines)
                    patch_file_hashes[current_path] = _sha256(block_text.encode("utf-8"))
                try:
                    validated = _validate_path(raw, check_exists=False)
                except Exception:
                    validated = os.path.abspath(raw)
                current_path = validated
                current_block_lines = [_line]
            elif current_path is not None:
                current_block_lines.append(_line)
        # Flush last block
        if current_path is not None and current_block_lines:
            block_text = "\n".join(current_block_lines)
            patch_file_hashes[current_path] = _sha256(block_text.encode("utf-8"))

        # Check per-file hashes for repeats (do NOT store yet — store after success)
        for vpath, file_hash in patch_file_hashes.items():
            if _LAST_PATCH_HASH.get(vpath) == file_hash:
                return (
                    f"error: repeated patch detected for {vpath}; "
                    "do not repeat the same patch. Re-read the file and use "
                    "a different patch with correct context, or switch to edit/write."
                )

        # First pass: validate update files were read
        idx = 1
        while idx < len(lines):
            line = lines[idx]
            if line.strip() == "*** End Patch":
                break
            if line.startswith("*** Update File: "):
                raw_path = line[len("*** Update File: "):].strip()
                path = _validate_path(raw_path, check_exists=True)
                if path not in FILE_VERSIONS:
                    return f"error: must read {raw_path} before patching (use read tool first)"
            idx += 1

        idx = 1
        files_changed: List[str] = []
        additions = 0
        removals = 0

        while idx < len(lines):
            line = lines[idx]
            if line.strip() == "*** End Patch":
                break

            if line.startswith("*** Update File: "):
                raw_path = line[len("*** Update File: "):].strip()
                path = _validate_path(raw_path, check_exists=True)
                # Snapshot before applying patch
                old_bytes = _read_file_bytes(path)
                idx += 1
                move_to = None
                if idx < len(lines) and lines[idx].startswith("*** Move to: "):
                    raw_move = lines[idx][len("*** Move to: "):].strip()
                    move_to = _validate_path(raw_move, check_exists=False)
                    idx += 1
                change_lines: List[str] = []
                while idx < len(lines) and not lines[idx].startswith("*** "):
                    cl = lines[idx]
                    change_lines.append(cl)
                    if cl.startswith("+") and not cl.startswith("+++"):
                        additions += 1
                    elif cl.startswith("-") and not cl.startswith("---"):
                        removals += 1
                    idx += 1
                _apply_update_patch(path, change_lines, move_to=move_to)
                updated = move_to or path
                # Check for no-op (file unchanged after patch)
                new_bytes = _read_file_bytes(updated)
                if old_bytes is not None and new_bytes is not None and old_bytes == new_bytes:
                    noop_key = updated
                    _NOOP_COUNTS.setdefault(noop_key, {})
                    _NOOP_COUNTS[noop_key]["apply_patch"] = _NOOP_COUNTS[noop_key].get("apply_patch", 0) + 1
                    noop_n = _NOOP_COUNTS[noop_key]["apply_patch"]
                    hint = ""
                    if noop_n >= 2:
                        hint = " STOP using apply_patch for this file; switch to edit or write."
                    return (
                        f"error: patch produced no changes for {updated} (no-op). "
                        f"The file content is identical before and after.{hint}\n"
                        "ACTION: read the file, then create a patch that actually modifies content, "
                        "or use edit/write."
                    )
                try:
                    with open(updated, "r", encoding="utf-8") as f:
                        _track_file_version(updated, f.read())
                except Exception:
                    FILE_VERSIONS.pop(updated, None)
                # Clear noop count on real change
                if updated in _NOOP_COUNTS:
                    _NOOP_COUNTS[updated].pop("apply_patch", None)
                if move_to and updated != path:
                    FILE_VERSIONS.pop(path, None)
                # Store hash for this file now (half-success safe)
                orig_hash = patch_file_hashes.get(path)
                if orig_hash:
                    _LAST_PATCH_HASH[updated] = orig_hash
                    if move_to and updated != path:
                        _LAST_PATCH_HASH.pop(path, None)
                files_changed.append(updated)
                continue

            if line.startswith("*** Add File: "):
                raw_path = line[len("*** Add File: "):].strip()
                path = _validate_path(raw_path, check_exists=False)
                idx += 1
                change_lines = []
                while idx < len(lines) and not lines[idx].startswith("*** "):
                    cl = lines[idx]
                    change_lines.append(cl)
                    if cl.startswith("+"):
                        additions += 1
                    idx += 1
                _apply_add_patch(path, change_lines)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        _track_file_version(path, f.read())
                except Exception:
                    FILE_VERSIONS.pop(path, None)
                if path in patch_file_hashes:
                    _LAST_PATCH_HASH[path] = patch_file_hashes[path]
                files_changed.append(path)
                continue

            if line.startswith("*** Delete File: "):
                raw_path = line[len("*** Delete File: "):].strip()
                path = _validate_path(raw_path, check_exists=True)
                idx += 1
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            removals += len(f.readlines())
                    except Exception:
                        pass
                _apply_delete_patch(path)
                FILE_VERSIONS.pop(path, None)
                if path in patch_file_hashes:
                    _LAST_PATCH_HASH[path] = patch_file_hashes[path]
                files_changed.append(path)
                continue

            return f"error: unexpected patch line: {line}"

        if not files_changed:
            return "error: no file operations found in patch"

        return f"ok: {len(files_changed)} file(s) changed, +{additions} -{removals}"
    except Exception as exc:
        return f"error: {exc}"


def glob_fn(args: Any) -> str:
    args, err = _require_args_dict(args, "glob")
    if err:
        return err
    pat = args.get("pat", "*")
    path = args.get("path", ".") or "."
    try:
        path = _validate_path(path, check_exists=True)
    except ValueError as e:
        return f"error: {e}"

    if not os.path.isdir(path):
        return f"error: path does not exist: {path}"

    def _safe_mtime(fp: str) -> float:
        try:
            return os.path.getmtime(fp) if os.path.isfile(fp) else 0
        except OSError:
            return 0

    if shutil.which("rg"):
        cmd = ["rg", "--files", "-g", str(pat), path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode in (0, 1):
            files = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            files = [f for f in files if not _is_ignored_path(f)]
            if len(files) <= 200:
                files.sort(key=_safe_mtime, reverse=True)
            else:
                files.sort()
            truncated = len(files) > MAX_GLOB_RESULTS
            files = files[:MAX_GLOB_RESULTS]
            if not files:
                return "no files found"
            out = "\n".join(files)
            if truncated:
                out += "\n\n(results are truncated; refine path or pattern)"
            return out

    pattern = os.path.join(path, str(pat))
    files = globlib.glob(pattern, recursive=True)
    files = [f for f in files if not _is_ignored_path(f)]
    if len(files) <= 200:
        files.sort(key=_safe_mtime, reverse=True)
    else:
        files.sort()
    truncated = len(files) > MAX_GLOB_RESULTS
    files = files[:MAX_GLOB_RESULTS]
    if not files:
        return "no files found"
    out = "\n".join(files)
    if truncated:
        out += "\n\n(results are truncated; refine path or pattern)"
    return out


def grep_fn(args: Any) -> str:
    args, err = _require_args_dict(args, "grep")
    if err:
        return err
    pat = args.get("pat")
    if not pat or not isinstance(pat, str):
        return "error: pat (pattern) is required"
    path = args.get("path", ".") or "."
    include = args.get("include")
    literal_text_raw = args.get("literal_text", False)
    if literal_text_raw is not None and not isinstance(literal_text_raw, bool):
        return "error: literal_text must be boolean"
    literal_text = bool(literal_text_raw)

    try:
        if SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
    except ValueError as e:
        return f"error: {e}"

    if not os.path.exists(path):
        return f"error: path does not exist: {path}"

    if shutil.which("rg"):
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]
        if literal_text:
            cmd.append("-F")
        if include:
            cmd.extend(["--glob", str(include)])
        cmd.extend(["--", pat, path])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode in (0, 1):
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            lines = [ln for ln in lines if not _is_ignored_path(ln.split(":", 1)[0])]
            truncated = len(lines) > MAX_GREP_RESULTS
            lines = lines[:MAX_GREP_RESULTS]
            if not lines:
                return "no matches found"
            out = "\n".join(lines)
            if truncated:
                out += "\n\n(results are truncated; refine path or include pattern)"
            return out

    try:
        rx = re.compile(re.escape(pat) if literal_text else pat)
    except re.error as e:
        return f"error: invalid regex: {e}"

    hits: List[str] = []
    scanned_files = 0
    scanned_bytes = 0
    MAX_SCAN_FILES = 2000
    MAX_SCAN_BYTES = 50 * 1024 * 1024  # 50MB
    if os.path.isfile(path):
        file_list = [path]
    else:
        file_list = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORE_DIRS]
            for name in files:
                file_list.append(os.path.join(root, name))

    scan_truncated = False
    for fp in file_list:
        if _is_ignored_path(fp):
            continue
        if include and not fnmatch.fnmatch(os.path.basename(fp), str(include)):
            continue
        if scanned_files >= MAX_SCAN_FILES or scanned_bytes >= MAX_SCAN_BYTES:
            scan_truncated = True
            break
        try:
            fsize = os.path.getsize(fp)
            if fsize > MAX_SINGLE_FILE_SCAN:
                continue
            scanned_bytes += fsize
            scanned_files += 1
            with open(fp, "r", errors="ignore") as f:
                for ln_no, ln in enumerate(f, 1):
                    if rx.search(ln):
                        hits.append(f"{fp}:{ln_no}:{ln.rstrip()}")
                        if len(hits) >= MAX_GREP_RESULTS:
                            break
        except Exception:
            pass
        if len(hits) >= MAX_GREP_RESULTS:
            break

    if not hits:
        if scan_truncated:
            return "no matches found (scan limit reached; install ripgrep for better performance)"
        return "no matches found"
    out = "\n".join(hits)
    if len(hits) >= MAX_GREP_RESULTS:
        out += "\n\n(results are truncated; refine path or include pattern)"
    elif scan_truncated:
        out += "\n\n(scan limit reached; install ripgrep for better performance)"
    return out


def search_fn(args: Any) -> str:
    args, err = _require_args_dict(args, "search")
    if err:
        return err
    pattern = args.get("pattern")
    if not pattern or not isinstance(pattern, str):
        return "error: pattern is required"
    path = args.get("path", ".") or "."
    include = args.get("include")
    literal_text_raw = args.get("literal_text", False)
    if literal_text_raw is not None and not isinstance(literal_text_raw, bool):
        return "error: literal_text must be boolean"
    literal_text = bool(literal_text_raw)

    max_results = args.get("max_results")
    if max_results is None:
        max_results_int = MAX_GREP_RESULTS
    else:
        try:
            max_results_int = int(max_results)
        except (TypeError, ValueError):
            return "error: max_results must be a number"
        if max_results_int <= 0:
            return "error: max_results must be positive"
        max_results_int = min(max_results_int, MAX_GREP_RESULTS)

    try:
        if SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
    except ValueError as e:
        return f"error: {e}"

    if not os.path.exists(path):
        return f"error: path does not exist: {path}"

    if shutil.which("rg"):
        cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]
        if literal_text:
            cmd.append("-F")
        if include:
            cmd.extend(["--glob", str(include)])
        cmd.extend(["--", pattern, path])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode in (0, 1):
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            lines = [ln for ln in lines if not _is_ignored_path(ln.split(":", 1)[0])]
            truncated = len(lines) > max_results_int
            lines = lines[:max_results_int]
            if not lines:
                return "no matches found"
            out = "\n".join(lines)
            if truncated:
                out += "\n\n(results are truncated; refine path or include pattern)"
            return out

    try:
        rx = re.compile(re.escape(pattern) if literal_text else pattern)
    except re.error as e:
        return f"error: invalid regex: {e}"

    hits: List[str] = []
    scanned_files = 0
    scanned_bytes = 0
    MAX_SCAN_FILES = 2000
    MAX_SCAN_BYTES = 50 * 1024 * 1024  # 50MB
    if os.path.isfile(path):
        file_list = [path]
    else:
        file_list = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORE_DIRS]
            for name in files:
                file_list.append(os.path.join(root, name))

    scan_truncated = False
    for fp in file_list:
        if _is_ignored_path(fp):
            continue
        if include and not fnmatch.fnmatch(os.path.basename(fp), str(include)):
            continue
        if scanned_files >= MAX_SCAN_FILES or scanned_bytes >= MAX_SCAN_BYTES:
            scan_truncated = True
            break
        try:
            fsize = os.path.getsize(fp)
            if fsize > MAX_SINGLE_FILE_SCAN:
                continue
            scanned_bytes += fsize
            scanned_files += 1
            with open(fp, "r", errors="ignore") as f:
                for ln_no, ln in enumerate(f, 1):
                    if rx.search(ln):
                        hits.append(f"{fp}:{ln_no}:{ln.rstrip()}")
                        if len(hits) >= max_results_int:
                            break
        except Exception:
            pass
        if len(hits) >= max_results_int:
            break

    if not hits:
        if scan_truncated:
            return "no matches found (scan limit reached; install ripgrep for better performance)"
        return "no matches found"
    out = "\n".join(hits)
    if len(hits) >= max_results_int:
        out += "\n\n(results are truncated; refine path or include pattern)"
    elif scan_truncated:
        out += "\n\n(scan limit reached; install ripgrep for better performance)"
    return out


def ls_fn(args: Any) -> str:
    args, err = _require_args_dict(args, "ls")
    if err:
        return err
    path = args.get("path", ".") or "."
    try:
        # ls on dirs inside sandbox; if sandbox off, allow.
        if SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
        if not os.path.isdir(path):
            return f"error: directory not found: {path}"
        entries = sorted(os.listdir(path))
        return "\n".join(entries) if entries else "(empty directory)"
    except Exception as e:
        return f"error: {e}"


def shell(args: Any) -> str:
    args, err = _require_args_dict(args, "shell")
    if err:
        return _shell_payload(err, 1, 0.0)

    command = args.get("command")
    workdir = args.get("workdir", ".") or "."
    workdir = os.path.abspath(os.path.expanduser(workdir))
    workdir_real = os.path.realpath(workdir)
    timeout_ms = args.get("timeout_ms", DEFAULT_SHELL_TIMEOUT_MS)

    if not command or not isinstance(command, str):
        return _shell_payload("error: command is required and must be a string", 1, 0.0)

    if not os.path.isdir(workdir_real):
        return _shell_payload(
            f"error: workdir does not exist: {workdir} (resolved: {workdir_real})",
            1,
            0.0,
        )

    if SANDBOX_ROOT and not _is_path_within_sandbox(workdir_real, SANDBOX_ROOT):
        return _shell_payload(f"error: workdir '{workdir}' is outside sandbox root '{SANDBOX_ROOT}'", 1, 0.0)

    if TEST_MENTION_RE.search(command):
        return _shell_payload("error: test commands are not allowed; tests run automatically after completion.", 1, 0.0)

    dangerous = _check_dangerous_command(command)
    if dangerous:
        return _shell_payload("error: command blocked by sandbox (matched dangerous pattern)", 1, 0.0)

    if SANDBOX_ROOT:
        if _SHELL_CHAINING_RE.search(command):
            return _shell_payload(
                "error: command contains shell chaining operators (;, &&, ||, `, $(), ../); not allowed in sandbox",
                1, 0.0,
            )
        if _SHELL_CD_RE.search(command):
            return _shell_payload(
                "error: 'cd' is not allowed in sandbox mode; use the workdir parameter instead",
                1, 0.0,
            )
        allowlist_err = _check_sandbox_allowlist(command)
        if allowlist_err:
            return _shell_payload(allowlist_err, 1, 0.0)

    try:
        timeout_ms_int = int(timeout_ms)
    except (TypeError, ValueError):
        return _shell_payload("error: timeout_ms must be a number", 1, 0.0)
    if timeout_ms_int <= 0:
        timeout_ms_int = DEFAULT_SHELL_TIMEOUT_MS
    if timeout_ms_int > MAX_SHELL_TIMEOUT_MS:
        timeout_ms_int = MAX_SHELL_TIMEOUT_MS
    timeout_sec = max(1, int(timeout_ms_int / 1000))

    try:
        cmd_args = shlex.split(command)
    except ValueError as e:
        return _shell_payload(f"error: failed to parse command: {e}", 1, 0.0)

    # Extract leading VAR=val assignments into env dict so they work with shell=False
    env: Optional[Dict[str, str]] = None
    cmd_start = 0
    while cmd_start < len(cmd_args) and _ENV_VAR_ASSIGN_RE.match(cmd_args[cmd_start]):
        cmd_start += 1
    if cmd_start > 0:
        env = dict(os.environ)
        for token in cmd_args[:cmd_start]:
            key, _, val = token.partition("=")
            env[key] = val
        cmd_args = cmd_args[cmd_start:]
    if not cmd_args:
        return _shell_payload("error: command contains only variable assignments, no actual command", 1, 0.0)

    start = time.time()
    try:
        result = subprocess.run(
            cmd_args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=workdir_real,
            env=env,
        )
        dur = round(time.time() - start, 1)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        out = "\n".join(parts) if parts else "(empty output)"
        out = _truncate_shell_output(out)
        return _shell_payload(out, int(result.returncode), dur)
    except subprocess.TimeoutExpired as exc:
        dur = round(time.time() - start, 1)
        out = f"command timed out after {timeout_ms_int} milliseconds"
        return _shell_payload(out, 124, dur, timed_out=True)
    except Exception as e:
        return _shell_payload(f"error: {e}", 1, 0.0)


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
                if SANDBOX_ROOT:
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


def build_tools(
    tool_defs: Dict[str, Dict[str, Any]],
    handlers: Dict[str, Any],
    tool_order: List[str],
) -> ToolsDict:
    tools: ToolsDict = {}
    for name in tool_order:
        tool_def = tool_defs.get(name)
        if not tool_def:
            raise ValueError(f"Missing tool definition for '{name}'")
        description = tool_def.get("description")
        params = tool_def.get("parameters")
        schema: Dict[str, Any] = {}
        if "additionalProperties" in tool_def:
            schema["additionalProperties"] = bool(tool_def.get("additionalProperties"))
        feedback = tool_def.get("feedback") or {}
        if not isinstance(feedback, dict):
            feedback = {}
        handler_name = tool_def.get("handler", name)
        handler = handlers.get(handler_name)
        if description is None or params is None or handler is None:
            raise ValueError(f"Invalid tool definition: {name}")
        tools[name] = (description, params, handler, schema, feedback)
    return tools


def make_openai_tools(
    tools_dict: ToolsDict,
    display_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name, tool_info in tools_dict.items():
        display = display_map.get(name, name) if display_map else name
        desc = render_tool_description(
            tool_info[0],
            display_map,
        )
        params = tool_info[1]
        schema = tool_info[3] if len(tool_info) > 3 else {}
        properties = {}
        required = []
        for pn, pt in (params or {}).items():
            base_type = None
            is_optional = False
            description = None
            default = None
            default_set = False
            min_length = None
            if isinstance(pt, str):
                is_optional = pt.endswith("?")
                base_type = pt.rstrip("?")
            elif isinstance(pt, dict):
                type_val = pt.get("type")
                if not isinstance(type_val, str):
                    continue
                is_optional = bool(pt.get("optional", False))
                if type_val.endswith("?"):
                    is_optional = True
                    type_val = type_val.rstrip("?")
                base_type = type_val
                if isinstance(pt.get("description"), str):
                    description = pt["description"]
                if "default" in pt:
                    default_set = True
                    default = pt["default"]
                if isinstance(pt.get("minLength"), int):
                    min_length = pt["minLength"]
            if not base_type:
                continue
            if base_type == "array":
                prop = {"type": "array"}
                if isinstance(pt, dict) and isinstance(pt.get("items"), dict):
                    prop["items"] = pt["items"]
                elif isinstance(pt, dict) and isinstance(pt.get("items"), str):
                    prop["items"] = {"type": pt["items"]}
                else:
                    prop["items"] = {"type": "string"}
                if description:
                    prop["description"] = description
                properties[pn] = prop
                if not is_optional:
                    required.append(pn)
                continue
            if base_type == "object":
                prop = {"type": "object"}
                if isinstance(pt, dict) and isinstance(pt.get("properties"), dict):
                    prop["properties"] = pt["properties"]
                if isinstance(pt, dict) and "additionalProperties" in pt:
                    prop["additionalProperties"] = pt["additionalProperties"]
                if description:
                    prop["description"] = description
                properties[pn] = prop
                if not is_optional:
                    required.append(pn)
                continue
            if base_type in ("integer", "int"):
                json_type = "integer"
            else:
                json_type = "number" if base_type == "number" else base_type
            prop = {"type": json_type}
            if description:
                prop["description"] = description
            if default_set:
                prop["default"] = default
            if min_length is not None:
                prop["minLength"] = min_length
            if isinstance(pt, dict):
                if isinstance(pt.get("minimum"), (int, float)):
                    prop["minimum"] = pt["minimum"]
                if isinstance(pt.get("maximum"), (int, float)):
                    prop["maximum"] = pt["maximum"]
            properties[pn] = prop
            if not is_optional:
                required.append(pn)
        parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
        }
        if "additionalProperties" in schema:
            parameters["additionalProperties"] = schema["additionalProperties"]
        out.append({
            "type": "function",
            "function": {
                "name": display,
                "description": desc,
                "parameters": parameters,
            }
        })
    return out


def render_tool_description(desc: str, display_map: Optional[Dict[str, str]]) -> str:
    if not desc or not display_map:
        return desc

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return match.group(0)
        return display_map.get(token, token) if token in display_map else match.group(0)

    return re.sub(r"\{\{\s*tool:\s*([^}]+?)\s*\}\}", _replace, desc)


def get_tool_feedback_template(
    tools_dict: ToolsDict,
    tool_name: str,
    reason: str,
) -> Optional[str]:
    tool_info = tools_dict.get(tool_name)
    if not tool_info or len(tool_info) < 5:
        return None
    feedback = tool_info[4]
    if not isinstance(feedback, dict):
        return None
    template = feedback.get(reason)
    return template if isinstance(template, str) else None


def render_feedback_template(
    template: str,
    display_map: Optional[Dict[str, str]],
    values: Optional[Dict[str, Any]] = None,
) -> str:
    rendered = render_tool_description(template, display_map)
    if values:
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def build_feedback_text(
    tools_dict: ToolsDict,
    display_map: Optional[Dict[str, str]],
    resolved_name: str,
    reason: str,
    fallback: str,
    values: Optional[Dict[str, Any]] = None,
) -> str:
    template = get_tool_feedback_template(tools_dict, resolved_name, reason)
    if template:
        return render_feedback_template(
            template,
            display_map,
            values,
        )
    return fallback


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
# Tool-arg repair for number words
# ---------------------------

_NUMBER_WORDS = {
    "zero": 0,
    "a": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

_TOOL_ARG_NUMBER_FIELDS = {
    "read": {"line_start", "line_end", "offset", "limit"},
}


def _parse_number_words(text: str) -> Optional[int]:
    if not text:
        return None
    words = [w for w in re.split(r"\s+", text.strip().lower()) if w and w != "and"]
    if not words:
        return None
    if len(words) == 1:
        return _NUMBER_WORDS.get(words[0])

    if len(words) == 2 and words[1] == "hundred":
        base = _NUMBER_WORDS.get(words[0])
        return None if base is None else base * 100

    if len(words) >= 3 and words[1] == "hundred":
        base = _NUMBER_WORDS.get(words[0])
        if base is None:
            return None
        remainder = " ".join(words[2:])
        rv = _parse_number_words(remainder)
        return None if rv is None else base * 100 + rv

    if len(words) == 2:
        first = _NUMBER_WORDS.get(words[0])
        second = _NUMBER_WORDS.get(words[1])
        return None if (first is None or second is None) else first + second

    return None


def _repair_number_word_args(raw_args: str, fields: set) -> str:
    if not raw_args or not fields:
        return raw_args
    field_pattern = "|".join(re.escape(f) for f in sorted(fields))
    pattern = rf'"({field_pattern})"\s*:\s*([A-Za-z_-]+(?:\s+[A-Za-z_-]+)*)'

    def repl(m: re.Match) -> str:
        v = _parse_number_words(m.group(2))
        if v is not None:
            return f"\"{m.group(1)}\": {v}"
        return m.group(0)

    return re.sub(pattern, repl, raw_args)


def _extract_patch_block(text: str) -> Optional[str]:
    if not text:
        return None
    start = text.find("*** Begin Patch")
    if start < 0:
        return None
    end = text.find("*** End Patch", start)
    if end < 0:
        return None
    end += len("*** End Patch")
    return text[start:end]


def _validate_tool_args(tool_name: str, args: Any, params: Optional[Dict[str, str]]) -> Optional[str]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return f"error: invalid arguments for tool '{tool_name}': expected object"
    if not params:
        return None

    required = []
    type_map: Dict[str, Tuple[str, bool]] = {}
    for key, param_type in params.items():
        base_type = None
        optional = False
        if isinstance(param_type, str):
            optional = param_type.endswith("?")
            base_type = param_type.rstrip("?")
        elif isinstance(param_type, dict):
            type_val = param_type.get("type")
            if isinstance(type_val, str):
                optional = bool(param_type.get("optional", False))
                if type_val.endswith("?"):
                    optional = True
                    type_val = type_val.rstrip("?")
                base_type = type_val
        if not base_type:
            continue
        type_map[key] = (base_type, optional)
        if not optional:
            required.append(key)

    unknown = sorted(set(args.keys()) - set(params.keys()))
    if unknown:
        return f"error: unknown parameter(s) for tool '{tool_name}': {', '.join(unknown)}"

    missing = sorted(set(required) - set(args.keys()))
    if missing:
        return f"error: missing required parameter(s) for tool '{tool_name}': {', '.join(missing)}"

    for key, value in args.items():
        base_type, optional = type_map.get(key, (None, False))
        if value is None:
            if optional:
                continue
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected {base_type}"
        if base_type == "string" and not isinstance(value, str):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected string"
        if base_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected number"
        if base_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected integer"
        if base_type == "boolean" and not isinstance(value, bool):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected boolean"
        if base_type == "array" and not isinstance(value, list):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected array"
        if base_type == "object" and not isinstance(value, dict):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected object"

    return None


def resolve_tool_name(name: str) -> str:
    raw = (name or "").strip()
    if raw:
        raw = raw.splitlines()[0]
    if "<|" in raw:
        raw = raw.split("<|", 1)[0]
    key = raw.strip().lower()
    return TOOL_ALIAS_MAP.get(key, key)


def display_tool_name(name: str) -> str:
    return TOOL_DISPLAY_MAP.get(name, name)


def _append_feedback(
    messages: List[Dict[str, Any]],
    turn: int,
    request_id: Optional[str],
    text: str,
    reason: str,
    attempt: Optional[int] = None,
) -> None:
    messages.append({"role": "user", "content": text})
    log_event("runtime_feedback", {
        "turn": turn,
        "request_id": request_id,
        "reason": reason,
        "attempt": attempt,
        "message": text[:200],
    })


def process_tool_call(tools_dict: ToolsDict, tc: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, str]:
    func = tc.get("function", {}) or {}
    tool_name = func.get("name", "") or ""
    if not tool_name.strip():
        return "", {}, "error: missing tool name", ""

    raw_args = func.get("arguments", "{}")
    resolved = resolve_tool_name(tool_name)

    if isinstance(raw_args, dict):
        tool_args = raw_args
    else:
        original_raw_args = raw_args
        try:
            tool_args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            if resolved == "apply_patch":
                patch = _extract_patch_block(str(raw_args))
                if patch:
                    tool_args = {"patch": patch}
                    log_event("format_repair", {
                        "tool": "apply_patch",
                        "reason": "patch_block_recover",
                    })
                else:
                    repaired = _repair_number_word_args(
                        str(raw_args),
                        _TOOL_ARG_NUMBER_FIELDS.get(resolved, set()),
                    )
                    if repaired != raw_args:
                        try:
                            tool_args = json.loads(repaired)
                        except json.JSONDecodeError:
                            return resolved, {}, f"error: invalid JSON in tool arguments after repair: {exc}. Raw: {str(original_raw_args)[:100]}", tool_name
                    else:
                        return resolved, {}, f"error: invalid JSON in tool arguments: {exc}. Raw: {str(original_raw_args)[:100]}", tool_name
            else:
                repaired = _repair_number_word_args(
                    str(raw_args),
                    _TOOL_ARG_NUMBER_FIELDS.get(resolved, set()),
                )
                if repaired != raw_args:
                    try:
                        tool_args = json.loads(repaired)
                    except json.JSONDecodeError:
                        return resolved, {}, f"error: invalid JSON in tool arguments after repair: {exc}. Raw: {str(original_raw_args)[:100]}", tool_name
                else:
                    return resolved, {}, f"error: invalid JSON in tool arguments: {exc}. Raw: {str(original_raw_args)[:100]}", tool_name

    unsupported_key = tool_name.strip().lower()
    unsupported_resolved = resolve_tool_name(tool_name)
    if unsupported_key in UNSUPPORTED_TOOLS:
        return resolved, tool_args, UNSUPPORTED_TOOLS[unsupported_key], tool_name
    if unsupported_resolved in UNSUPPORTED_TOOLS:
        return resolved, tool_args, UNSUPPORTED_TOOLS[unsupported_resolved], tool_name

    if resolved not in tools_dict:
        return resolved, tool_args, f"error: unknown tool '{tool_name}'", tool_name

    # Post-parse repair for string number fields
    if resolved in _TOOL_ARG_NUMBER_FIELDS:
        for field in _TOOL_ARG_NUMBER_FIELDS[resolved]:
            if isinstance(tool_args.get(field), str):
                v = _parse_number_words(tool_args[field])
                if v is not None:
                    tool_args[field] = v

    params = tools_dict[resolved][1]
    validation_error = _validate_tool_args(resolved, tool_args, params)
    if validation_error:
        return resolved, tool_args, validation_error, tool_name

    try:
        result = tools_dict[resolved][2](tool_args)
    except Exception as err:
        result = f"error: {err}"

    return resolved, tool_args, result, tool_name


def is_tool_error(tool_name: str, result: Any) -> bool:
    if not isinstance(result, str):
        return False
    if result.startswith("error:"):
        return True
    if tool_name == "shell":
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        exit_code = (payload.get("metadata") or {}).get("exit_code")
        return isinstance(exit_code, int) and exit_code != 0
    return False


def extract_patch_file(patch_text: str) -> Optional[str]:
    if not patch_text:
        return None
    match = _PATCH_FILE_RE.search(patch_text)
    if not match:
        return None
    return match.group(1).strip()


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
    log_event("request", {
        "tools": list(tools_dict.keys()),
        "tools_display": [TOOL_DISPLAY_MAP.get(n, n) for n in tools_dict.keys()],
        "message_summary": summarize_messages(full_messages),
        "request_params": {k: v for k, v in request_data.items() if k not in ("messages", "tools")},
        "inference_params_full": all_inference_params,
    })

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(request_data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        raw = resp.read()
    except Exception as exc:
        log_event("request_error", {"error": str(exc)})
        return {"error": f"request failed: {exc}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:200].decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)[:200]
        log_event("response_error", {"error": str(exc), "raw_preview": preview})
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
    log_event("response_meta", meta)
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
                    if SANDBOX_ROOT and not _is_path_within_sandbox(os.path.realpath(c), SANDBOX_ROOT):
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


def run_agent(
    prompt: str,
    system_prompt: str,
    tools_dict: ToolsDict,
    agent_settings: Dict[str, Any],
    previous_messages: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    global LAST_RUN_SUMMARY, CURRENT_MESSAGES
    LAST_RUN_SUMMARY = None

    messages = (previous_messages or []) + [{"role": "user", "content": prompt}]
    CURRENT_MESSAGES = messages  # Keep global reference for tools like plan_solution
    turns = 0
    last_request_id: Optional[str] = None

    tool_calls_total = 0
    tool_errors_total = 0
    tool_call_counts: Dict[str, int] = {}
    tool_error_counts: Dict[str, int] = {}
    feedback_counts: Counter[str] = Counter()
    patch_fail_count: Dict[str, int] = {}
    task_header_printed = False

    format_retries = 0
    analysis_retries = 0

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
        if turns > MAX_TURNS * 3:
            # Hard cap: prevent infinite loops even with format retries
            log_event("agent_abort", {"reason": "hard_turn_limit", "turns": turns})
            return "error: hard turn limit reached", messages
        if (turns - format_retry_turns) > MAX_TURNS:
            summary = {
                "tool_calls_total": tool_calls_total,
                "tool_errors_total": tool_errors_total,
                "tool_call_counts": tool_call_counts,
                "tool_error_counts": tool_error_counts,
                "analysis_retries": analysis_retries,
            }
            LAST_RUN_SUMMARY = summary
            log_event("agent_abort", {"reason": "max_turns", "turns": turns, **summary})
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
            log_event("forced_tool_choice", {"turn": turns, "tool": enforced_tool_choice})

        tool_choice_required = is_tool_choice_required(current_overrides.get("tool_choice")) or base_tool_choice_required

        response = call_api(request_messages, system_prompt, tools_dict, current_overrides)
        last_request_id = response.get("request_id")

        if response.get("error"):
            log_event("api_error", {"turn": turns, "error": response["error"]})
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
            log_event("analysis_artifact_normalized", {"turn": turns, "original_len": len(raw_content)})
            analysis_retries += 1
        tool_calls = message.get("tool_calls", []) or []
        thinking = message.get("thinking")
        if not thinking:
            thinking = message.get("reasoning_content")

        if thinking and native_thinking:
            t = str(thinking).strip()
            if t:
                log_event("thinking_captured", {"turn": turns, "chars": len(t)})

        tool_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
        resolved_tool_names = [resolve_tool_name(n) for n in tool_names]
        tool_call_ids = [tc.get("id", "") for tc in tool_calls]
        log_event("response", {
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
                    log_event("format_retry", {
                        "turn": turns,
                        "reason": "forced_tool_choice_mismatch",
                        "expected_tool": enforced_tool_choice,
                        "actual_tools": resolved_tool_names,
                    })
                    format_retry_turns += 1
                    continue
                return "error: forced tool choice mismatch", messages
            forced_tool_choice = None

        if tool_calls:
            tool_calls_total += len(tool_calls)

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
                    log_event("format_retry", {"turn": turns, "reason": "analysis_only_no_tool_calls"})
                    format_retry_turns += 1
                    continue
                # Exhausted retries — return error, never treat analysis as final content
                log_event("analysis_only_exhausted", {"turn": turns, "retries": format_retries})
                return "error: analysis-only output after retries exhausted", messages

            if min_tool_calls and tool_calls_total < min_tool_calls:
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
                    log_event("format_retry", {"turn": turns, "reason": "min_tool_calls_not_met"})
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
                        if is_tool_error(resolved_name, result):
                            tool_errors_total += 1
                        elif is_write_tool(resolved_name):
                            if _did_tool_make_change(resolved_name, result):
                                code_change_made = True
                        messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
                        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": display_name, "content": result})
                        tool_calls_total += 1
                        log_event("forced_tool_call", {"turn": turns, "tool": resolved_name, "tool_display": display_name})
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
                    log_event("format_retry", {"turn": turns, "reason": "code_change_required"})
                    format_retry_turns += 1
                    continue
                return "error: code change required", messages

            # Final content
            if content:
                print(f"\n{CYAN}⏺{RESET} {content}")

            messages.append(message)
            summary = {
                "tool_calls_total": tool_calls_total,
                "tool_errors_total": tool_errors_total,
                "tool_call_counts": tool_call_counts,
                "tool_error_counts": tool_error_counts,
                "analysis_retries": analysis_retries,
            }
            LAST_RUN_SUMMARY = summary
            log_event("agent_done", {"turns": turns, **summary, "message_summary": summarize_messages(messages)})
            # Dump full conversation to separate files
            if LOG_PATH:
                base_path = LOG_PATH.rsplit(".", 1)[0]
                full_conv = [{"role": "system", "content": system_prompt}] + list(messages)
                # 1) Raw JSON (.raw.json)
                raw_path = base_path + ".raw.json"
                try:
                    with open(raw_path, "w", encoding="utf-8") as rf:
                        json.dump(full_conv, rf, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                # 2) Pretty human-readable (.log)
                pretty_path = base_path + ".log"
                try:
                    with open(pretty_path, "w", encoding="utf-8") as cf:
                        for i, msg in enumerate(full_conv):
                            role = msg.get("role", "?")
                            cf.write(f"{'='*60}\n")
                            cf.write(f"[{i}] {role.upper()}")
                            if msg.get("tool_call_id"):
                                cf.write(f"  (tool_call_id: {msg['tool_call_id']})")
                            cf.write(f"\n{'='*60}\n\n")
                            for tk in ("thinking", "reasoning_content"):
                                if msg.get(tk):
                                    cf.write(f"--- THINKING ---\n{msg[tk]}\n--- /THINKING ---\n\n")
                            content_val = msg.get("content")
                            if content_val:
                                cf.write(f"{content_val}\n\n")
                            for tc in msg.get("tool_calls") or []:
                                fn = tc.get("function", {})
                                cf.write(f">>> TOOL CALL: {fn.get('name', '?')}  (id: {tc.get('id', '?')})\n")
                                args_str = fn.get("arguments", "")
                                try:
                                    args_obj = json.loads(args_str) if isinstance(args_str, str) else args_str
                                    cf.write(json.dumps(args_obj, indent=2, ensure_ascii=False))
                                except (json.JSONDecodeError, TypeError):
                                    cf.write(str(args_str))
                                cf.write(f"\n\n")
                        cf.write(f"{'='*60}\nEND ({len(full_conv)} messages)\n{'='*60}\n")
                except Exception:
                    pass
                log_event("conversation_saved", {"raw": raw_path, "pretty": pretty_path, "message_count": len(full_conv)})
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
            resolved_name, tool_args, result, response_name = process_tool_call(tools_dict, tc)
            tool_call_counts[resolved_name] = tool_call_counts.get(resolved_name, 0) + 1
            tc_display_name = (tc.get("function") or {}).get("name") or ""

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

            if is_tool_error(resolved_name, result):
                tool_errors_total += 1
                tool_error_counts[resolved_name] = tool_error_counts.get(resolved_name, 0) + 1
                result_text = result if isinstance(result, str) else ""
                if resolved_name == "apply_patch" and "patch context not found" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to patch: {path_value}"
                        patch_fail_count[path_value] = patch_fail_count.get(path_value, 0) + 1
                        fail_count = patch_fail_count[path_value]
                    else:
                        target = "the file named in the patch header line: '*** Update File: <path>'"
                        fail_count = 0
                    patch_tool = display_tool_name("apply_patch")
                    read_tool = display_tool_name("read")
                    edit_tool = display_tool_name("edit")
                    write_tool = display_tool_name("write")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "patch_context_not_found",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: patch context not found.\n"
                            f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {patch_tool} using the CURRENT content with exact context lines.\n"
                            "Do NOT repeat the same patch."
                        ),
                        {"target": target},
                    )
                    if fail_count >= 2:
                        feedback_text += (
                            f"\nSECOND FAILURE on same file ({path_value}): "
                            f"STOP patching; re-read and switch to {edit_tool} or {write_tool}."
                        )
                    feedback_reason = "patch_context_not_found"
                elif resolved_name == "apply_patch" and "patch context not unique" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to patch: {path_value}"
                    else:
                        target = "the file named in the patch header line: '*** Update File: <path>'"
                    patch_tool = display_tool_name("apply_patch")
                    read_tool = display_tool_name("read")
                    edit_tool = display_tool_name("edit")
                    write_tool = display_tool_name("write")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "patch_context_not_unique",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: patch context not unique.\n"
                            f"ACTION: Call {read_tool}(path) for {target}, then retry {patch_tool} with MORE unique context lines, "
                            f"OR switch to {edit_tool} / {write_tool} if the file is small."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "patch_context_not_unique"
                elif resolved_name == "apply_patch" and "must read" in result_text and "before patching" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to patch: {path_value}"
                    else:
                        target = "the file named in the patch header line: '*** Update File: <path>'"
                    patch_tool = display_tool_name("apply_patch")
                    read_tool = display_tool_name("read")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "must_read_before_patching",
                        (
                            f"FORMAT ERROR: {patch_tool} requires the file to be read first.\n"
                            f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {patch_tool}."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "must_read_before_patching"
                elif resolved_name == "apply_patch" and "invalid patch format" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "invalid_patch_format",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: invalid patch format.\n"
                            f"ACTION: Provide a COMPLETE patch with *** Begin Patch and *** End Patch markers and valid context lines. "
                            f"Re-read the target file and retry {patch_tool}."
                        ),
                    )
                    feedback_reason = "invalid_patch_format"
                elif resolved_name == "apply_patch" and "unexpected patch line" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "unexpected_patch_line",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: unexpected patch line.\n"
                            f"ACTION: Ensure each line starts with ' ', '+', or '-' and include a valid @@ context header. "
                            f"Re-read the target file and retry {patch_tool}."
                        ),
                    )
                    feedback_reason = "unexpected_patch_line"
                elif resolved_name == "apply_patch" and "invalid add line" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "invalid_add_line",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: invalid add line.\n"
                            f"ACTION: Lines being added must start with '+'. "
                            f"Re-read the target file and retry {patch_tool}."
                        ),
                    )
                    feedback_reason = "invalid_add_line"
                elif resolved_name == "edit" and "must read" in result_text and "before editing" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to edit: {path_value}"
                    else:
                        target = "the SAME path you attempted to edit (use the 'path' argument from your edit tool call)"
                    edit_tool = display_tool_name("edit")
                    read_tool = display_tool_name("read")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "must_read_before_editing",
                        (
                            f"FORMAT ERROR: {edit_tool} requires the file to be read first.\n"
                            f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {edit_tool}."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "must_read_before_editing"
                elif resolved_name == "edit" and "old_string not found" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to edit: {path_value}"
                    else:
                        target = "the SAME path you attempted to edit (use the 'path' argument from your edit tool call)"
                    edit_tool = display_tool_name("edit")
                    read_tool = display_tool_name("read")
                    patch_tool = display_tool_name("apply_patch")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "old_string_not_found",
                        (
                            f"FORMAT ERROR: {edit_tool} failed: old_string not found.\n"
                            f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry with an EXACT substring (including whitespace), "
                            f"OR switch to {patch_tool} with exact context."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "old_string_not_found"
                elif resolved_name == "edit" and "must be unique" in result_text and "all=true" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to edit: {path_value}"
                    else:
                        target = "the SAME path you attempted to edit (use the 'path' argument from your edit tool call)"
                    edit_tool = display_tool_name("edit")
                    read_tool = display_tool_name("read")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "old_string_not_unique",
                        (
                            f"FORMAT ERROR: {edit_tool} failed: old_string is not unique.\n"
                            f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry with an exact unique substring, "
                            f"OR set all=true if you intend to replace all occurrences."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "old_string_not_unique"
                elif resolved_name == "edit" and "no changes" in result_text and "old_string equals new_string" in result_text:
                    if path_value:
                        target = f"the SAME path you attempted to edit: {path_value}"
                    else:
                        target = "the SAME path you attempted to edit (use the 'path' argument from your edit tool call)"
                    edit_tool = display_tool_name("edit")
                    read_tool = display_tool_name("read")
                    write_tool = display_tool_name("write")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "old_equals_new",
                        (
                            f"ERROR: {edit_tool} called with old='...' identical to new='...' - no change would occur.\n"
                            f"This usually means you want to MODIFY the code, not copy it unchanged.\n"
                            f"ACTION:\n"
                            f"1. Re-read the file with {read_tool}({target})\n"
                            f"2. Identify the EXACT text you want to CHANGE (old)\n"
                            f"3. Write the MODIFIED version (new) - it must be DIFFERENT from old\n"
                            f"4. If the file already has correct content, the task may be complete - verify and move on.\n"
                            f"TIP: For small files, consider using {write_tool} to rewrite the entire file."
                        ),
                        {"target": target},
                    )
                    feedback_reason = "old_equals_new"
                elif resolved_name == "apply_patch" and "no changes" in result_text and "no-op" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    read_tool = display_tool_name("read")
                    edit_tool = display_tool_name("edit")
                    write_tool = display_tool_name("write")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "patch_noop",
                        (
                            f"FORMAT ERROR: {patch_tool} applied but made NO changes to the file (no-op).\n"
                            f"The file content is identical before and after your patch.\n"
                            f"ACTION:\n"
                            f"1. Call {read_tool}(path) to see current content\n"
                            f"2. Create a NEW {patch_tool} that actually changes content\n"
                            f"3. Or switch to {edit_tool}/{write_tool}\n"
                            f"Do NOT repeat the same patch."
                        ),
                    )
                    feedback_reason = "patch_noop"
                elif resolved_name == "apply_patch" and "repeated patch detected" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    read_tool = display_tool_name("read")
                    edit_tool = display_tool_name("edit")
                    write_tool = display_tool_name("write")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "patch_repeated",
                        (
                            f"FORMAT ERROR: You submitted the exact same patch text again.\n"
                            f"This will loop forever.\n"
                            f"ACTION: {read_tool}(path), then create a DIFFERENT {patch_tool} "
                            f"or use {edit_tool}/{write_tool}."
                        ),
                    )
                    feedback_reason = "patch_repeated"
                elif resolved_name in ("write", "write_file") and "no changes" in result_text:
                    write_tool = display_tool_name("write")
                    read_tool = display_tool_name("read")
                    edit_tool = display_tool_name("edit")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "write_noop",
                        (
                            f"FORMAT ERROR: {write_tool} wrote identical content (no-op).\n"
                            f"ACTION: {read_tool}(path) then {write_tool} with DIFFERENT content, "
                            f"or use {edit_tool} to change a specific part."
                        ),
                    )
                    feedback_reason = "write_noop"
                elif resolved_name == "read" and "Is a directory" in result_text:
                    read_tool = display_tool_name("read")
                    ls_tool = display_tool_name("ls")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "read_is_directory",
                        (
                            f"FORMAT ERROR: {read_tool} failed: path is a directory.\n"
                            f"ACTION: Use {ls_tool}(path) to list files, then call {read_tool} on a file path."
                        ),
                    )
                    feedback_reason = "read_is_directory"
                elif resolved_name == "read" and "File not found" in result_text:
                    read_tool = display_tool_name("read")
                    ls_tool = display_tool_name("ls")
                    glob_tool = display_tool_name("glob")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "read_file_not_found",
                        (
                            f"FORMAT ERROR: {read_tool} failed: file not found.\n"
                            f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to locate the correct file, then call {read_tool} with the valid path."
                        ),
                    )
                    feedback_reason = "read_file_not_found"
                elif resolved_name in {"search", "grep"} and "invalid regex" in result_text:
                    search_tool = display_tool_name(resolved_name)
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "invalid_regex",
                        (
                            f"FORMAT ERROR: {search_tool} failed: invalid regex.\n"
                            f"ACTION: If you want literal text, set literal_text=true. Otherwise escape regex metacharacters and retry {search_tool}."
                        ),
                    )
                    feedback_reason = "invalid_regex"
                elif resolved_name in {"search", "grep"} and "path does not exist" in result_text:
                    search_tool = display_tool_name(resolved_name)
                    ls_tool = display_tool_name("ls")
                    glob_tool = display_tool_name("glob")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "search_path_missing",
                        (
                            f"FORMAT ERROR: {search_tool} failed: path does not exist.\n"
                            f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to find the correct path, then retry {search_tool}."
                        ),
                    )
                    feedback_reason = "search_path_missing"
                elif resolved_name == "apply_patch" and "File not found" in result_text:
                    patch_tool = display_tool_name("apply_patch")
                    ls_tool = display_tool_name("ls")
                    glob_tool = display_tool_name("glob")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "patch_file_not_found",
                        (
                            f"FORMAT ERROR: {patch_tool} failed: file not found in patch header.\n"
                            f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to locate the correct file path, then retry {patch_tool} with the correct '*** Update File:' path."
                        ),
                    )
                    feedback_reason = "patch_file_not_found"
                elif resolved_name == "ls" and ("directory not found" in result_text or "File not found" in result_text):
                    ls_tool = display_tool_name("ls")
                    glob_tool = display_tool_name("glob")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "ls_path_missing",
                        (
                            f"FORMAT ERROR: {ls_tool} failed: path does not exist.\n"
                            f"ACTION: Use {ls_tool} with a valid path (e.g. '.') or use {glob_tool}(pat, path) to discover files."
                        ),
                    )
                    feedback_reason = "ls_path_missing"
                elif resolved_name == "glob" and "path does not exist" in result_text:
                    glob_tool = display_tool_name("glob")
                    ls_tool = display_tool_name("ls")
                    feedback_text = build_feedback_text(
                        tools_dict,
                        TOOL_DISPLAY_MAP,
                        resolved_name,
                        "glob_path_missing",
                        (
                            f"FORMAT ERROR: {glob_tool} failed: path does not exist.\n"
                            f"ACTION: Use {ls_tool}(path) to verify directories, then retry {glob_tool} with a valid path."
                        ),
                    )
                    feedback_reason = "glob_path_missing"
            elif resolved_name in ("apply_patch", "patch_files") and path_value:
                patch_fail_count.pop(path_value, None)
                if _did_tool_make_change(resolved_name, result):
                    code_change_made = True
            elif is_write_tool(resolved_name):
                if _did_tool_make_change(resolved_name, result):
                    code_change_made = True
            log_event("tool_result", {
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
                feedback_counts[feedback_reason] += 1
                attempt_num = feedback_counts[feedback_reason]
                lines = feedback_text.splitlines()
                if lines:
                    lines[0] = f"{lines[0]} (attempt {attempt_num})"
                    feedback_text = "\n".join(lines)
            _append_feedback(
                messages,
                turns,
                last_request_id,
                feedback_text,
                feedback_reason or "tool_error_feedback",
                attempt=attempt_num,
            )
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

    log_event("run_start", {
        "prompt_len": len(prompt or ""),
        "prompt_preview": (prompt or "")[:200],
        "continue_session": continue_mode,
        "previous_message_count": len(previous) if previous else 0,
    })

    content, messages = run_agent(prompt, system_prompt, tools_dict, AGENT_SETTINGS, previous)

    save_session(AGENT_NAME or "agent", messages, MODEL)

    global LAST_RUN_SUMMARY
    log_event("run_end", LAST_RUN_SUMMARY or {})


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
    TOOL_DISPLAY_MAP.clear()
    TOOL_DISPLAY_MAP.update(display_map)
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
