"""
Shell command tool handler: shell() and helpers.
"""

import json
import os
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers import _state
from localcode.tool_handlers._state import (
    DEFAULT_SHELL_TIMEOUT_MS,
    MAX_SHELL_OUTPUT_CHARS,
    MAX_SHELL_TIMEOUT_MS,
    _require_args_dict,
)
from localcode.tool_handlers._path import _is_path_within_sandbox, to_display_path
from localcode.tool_handlers._sandbox import (
    TEST_MENTION_RE,
    _ENV_VAR_ASSIGN_RE,
    _SHELL_CD_RE,
    _SHELL_CHAINING_RE,
    _check_dangerous_command,
    _check_sandbox_allowlist,
)


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


def shell(args: Any) -> str:
    args, err = _require_args_dict(args, "shell")
    if err:
        return _shell_payload(err, 1, 0.0)

    command = args.get("command")
    workdir = args.get("workdir", ".") or "."
    workdir = os.path.abspath(os.path.expanduser(workdir))
    workdir_real = os.path.realpath(workdir)
    display_workdir = to_display_path(workdir_real)
    timeout_ms = args.get("timeout_ms", DEFAULT_SHELL_TIMEOUT_MS)

    if not command or not isinstance(command, str):
        return _shell_payload("error: command is required and must be a string", 1, 0.0)

    if not os.path.isdir(workdir_real):
        return _shell_payload(
            f"error: workdir does not exist: {display_workdir}",
            1,
            0.0,
        )

    if _state.SANDBOX_ROOT and not _is_path_within_sandbox(workdir_real, _state.SANDBOX_ROOT):
        return _shell_payload(f"error: workdir '{display_workdir}' is outside sandbox root", 1, 0.0)

    if TEST_MENTION_RE.search(command):
        return _shell_payload("error: test commands are not allowed; tests run automatically after completion.", 1, 0.0)

    dangerous = _check_dangerous_command(command)
    if dangerous:
        return _shell_payload("error: command blocked by sandbox (matched dangerous pattern)", 1, 0.0)

    if _state.SANDBOX_ROOT:
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
    except FileNotFoundError:
        cmd_name = cmd_args[0] if cmd_args else "command"
        return _shell_payload(f"error: command not found: {cmd_name}", 1, 0.0)
    except PermissionError:
        return _shell_payload("error: permission denied while executing command", 1, 0.0)
    except Exception:
        return _shell_payload("error: failed to execute command", 1, 0.0)
