"""
File reading tool handlers: read(), batch_read().
"""

import difflib
import os
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers._state import (
    DEFAULT_READ_LIMIT,
    FILE_VERSIONS,
    MAX_FILE_SIZE,
    MAX_LINE_LENGTH,
    _LAST_PATCH_HASH,
    _require_args_dict,
    _track_file_version,
)
from localcode.tool_handlers._path import _find_file_in_sandbox, _validate_path


def _tool_hints_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_TOOL_HINTS", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def read(args: Any) -> str:
    args, err = _require_args_dict(args, "read")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=True)
    except ValueError as e:
        # Path auto-correction: try finding file by name in sandbox
        original_path = args.get("path", "")
        filename = os.path.basename(original_path) if original_path else ""
        corrected = _find_file_in_sandbox(filename) if filename else None
        if corrected:
            try:
                path = _validate_path(corrected, check_exists=True)
            except ValueError:
                return f"error: {e}"
        else:
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
            return f"File already fully read ({total_lines} lines). No more content. Proceed with your implementation."

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
            return f"File already fully read ({total_lines} lines). No more content. Proceed with your implementation."
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

    note = _context_note_after_read(path, content)
    if note:
        output += f"\n\n{note}"

    # Optional hinting (off by default to avoid steering model workflow).
    if _tool_hints_enabled():
        hint = _next_step_hint_after_read(path)
        if hint:
            output += f"\n\n{hint}"

    return output


def _next_step_hint_after_read(path: str) -> str:
    """Return an optional neutral hint based on what file was read."""
    basename = os.path.basename(path)
    is_spec = basename.endswith(('.spec.js', '.test.js'))
    is_source = basename.endswith('.js') and not is_spec

    if is_spec:
        source_name = basename.replace('.spec.js', '.js').replace('.test.js', '.js')
        return f"Hint: test expectations are visible. If needed, inspect {source_name} before editing."

    if is_source:
        from localcode.tool_handlers._state import WRITTEN_PATHS
        source_written = any(
            p.endswith('.js') and not os.path.basename(p).endswith(('.spec.js', '.test.js'))
            for p in WRITTEN_PATHS
        )
        if source_written:
            return "Hint: if implementation looks correct, finish; otherwise apply a targeted change."
        else:
            return "Hint: choose any suitable write tool to implement required behavior."

    return ""


def _context_note_after_read(path: str, content: str) -> str:
    """Return lightweight context notes that may improve model decisions."""
    basename = os.path.basename(path)
    if not basename.endswith(".js"):
        return ""
    if basename.endswith((".spec.js", ".test.js")):
        return ""

    if "Remove this statement and implement this function" not in content:
        return ""

    stem = basename[:-3]
    parent = os.path.dirname(path) or "."
    candidates = [f"{stem}.spec.js", f"{stem}.test.js"]
    for cand in candidates:
        cand_path = os.path.join(parent, cand)
        if os.path.exists(cand_path):
            return f"Note: companion test file exists: {cand}. Read it if behavior details are unclear."
    return ""


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
