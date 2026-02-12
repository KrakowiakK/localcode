"""
Search tool handlers: glob_fn(), grep_fn(), search_fn(), ls_fn().
"""

import fnmatch
import glob as globlib
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers import _state
from localcode.tool_handlers._state import (
    DEFAULT_IGNORE_DIRS,
    MAX_GLOB_RESULTS,
    MAX_GREP_RESULTS,
    MAX_SINGLE_FILE_SCAN,
    _require_args_dict,
)
from localcode.tool_handlers._path import _is_ignored_path, _validate_path, to_display_path


_HIT_RE = re.compile(r"^(.*?):([0-9]+):(.*)$")


def _extract_hit_path(line: str) -> str:
    match = _HIT_RE.match(line)
    if match:
        return match.group(1)
    return line.split(":", 1)[0]


def _render_hit_line(line: str) -> str:
    match = _HIT_RE.match(line)
    if not match:
        return line
    return f"{to_display_path(match.group(1))}:{match.group(2)}:{match.group(3)}"


def _tool_hints_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_TOOL_HINTS", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


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
    display_path = to_display_path(path)

    if not os.path.isdir(path):
        return f"error: path does not exist: {display_path}"

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
            display_files = [to_display_path(f) for f in files]
            out = "\n".join(display_files)
            if truncated:
                out += "\n\n(results are truncated; refine path or pattern)"
            if _tool_hints_enabled():
                out += _glob_next_step_hint(display_files)
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
    display_files = [to_display_path(f) for f in files]
    out = "\n".join(display_files)
    if truncated:
        out += "\n\n(results are truncated; refine path or pattern)"
    if _tool_hints_enabled():
        out += _glob_next_step_hint(display_files)
    return out


def _glob_next_step_hint(files: list) -> str:
    """Return an optional neutral hint after file discovery."""
    spec_files = [f for f in files if f.endswith(('.spec.js', '.test.js'))]
    if spec_files:
        return f"\n\nHint: tests were found (e.g. {spec_files[0]}). Read relevant files as needed."
    return ""


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
        if _state.SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
    except ValueError as e:
        return f"error: {e}"
    display_path = to_display_path(path)

    if not os.path.exists(path):
        return f"error: path does not exist: {display_path}"

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
            lines = [ln for ln in lines if not _is_ignored_path(_extract_hit_path(ln))]
            lines = [_render_hit_line(ln) for ln in lines]
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
                        hits.append(f"{to_display_path(fp)}:{ln_no}:{ln.rstrip()}")
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
        if _state.SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
    except ValueError as e:
        return f"error: {e}"
    display_path = to_display_path(path)

    if not os.path.exists(path):
        return f"error: path does not exist: {display_path}"

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
            lines = [ln for ln in lines if not _is_ignored_path(_extract_hit_path(ln))]
            lines = [_render_hit_line(ln) for ln in lines]
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
                        hits.append(f"{to_display_path(fp)}:{ln_no}:{ln.rstrip()}")
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
        if _state.SANDBOX_ROOT:
            path = _validate_path(path, check_exists=True)
        else:
            path = os.path.abspath(path)
    except ValueError as e:
        return f"error: {e}"

    display_path = to_display_path(path)
    if not os.path.isdir(path):
        return f"error: directory not found: {display_path}"

    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return f"error: permission denied: {display_path}"
    except OSError:
        return f"error: cannot list directory: {display_path}"
    return "\n".join(entries) if entries else "(empty directory)"
