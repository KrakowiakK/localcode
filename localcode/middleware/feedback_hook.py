"""
Feedback middleware — tool error feedback rules.

Extracts the error feedback system from run_agent. Contains all tool-error-specific
feedback messages as a rule table. Registers on 'tool_after' event and sets
feedback_text/feedback_reason in the event data when a matching rule fires.
"""

import re
from typing import Any, Callable, Dict, List, Optional

from localcode import hooks

# Module state set by install()
_tools_dict = None
_display_map: Optional[Dict[str, str]] = None
_build_feedback_text_fn: Optional[Callable] = None
_display_tool_name_fn: Optional[Callable] = None


# Feedback rules table.
# Each rule matches on tool name + substring in the error result text.
# When matched, the rule generates feedback_text using a template function.
FEEDBACK_RULES: List[Dict[str, Any]] = [
    {
        "tool": "*",
        "match": "unknown tool '",
        "reason": "unknown_tool_name",
        "build": "_build_unknown_tool_name",
    },
    {
        "tool": "apply_patch",
        "match": "patch context not found",
        "reason": "patch_context_not_found",
        "build": "_build_patch_context_not_found",
    },
    {
        "tool": "apply_patch",
        "match": "patch context not unique",
        "reason": "patch_context_not_unique",
        "build": "_build_patch_context_not_unique",
    },
    {
        "tool": "apply_patch",
        "match_fn": lambda r: "must read" in r and "before patching" in r,
        "reason": "must_read_before_patching",
        "build": "_build_must_read_before_patching",
    },
    {
        "tool": "apply_patch",
        "match": "invalid patch format",
        "reason": "invalid_patch_format",
        "build": "_build_invalid_patch_format",
    },
    {
        "tool": "apply_patch",
        "match": "unexpected patch line",
        "reason": "unexpected_patch_line",
        "build": "_build_unexpected_patch_line",
    },
    {
        "tool": "apply_patch",
        "match": "invalid add line",
        "reason": "invalid_add_line",
        "build": "_build_invalid_add_line",
    },
    {
        "tool": "edit",
        "match_fn": lambda r: "must read" in r and "before editing" in r,
        "reason": "must_read_before_editing",
        "build": "_build_must_read_before_editing",
    },
    {
        "tool": "edit",
        "match_fn": lambda r: (
            "old_string not found" in r
            or "old text was not found" in r
            or "'old' text was not found" in r
            or "old text not found" in r
        ),
        "reason": "old_string_not_found",
        "build": "_build_old_string_not_found",
    },
    {
        "tool": "edit",
        "match_fn": lambda r: "must be unique" in r and "all=true" in r,
        "reason": "old_string_not_unique",
        "build": "_build_old_string_not_unique",
    },
    {
        "tool": "edit",
        "match_fn": lambda r: (
            ("no changes" in r and "old_string equals new_string" in r)
            or ("no changes" in r and "old equals new" in r)
            or ("old and new are identical" in r)
        ),
        "reason": "old_equals_new",
        "build": "_build_old_equals_new",
    },
    {
        "tool": "apply_patch",
        "match_fn": lambda r: "no changes" in r and "no-op" in r,
        "reason": "patch_noop",
        "build": "_build_patch_noop",
    },
    {
        "tool": "apply_patch",
        "match": "repeated patch detected",
        "reason": "patch_repeated",
        "build": "_build_patch_repeated",
    },
    {
        "tool": ("write", "write_file"),
        "match": "no changes",
        "reason": "write_noop",
        "build": "_build_write_noop",
    },
    {
        "tool": ("write", "write_file"),
        "match": "repeated no-op write",
        "reason": "write_repeated_noop",
        "build": "_build_write_repeated_noop",
    },
    {
        "tool": ("write", "write_file"),
        "match_fn": lambda r: (
            "missing required parameter(s) for tool 'write'" in r
            and "content" in r
        ),
        "reason": "write_missing_content",
        "build": "_build_write_missing_content",
    },
    {
        "tool": "read",
        "match": "Is a directory",
        "reason": "read_is_directory",
        "build": "_build_read_is_directory",
    },
    {
        "tool": "read",
        "match": "File not found",
        "reason": "read_file_not_found",
        "build": "_build_read_file_not_found",
    },
    {
        "tool": ("search", "grep"),
        "match": "invalid regex",
        "reason": "invalid_regex",
        "build": "_build_invalid_regex",
    },
    {
        "tool": ("search", "grep"),
        "match": "path does not exist",
        "reason": "search_path_missing",
        "build": "_build_search_path_missing",
    },
    {
        "tool": "apply_patch",
        "match": "File not found",
        "reason": "patch_file_not_found",
        "build": "_build_patch_file_not_found",
    },
    {
        "tool": "ls",
        "match_fn": lambda r: "directory not found" in r or "File not found" in r,
        "reason": "ls_path_missing",
        "build": "_build_ls_path_missing",
    },
    {
        "tool": "glob",
        "match": "path does not exist",
        "reason": "glob_path_missing",
        "build": "_build_glob_path_missing",
    },
]


def _dn(name: str) -> str:
    """Shortcut to get display name for a tool."""
    if _display_tool_name_fn:
        return _display_tool_name_fn(name)
    if _display_map:
        return _display_map.get(name, name)
    return name


def _bft(resolved_name: str, reason: str, fallback: str, values: Optional[Dict[str, Any]] = None) -> str:
    """Shortcut to call build_feedback_text."""
    if _build_feedback_text_fn and _tools_dict:
        return _build_feedback_text_fn(_tools_dict, _display_map, resolved_name, reason, fallback, values)
    return fallback


def _get_target(data: Dict[str, Any], action: str) -> str:
    """Get target path description from event data."""
    path_value = data.get("path_value")
    if path_value:
        return f"the SAME path you attempted to {action}: {path_value}"
    if data.get("tool_name") == "apply_patch":
        return "the file named in the patch header line: '*** Update File: <path>'"
    return f"the SAME path you attempted to {action} (use the 'path' argument from your tool call)"


# --- Builder functions for each feedback rule ---

def _build_patch_context_not_found(data: Dict[str, Any]) -> str:
    target = _get_target(data, "patch")
    patch_tool = _dn("apply_patch")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    write_tool = _dn("write")
    text = _bft("apply_patch", "patch_context_not_found", (
        f"FORMAT ERROR: {patch_tool} failed: patch context not found.\n"
        f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {patch_tool} using the CURRENT content with exact context lines.\n"
        "Do NOT repeat the same patch."
    ), {"target": target})
    fail_count = data.get("patch_fail_count", 0)
    if fail_count >= 2:
        path_value = data.get("path_value", "?")
        text += (
            f"\nSECOND FAILURE on same file ({path_value}): "
            f"STOP patching; re-read and switch to {edit_tool} or {write_tool}."
        )
    return text


def _build_patch_context_not_unique(data: Dict[str, Any]) -> str:
    target = _get_target(data, "patch")
    patch_tool = _dn("apply_patch")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    write_tool = _dn("write")
    return _bft("apply_patch", "patch_context_not_unique", (
        f"FORMAT ERROR: {patch_tool} failed: patch context not unique.\n"
        f"ACTION: Call {read_tool}(path) for {target}, then retry {patch_tool} with MORE unique context lines, "
        f"OR switch to {edit_tool} / {write_tool} if the file is small."
    ), {"target": target})


def _build_must_read_before_patching(data: Dict[str, Any]) -> str:
    target = _get_target(data, "patch")
    patch_tool = _dn("apply_patch")
    read_tool = _dn("read")
    return _bft("apply_patch", "must_read_before_patching", (
        f"FORMAT ERROR: {patch_tool} requires the file to be read first.\n"
        f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {patch_tool}."
    ), {"target": target})


def _build_invalid_patch_format(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    return _bft("apply_patch", "invalid_patch_format", (
        f"FORMAT ERROR: {patch_tool} failed: invalid patch format.\n"
        f"ACTION: Provide a COMPLETE patch with *** Begin Patch and *** End Patch markers and valid context lines. "
        f"Re-read the target file and retry {patch_tool}."
    ))


def _build_unexpected_patch_line(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    return _bft("apply_patch", "unexpected_patch_line", (
        f"FORMAT ERROR: {patch_tool} failed: unexpected patch line.\n"
        f"ACTION: Ensure each line starts with ' ', '+', or '-' and include a valid @@ context header. "
        f"Re-read the target file and retry {patch_tool}."
    ))


def _build_invalid_add_line(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    return _bft("apply_patch", "invalid_add_line", (
        f"FORMAT ERROR: {patch_tool} failed: invalid add line.\n"
        f"ACTION: Lines being added must start with '+'. "
        f"Re-read the target file and retry {patch_tool}."
    ))


def _build_must_read_before_editing(data: Dict[str, Any]) -> str:
    target = _get_target(data, "edit")
    edit_tool = _dn("edit")
    read_tool = _dn("read")
    return _bft("edit", "must_read_before_editing", (
        f"FORMAT ERROR: {edit_tool} requires the file to be read first.\n"
        f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry {edit_tool}."
    ), {"target": target})


def _build_old_string_not_found(data: Dict[str, Any]) -> str:
    target = _get_target(data, "edit")
    edit_tool = _dn("edit")
    read_tool = _dn("read")
    patch_tool = _dn("apply_patch")
    return _bft("edit", "old_string_not_found", (
        f"FORMAT ERROR: {edit_tool} failed: old_string not found.\n"
        f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry with an EXACT substring (including whitespace), "
        f"OR switch to {patch_tool} with exact context."
    ), {"target": target})


def _build_old_string_not_unique(data: Dict[str, Any]) -> str:
    target = _get_target(data, "edit")
    edit_tool = _dn("edit")
    read_tool = _dn("read")
    return _bft("edit", "old_string_not_unique", (
        f"FORMAT ERROR: {edit_tool} failed: old_string is not unique.\n"
        f"ACTION: Call {read_tool}(path) for {target} (use {read_tool}, NOT grep/search), then retry with an exact unique substring, "
        f"OR set all=true if you intend to replace all occurrences."
    ), {"target": target})


def _build_old_equals_new(data: Dict[str, Any]) -> str:
    target = _get_target(data, "edit")
    edit_tool = _dn("edit")
    read_tool = _dn("read")
    write_tool = _dn("write")
    return _bft("edit", "old_equals_new", (
        f"ERROR: {edit_tool} called with old='...' identical to new='...' - no change would occur.\n"
        f"This usually means you want to MODIFY the code, not copy it unchanged.\n"
        f"ACTION:\n"
        f"1. Re-read the file with {read_tool}({target})\n"
        f"2. Identify the EXACT text you want to CHANGE (old)\n"
        f"3. Write the MODIFIED version (new) - it must be DIFFERENT from old\n"
        f"4. If the file already has correct content, the task may be complete - verify and move on.\n"
        f"TIP: For small files, consider using {write_tool} to rewrite the entire file."
    ), {"target": target})


def _build_patch_noop(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    write_tool = _dn("write")
    return _bft("apply_patch", "patch_noop", (
        f"FORMAT ERROR: {patch_tool} applied but made NO changes to the file (no-op).\n"
        f"The file content is identical before and after your patch.\n"
        f"ACTION:\n"
        f"1. Call {read_tool}(path) to see current content\n"
        f"2. Create a NEW {patch_tool} that actually changes content\n"
        f"3. Or switch to {edit_tool}/{write_tool}\n"
        f"Do NOT repeat the same patch."
    ))


def _build_patch_repeated(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    write_tool = _dn("write")
    return _bft("apply_patch", "patch_repeated", (
        f"FORMAT ERROR: You submitted the exact same patch text again.\n"
        f"This will loop forever.\n"
        f"ACTION: {read_tool}(path), then create a DIFFERENT {patch_tool} "
        f"or use {edit_tool}/{write_tool}."
    ))


def _build_write_noop(data: Dict[str, Any]) -> str:
    write_tool = _dn("write")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    return _bft(data.get("tool_name", "write"), "write_noop", (
        f"FORMAT ERROR: {write_tool} wrote identical content (no-op).\n"
        f"ACTION: {read_tool}(path) (optionally diff=true), then {write_tool} with DIFFERENT content, "
        f"or use {edit_tool} for a targeted change. If already correct, call finish."
    ))


def _build_write_repeated_noop(data: Dict[str, Any]) -> str:
    write_tool = _dn("write")
    read_tool = _dn("read")
    edit_tool = _dn("edit")
    path_value = data.get("path_value")
    target = path_value if isinstance(path_value, str) and path_value else "the same file path"
    return _bft(data.get("tool_name", "write"), "write_repeated_noop", (
        f"LOOP GUARD: repeated no-op {write_tool} calls detected.\n"
        "ACTION:\n"
        f"1. Call {read_tool} on {target} (use diff=true if available).\n"
        f"2. Change strategy: use {edit_tool} or substantially different {write_tool} content.\n"
        "3. If implementation is already correct, call finish.\n"
        "Do NOT repeat the same write again."
    ))


def _build_write_missing_content(data: Dict[str, Any]) -> str:
    write_tool = _dn("write")
    return _bft(data.get("tool_name", "write"), "write_missing_content", (
        f"FORMAT ERROR: {write_tool} requires both path and content.\n"
        f"ACTION: call {write_tool} with a complete JSON object containing both fields.\n"
        "If this was an accidental duplicate call after a successful write, skip it and continue."
    ))


def _build_read_is_directory(data: Dict[str, Any]) -> str:
    read_tool = _dn("read")
    ls_tool = _dn("ls")
    return _bft("read", "read_is_directory", (
        f"FORMAT ERROR: {read_tool} failed: path is a directory.\n"
        f"ACTION: Use {ls_tool}(path) to list files, then call {read_tool} on a file path."
    ))


def _build_read_file_not_found(data: Dict[str, Any]) -> str:
    read_tool = _dn("read")
    ls_tool = _dn("ls")
    glob_tool = _dn("glob")
    return _bft("read", "read_file_not_found", (
        f"FORMAT ERROR: {read_tool} failed: file not found.\n"
        f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to locate the correct file, then call {read_tool} with the valid path."
    ))


def _build_invalid_regex(data: Dict[str, Any]) -> str:
    tool_name = data.get("tool_name", "search")
    search_tool = _dn(tool_name)
    return _bft(tool_name, "invalid_regex", (
        f"FORMAT ERROR: {search_tool} failed: invalid regex.\n"
        f"ACTION: If you want literal text, set literal_text=true. Otherwise escape regex metacharacters and retry {search_tool}."
    ))


def _build_search_path_missing(data: Dict[str, Any]) -> str:
    tool_name = data.get("tool_name", "search")
    search_tool = _dn(tool_name)
    ls_tool = _dn("ls")
    glob_tool = _dn("glob")
    return _bft(tool_name, "search_path_missing", (
        f"FORMAT ERROR: {search_tool} failed: path does not exist.\n"
        f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to find the correct path, then retry {search_tool}."
    ))


def _build_patch_file_not_found(data: Dict[str, Any]) -> str:
    patch_tool = _dn("apply_patch")
    ls_tool = _dn("ls")
    glob_tool = _dn("glob")
    return _bft("apply_patch", "patch_file_not_found", (
        f"FORMAT ERROR: {patch_tool} failed: file not found in patch header.\n"
        f"ACTION: Use {ls_tool}(path) or {glob_tool}(pat, path) to locate the correct file path, then retry {patch_tool} with the correct '*** Update File:' path."
    ))


def _build_ls_path_missing(data: Dict[str, Any]) -> str:
    ls_tool = _dn("ls")
    glob_tool = _dn("glob")
    return _bft("ls", "ls_path_missing", (
        f"FORMAT ERROR: {ls_tool} failed: path does not exist.\n"
        f"ACTION: Use {ls_tool} with a valid path (e.g. '.') or use {glob_tool}(pat, path) to discover files."
    ))


def _build_glob_path_missing(data: Dict[str, Any]) -> str:
    glob_tool = _dn("glob")
    ls_tool = _dn("ls")
    return _bft("glob", "glob_path_missing", (
        f"FORMAT ERROR: {glob_tool} failed: path does not exist.\n"
        f"ACTION: Use {ls_tool}(path) to verify directories, then retry {glob_tool} with a valid path."
    ))


def _build_unknown_tool_name(data: Dict[str, Any]) -> str:
    read_tool = _dn("read")
    grep_tool = _dn("grep")
    search_tool = _dn("search")
    write_tool = _dn("write")
    edit_tool = _dn("edit")
    patch_tool = _dn("apply_patch")
    finish_tool = _dn("finish")
    result_text = str(data.get("result") or "")
    attempted = ""
    m = re.search(r"unknown tool '([^']+)'", result_text, re.IGNORECASE)
    if m:
        attempted = m.group(1).strip().lower()
    extra = ""
    if attempted in {"run", "exec", "execute", "bash", "cmd"}:
        extra = (
            f"\nIf you wanted to inspect code, use {read_tool}/{grep_tool}/{search_tool}. "
            "There is no run/exec tool."
        )
    return (
        "FORMAT ERROR: unknown tool name was called.\n"
        "ACTION: use only available tools shown in the error.\n"
        f"Common choices: {read_tool}, {write_tool}, {edit_tool}, {patch_tool}, {finish_tool}.\n"
        "Do not retry the same unknown tool name."
        f"{extra}"
    )


# Builder lookup (string name -> function)
_BUILDERS: Dict[str, Callable] = {
    "_build_unknown_tool_name": _build_unknown_tool_name,
    "_build_patch_context_not_found": _build_patch_context_not_found,
    "_build_patch_context_not_unique": _build_patch_context_not_unique,
    "_build_must_read_before_patching": _build_must_read_before_patching,
    "_build_invalid_patch_format": _build_invalid_patch_format,
    "_build_unexpected_patch_line": _build_unexpected_patch_line,
    "_build_invalid_add_line": _build_invalid_add_line,
    "_build_must_read_before_editing": _build_must_read_before_editing,
    "_build_old_string_not_found": _build_old_string_not_found,
    "_build_old_string_not_unique": _build_old_string_not_unique,
    "_build_old_equals_new": _build_old_equals_new,
    "_build_patch_noop": _build_patch_noop,
    "_build_patch_repeated": _build_patch_repeated,
    "_build_write_noop": _build_write_noop,
    "_build_write_repeated_noop": _build_write_repeated_noop,
    "_build_write_missing_content": _build_write_missing_content,
    "_build_read_is_directory": _build_read_is_directory,
    "_build_read_file_not_found": _build_read_file_not_found,
    "_build_invalid_regex": _build_invalid_regex,
    "_build_search_path_missing": _build_search_path_missing,
    "_build_patch_file_not_found": _build_patch_file_not_found,
    "_build_ls_path_missing": _build_ls_path_missing,
    "_build_glob_path_missing": _build_glob_path_missing,
}


def _rule_matches(rule: Dict[str, Any], tool_name: str, result_text: str) -> bool:
    """Check if a feedback rule matches the given tool name and result text."""
    rule_tool = rule["tool"]
    if rule_tool == "*":
        pass
    elif isinstance(rule_tool, tuple):
        if tool_name not in rule_tool:
            return False
    elif tool_name != rule_tool:
        return False

    result_lc = result_text.lower()
    if "match_fn" in rule:
        return rule["match_fn"](result_lc)
    needle = rule.get("match", "")
    if not isinstance(needle, str):
        return False
    return needle.lower() in result_lc


def on_tool_after(data: Dict[str, Any]) -> Dict[str, Any]:
    """Check tool result against feedback rules, set feedback_text if matched."""
    if not data.get("is_error"):
        return data

    tool_name = data.get("tool_name", "")
    result = data.get("result", "")
    result_text = result if isinstance(result, str) else ""

    for rule in FEEDBACK_RULES:
        if _rule_matches(rule, tool_name, result_text):
            builder_name = rule["build"]
            builder_fn = _BUILDERS.get(builder_name)
            if builder_fn:
                data["feedback_text"] = builder_fn(data)
                data["feedback_reason"] = rule["reason"]
                return data
    return data


def install(tools_dict=None, display_map=None) -> None:
    """Register feedback hook on tool_after event."""
    global _tools_dict, _display_map
    _tools_dict = tools_dict
    _display_map = display_map
    hooks.register("tool_after", on_tool_after)


def set_functions(build_feedback_text_fn=None, display_tool_name_fn=None) -> None:
    """Set callback functions from localcode.py for template rendering."""
    global _build_feedback_text_fn, _display_tool_name_fn
    if build_feedback_text_fn:
        _build_feedback_text_fn = build_feedback_text_fn
    if display_tool_name_fn:
        _display_tool_name_fn = display_tool_name_fn
