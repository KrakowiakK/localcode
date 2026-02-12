"""
File writing tool handlers: write(), edit().
"""

import os
import hashlib
import difflib
import re
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
    _mutation_brief_line,
    _mutation_decision_hint,
    _mutation_state_line,
    _record_mutation,
    _require_args_dict,
    _short_sha_text,
    _track_file_version,
)
from localcode.tool_handlers._path import (
    _find_file_in_sandbox,
    _should_block_test_edit,
    _validate_path,
    to_display_path,
)


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

    display_spec = to_display_path(spec_path)
    return f"\n\nYou have not read the test file. Here are the tests:\n=== {display_spec} ===\n{''.join(out_parts)}"


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


def _content_line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _changed_lines_est(previous: str, current: str) -> int:
    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(a=prev_lines, b=curr_lines, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            changed += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            changed += i2 - i1
        elif tag == "insert":
            changed += j2 - j1
    return changed


def _change_summary(previous: str, current: str) -> str:
    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    changed_lines_est = _changed_lines_est(previous, current)
    return (
        f"change_summary: prev_sha256={_content_digest(previous)} "
        f"new_sha256={_content_digest(current)} "
        f"changed_lines~={changed_lines_est} "
        f"line_delta={len(curr_lines) - len(prev_lines)} "
        f"char_delta={len(current) - len(previous)}"
    )


def _changed_symbols(previous: str, current: str, max_symbols: int = 10) -> List[str]:
    js_keywords = {
        "if", "for", "while", "switch", "catch", "return", "throw", "new",
        "typeof", "instanceof", "void", "delete", "in", "of", "do", "else",
        "case", "default", "break", "continue", "try", "finally", "await",
        "yield", "class", "function", "const", "let", "var", "import", "export",
        "extends", "super",
    }

    def _add_symbol(raw: str, out: List[str], seen: set) -> None:
        name = raw.strip()
        if not name or name in js_keywords:
            return
        if name in seen:
            return
        seen.add(name)
        out.append(name)

    diff_lines = list(
        difflib.unified_diff(
            previous.splitlines(),
            current.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
            n=1,
        )
    )
    symbols: List[str] = []
    seen = set()
    for line in diff_lines:
        if not (line.startswith("+") or line.startswith("-")):
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        text = line[1:].strip()
        m_class = re.match(r"^(?:export\s+)?class\s+([A-Za-z_]\w*)\b", text)
        if m_class:
            _add_symbol(f"class:{m_class.group(1)}", symbols, seen)

        m_function = re.match(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", text)
        if m_function:
            _add_symbol(f"fn:{m_function.group(1)}", symbols, seen)

        m_method = re.match(r"^(?:async\s+)?([A-Za-z_]\w*)\s*\([^=]*\)\s*\{?$", text)
        if m_method:
            name = m_method.group(1)
            if name not in js_keywords:
                _add_symbol(f"fn:{name}", symbols, seen)

        m_const_fn = re.match(
            r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>",
            text,
        )
        if m_const_fn:
            _add_symbol(f"fn:{m_const_fn.group(1)}", symbols, seen)
        if len(symbols) >= max_symbols:
            break
    return symbols


def _changed_line_preview(previous: str, current: str, max_lines: int = 6) -> str:
    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    matcher = difflib.SequenceMatcher(a=prev_lines, b=curr_lines, autojunk=False)

    changed_indexes: List[int] = []
    seen = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j1 < j2:
            for idx in range(j1, min(j2, j1 + max_lines)):
                if idx not in seen:
                    seen.add(idx)
                    changed_indexes.append(idx)
                if len(changed_indexes) >= max_lines:
                    break
        elif curr_lines:
            idx = min(max(j1 - 1, 0), len(curr_lines) - 1)
            if idx not in seen:
                seen.add(idx)
                changed_indexes.append(idx)
        if len(changed_indexes) >= max_lines:
            break

    if not changed_indexes:
        return "changed_lines_preview:\n(no changed lines captured)"

    out = ["changed_lines_preview:"]
    for idx in changed_indexes[:max_lines]:
        line_text = curr_lines[idx] if 0 <= idx < len(curr_lines) else ""
        out.append(f"{idx + 1:4}| {line_text}")
    return "\n".join(out)


def _current_file_sha(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _short_sha_text(f.read())
    except Exception:
        return "unknown"


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
    display_path = to_display_path(path)

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
            file_state = (
                f"file_state: lines={_content_line_count(content)} "
                f"chars={len(content)} sha256={_content_digest(content)}"
            )
            mutation = _record_mutation(
                op="write",
                path=path,
                changed=False,
                before_sha=_content_digest(content),
                after_sha=_content_digest(content),
                changed_lines_est=0,
                noop_streak_for_file=noop_n,
            )
            decision_hint = _mutation_decision_hint(mutation)
            state_brief = _mutation_brief_line(mutation)
            state_line = _mutation_state_line(mutation)
            if noop_n == 1:
                # First noop: keep as success so model can finish when file is already correct.
                return (
                    "ok: no changes - file already has this content.\n"
                    f"{file_state}\n"
                    f"{decision_hint}\n"
                    f"{state_brief}\n"
                    f"{state_line}"
                )
            return (
                f"error: repeated no-op write for {os.path.basename(path)}. "
                "Write different content, or call finish if implementation is already correct.\n"
                f"{file_state}\n"
                f"{decision_hint}\n"
                f"{state_brief}\n"
                f"{state_line}"
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
        file_state = (
            f"file_state: lines={_content_line_count(content)} "
            f"chars={len(content)} sha256={_content_digest(content)}"
        )
        mutation = _record_mutation(
            op="write",
            path=path,
            changed=True,
            before_sha=_short_sha_text(""),
            after_sha=_content_digest(content),
            changed_lines_est=_changed_lines_est("", content),
            changed_symbols=_changed_symbols("", content),
            noop_streak_for_file=0,
        )
        decision_hint = _mutation_decision_hint(mutation)
        state_brief = _mutation_brief_line(mutation)
        state_line = _mutation_state_line(mutation)
        loop_hint = ""
        if int(mutation.get("write_streak_for_file", 0) or 0) >= 2:
            loop_hint = (
                f"\nloop_guard: repeated full-file write streak={mutation.get('write_streak_for_file')} "
                "on this file; prefer edit/apply_patch or finish."
            )
        changed_preview = _changed_line_preview("", content)
        return (
            f"ok: created {display_path}, +{additions} lines\n"
            f"{file_state}\n"
            f"{decision_hint}\n"
            f"{state_brief}\n"
            f"{state_line}\n"
            f"{changed_preview}\n"
            f"{loop_hint}"
            f"{spec_inject}{write_hint}"
        )

    old_lines = old_content.count("\n")
    new_lines = content.count("\n")
    additions = max(0, new_lines - old_lines)
    removals = max(0, old_lines - new_lines)
    file_state = (
        f"file_state: lines={_content_line_count(content)} "
        f"chars={len(content)} sha256={_content_digest(content)}"
    )
    changed_lines = _changed_lines_est(old_content, content)
    symbols = _changed_symbols(old_content, content)
    mutation = _record_mutation(
        op="write",
        path=path,
        changed=True,
        before_sha=_content_digest(old_content),
        after_sha=_content_digest(content),
        changed_lines_est=changed_lines,
        changed_symbols=symbols,
        noop_streak_for_file=0,
    )
    decision_hint = _mutation_decision_hint(mutation)
    state_brief = _mutation_brief_line(mutation)
    state_line = _mutation_state_line(mutation)
    loop_hint = ""
    if int(mutation.get("write_streak_for_file", 0) or 0) >= 2:
        loop_hint = (
            f"\nloop_guard: repeated full-file write streak={mutation.get('write_streak_for_file')} "
            "on this file; prefer edit/apply_patch or finish."
        )
    summary = _change_summary(old_content, content)
    changed_preview = _changed_line_preview(old_content, content)
    return (
        f"ok: updated {display_path}, +{additions} -{removals} lines\n"
        f"{file_state}\n"
        f"{decision_hint}\n"
        f"{state_brief}\n"
        f"{state_line}\n"
        f"{summary}\n"
        f"{changed_preview}\n"
        f"{loop_hint}"
        f"{spec_inject}{write_hint}"
    )


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
    display_path = to_display_path(path)

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
            return f"error: file not found: {display_path}"

    if not isinstance(old, str) or not isinstance(new, str):
        return "error: old and new must be strings"

    _NOOP_COUNTS.setdefault(path, {})

    basename = os.path.basename(path)

    # Noop: old == new — progressive handling to break loops
    if old == new:
        _NOOP_COUNTS[path]["edit_noop"] = _NOOP_COUNTS[path].get("edit_noop", 0) + 1
        noop_n = _NOOP_COUNTS[path]["edit_noop"]
        current_sha = _current_file_sha(path)
        mutation = _record_mutation(
            op="edit",
            path=path,
            changed=False,
            before_sha=current_sha,
            after_sha=current_sha,
            changed_lines_est=0,
            noop_streak_for_file=noop_n,
        )
        decision_hint = _mutation_decision_hint(mutation)
        state_brief = _mutation_brief_line(mutation)
        state_line = _mutation_state_line(mutation)
        if noop_n == 1:
            return (
                f"error: no changes - old equals new in {basename}. "
                "Use a different 'new' value, or switch to write_file for full rewrite.\n"
                f"{decision_hint}\n"
                f"{state_brief}\n"
                f"{state_line}"
            )
        if noop_n == 2:
            return (
                f"error: repeated no-op edit in {basename}. "
                "Read the latest file content and apply a real change, or finish if already correct.\n"
                f"{decision_hint}\n"
                f"{state_brief}\n"
                f"{state_line}"
            )
        return f"error: repeated no-op edit in {basename}\n{decision_hint}\n{state_brief}\n{state_line}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return f"error: file not found: {display_path}"

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

    replacement_count = count if args.get("all") else 1
    file_state = (
        f"file_state: lines={_content_line_count(replacement)} "
        f"chars={len(replacement)} sha256={_content_digest(replacement)}"
    )
    changed_lines = _changed_lines_est(text, replacement)
    symbols = _changed_symbols(text, replacement)
    mutation = _record_mutation(
        op="edit",
        path=path,
        changed=True,
        before_sha=_content_digest(text),
        after_sha=_content_digest(replacement),
        changed_lines_est=changed_lines,
        changed_symbols=symbols,
        noop_streak_for_file=0,
    )
    decision_hint = _mutation_decision_hint(mutation)
    state_brief = _mutation_brief_line(mutation)
    state_line = _mutation_state_line(mutation)
    summary = _change_summary(text, replacement)
    changed_preview = _changed_line_preview(text, replacement)
    return (
        f"ok: {replacement_count} replacement(s). Edit applied in {basename}.\n"
        f"{file_state}\n"
        f"{decision_hint}\n"
        f"{state_brief}\n"
        f"{state_line}\n"
        f"{summary}\n"
        f"{changed_preview}\n"
        "Action: if this satisfies requirements, call finish; otherwise make the next targeted edit."
    )
