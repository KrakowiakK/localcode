"""
Patch tool handler: apply_patch_fn() and all patch helpers.
"""

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers._state import (
    FILE_VERSIONS,
    _LAST_PATCH_HASH,
    _NOOP_COUNTS,
    _read_file_bytes,
    _require_args_dict,
    _sha256,
    _track_file_version,
)
from localcode.tool_handlers._path import _should_block_test_edit, _validate_path


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

        for line in lines:
            raw_path = None
            if line.startswith("*** Update File: "):
                raw_path = line[len("*** Update File: "):].strip()
            elif line.startswith("*** Add File: "):
                raw_path = line[len("*** Add File: "):].strip()
            elif line.startswith("*** Delete File: "):
                raw_path = line[len("*** Delete File: "):].strip()
            elif line.startswith("*** Move to: "):
                raw_path = line[len("*** Move to: "):].strip()
            if raw_path and _should_block_test_edit(raw_path):
                return f"error: test file edits are blocked in benchmark mode ({raw_path})"

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
