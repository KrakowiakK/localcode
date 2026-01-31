"""
File writing tool handlers: write(), edit().
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers._state import (
    FILE_VERSIONS,
    _NOOP_COUNTS,
    _require_args_dict,
    _track_file_version,
)
from localcode.tool_handlers._path import _should_block_test_edit, _validate_path


def write(args: Any) -> str:
    args, err = _require_args_dict(args, "write")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=False)
    except ValueError as e:
        return f"error: {e}"
    if _should_block_test_edit(path):
        return f"error: test file edits are blocked in benchmark mode ({path})"

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
    if _should_block_test_edit(path):
        return f"error: test file edits are blocked in benchmark mode ({path})"

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
