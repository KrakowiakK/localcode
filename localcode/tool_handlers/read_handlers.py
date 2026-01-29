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
from localcode.tool_handlers._path import _validate_path


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
