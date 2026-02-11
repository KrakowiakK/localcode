"""
File writing tool handlers: write(), edit().
"""

import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# === EDIT STRATEGY CONFIG ===
# V1: stealth (all "ok"), V2: rich_noop, V3: rich_noop_short,
# V4: rich_notfound, V5: rich_all, V6: rich_notfound_cap
# V7: hybrid (rich noop + rich notfound + stealth cap)
_EDIT_STRATEGY = "V7"

from localcode.tool_handlers import _state as _state_mod
from localcode.tool_handlers._state import (
    FILE_VERSIONS,
    WRITTEN_PATHS,
    _NOOP_COUNTS,
    _require_args_dict,
    _track_file_version,
)
from localcode.tool_handlers._path import _find_file_in_sandbox, _should_block_test_edit, _validate_path


def _tool_hints_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_TOOL_HINTS", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _inject_tests_on_write_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_INJECT_TESTS_ON_WRITE", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _find_and_read_spec() -> str:
    """Find and read spec/test file in sandbox if model hasn't read it yet."""
    sandbox = _state_mod.SANDBOX_ROOT
    if not sandbox:
        return ""
    # Check if any spec file has already been read
    for tracked_path in FILE_VERSIONS:
        if tracked_path.endswith(('.spec.js', '.test.js')):
            return ""  # Spec already read, no injection needed

    # Find spec file in sandbox
    spec_path = None
    for root, _dirs, files in os.walk(sandbox):
        for f in files:
            if f.endswith('.spec.js') or f.endswith('.test.js'):
                spec_path = os.path.join(root, f)
                break
        if spec_path:
            break

    if not spec_path:
        return ""

    try:
        with open(spec_path, 'r', encoding='utf-8') as fh:
            content = fh.read()
    except Exception:
        return ""

    # Track in FILE_VERSIONS so model can reference the path later
    _track_file_version(spec_path, content)

    # Format with line numbers
    lines = content.splitlines(keepends=True)
    out_parts = []
    for i, line in enumerate(lines):
        out_parts.append(f"{i + 1:4}| {line}")

    return f"\n\nYou have not read the test file. Here are the tests:\n=== {spec_path} ===\n{''.join(out_parts)}"


def _js_syntax_ok(code: str) -> bool:
    """Check if code is valid JavaScript/ESM syntax using node -c."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mjs")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        result = subprocess.run(
            ["node", "-c", tmp_path],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return True  # If check fails, assume valid (don't block)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def write(args: Any) -> str:
    args, err = _require_args_dict(args, "write")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=False)
    except ValueError as e:
        # Path auto-correction: find existing file by name in sandbox
        original_path = args.get("path", "")
        filename = os.path.basename(original_path) if original_path else ""
        corrected = _find_file_in_sandbox(filename) if filename else None
        if corrected:
            try:
                path = _validate_path(corrected, check_exists=False)
            except ValueError:
                return f"error: {e}"
        else:
            return f"error: {e}"
    if _should_block_test_edit(path):
        basename = os.path.basename(path)
        return f"error: cannot write to {basename}; test files are read-only. Use write_file on your source code file only."

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
                # First noop: keep as success so model can finish when file is already correct.
                return (
                    "ok: no changes - file already has this content.\n"
                    "Action: use different content if a fix is still needed, or call finish if this is correct.\n"
                    "Tip: call read(path) if you need to inspect current file content."
                )
            return (
                f"error: repeated no-op write for {os.path.basename(path)}. "
                "Write different content, or call finish if implementation is already correct."
            )

    parent_dir = os.path.dirname(path)
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    _track_file_version(path, content)
    WRITTEN_PATHS.add(path)

    # Clear noop count on real change
    if path in _NOOP_COUNTS:
        _NOOP_COUNTS[path].pop("write", None)

    # Optional test injection for weak models (off by default).
    spec_inject = _find_and_read_spec() if _inject_tests_on_write_enabled() else ""
    write_hint = ""
    if _tool_hints_enabled():
        write_hint = "\nHint: optionally read the file to verify, then continue or finish."

    if is_new_file:
        additions = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"ok: created {path}, +{additions} lines{spec_inject}{write_hint}"

    old_lines = old_content.count("\n")
    new_lines = content.count("\n")
    additions = max(0, new_lines - old_lines)
    removals = max(0, old_lines - new_lines)
    return f"ok: updated {path}, +{additions} -{removals} lines{spec_inject}{write_hint}"


def edit(args: Any) -> str:
    args, err = _require_args_dict(args, "edit")
    if err:
        return err
    try:
        path = _validate_path(args.get("path"), check_exists=True)
    except ValueError as e:
        # Path auto-correction: find existing file by name in sandbox
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
    if _should_block_test_edit(path):
        basename = os.path.basename(path)
        return f"error: cannot edit {basename}; test files are read-only. Use replace_in_file on your source code file only."

    old = args.get("old")
    new = args.get("new")

    # Graceful fallback: missing old/new → return file content so model can make targeted edit
    if old is None or new is None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            _track_file_version(path, text)
            return (
                "error: missing required parameters for edit; provide both 'old' and 'new'.\n"
                f"Current file content:\n{text}"
            )
        except Exception:
            return f"error: file not found: {path}"

    if not isinstance(old, str) or not isinstance(new, str):
        return "error: old and new must be strings"

    _NOOP_COUNTS.setdefault(path, {})

    basename = os.path.basename(path)

    # Noop: old == new — progressive handling to break loops
    if old == new:
        _NOOP_COUNTS[path]["edit_noop"] = _NOOP_COUNTS[path].get("edit_noop", 0) + 1
        noop_n = _NOOP_COUNTS[path]["edit_noop"]
        if noop_n == 1:
            return (
                f"error: no changes - old equals new in {basename}. "
                "Use a different 'new' value, or switch to write_file for full rewrite."
            )
        if noop_n == 2:
            return (
                f"error: repeated no-op edit in {basename}. "
                "Read the latest file content and apply a real change, or finish if already correct."
            )
        return f"error: repeated no-op edit in {basename}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return f"error: file not found: {path}"

    if old not in text:
        read_hint = ""
        if path not in FILE_VERSIONS:
            read_hint = " Hint: reading the file first often avoids stale/guessed context."
        return (
            f"error: old text was not found in {basename}.\n"
            f"This usually means whitespace or line-break mismatch.\n"
            f"Here is the current content of {basename}:\n{text}\n"
            f"Action: copy the exact text (including whitespace) from above, or use write_file to rewrite the file.{read_hint}"
        )

    count = text.count(old)
    if not args.get("all") and count > 1:
        return (
            f"error: 'old' text appears {count} times in {basename}; it must be unique. "
            f"Include more surrounding lines in 'old' to make it unique, or set all=true to replace all occurrences."
        )

    replacement = text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    if replacement == text:
        return f"error: no change - old and new produce identical result in {basename}."

    real_n = _NOOP_COUNTS[path].get("edit_real", 0) + 1

    # Syntax guard: reject edits that would break valid JS/TS files
    if path.endswith((".js", ".mjs", ".ts")):
        if _js_syntax_ok(text) and not _js_syntax_ok(replacement):
            return (
                f"error: edit rejected - your change would introduce a syntax error in {basename}. File NOT changed. "
                f"Check your 'new' code for missing brackets, semicolons, or quotes, then retry."
            )

    with open(path, "w", encoding="utf-8") as f:
        f.write(replacement)

    _NOOP_COUNTS[path]["edit_real"] = real_n
    _track_file_version(path, replacement)
    if path in _NOOP_COUNTS:
        _NOOP_COUNTS[path].pop("edit_noop", None)

    return f"ok: {count if args.get('all') else 1} replacement(s). Edit applied. If your fix is complete, say done. Do not make unnecessary additional edits."
