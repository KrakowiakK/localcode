"""
Tool handlers package — extracted from localcode.py (Phase 2).

Re-exports all public names so that localcode.py can import from
`localcode.tool_handlers` directly.
"""

# _state: constants, mutable state, utilities
from localcode.tool_handlers._state import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_READ_LIMIT,
    DEFAULT_SHELL_TIMEOUT_MS,
    FILE_VERSIONS,
    MAX_FILE_SIZE,
    MAX_FILE_VERSIONS,
    MAX_GLOB_RESULTS,
    MAX_GREP_RESULTS,
    MAX_LINE_LENGTH,
    MAX_SHELL_OUTPUT_CHARS,
    MAX_SHELL_TIMEOUT_MS,
    MAX_SINGLE_FILE_SCAN,
    SANDBOX_ROOT,
    TOOL_ALIAS_MAP,
    TOOL_DISPLAY_MAP,
    UNSUPPORTED_TOOLS,
    _LAST_PATCH_HASH,
    _NOOP_COUNTS,
    _PATCH_FILE_RE,
    _read_file_bytes,
    _require_args_dict,
    _reset_noop_tracking,
    _sha256,
    _track_file_version,
    extract_patch_file,
    normalize_args,
)

# _path: path validation
from localcode.tool_handlers._path import (
    _is_ignored_path,
    _is_path_within_sandbox,
    _validate_path,
)

# _sandbox: shell security
from localcode.tool_handlers._sandbox import (
    DANGEROUS_PATTERNS,
    TEST_MENTION_RE,
    _check_dangerous_command,
    _check_sandbox_allowlist,
    _DANGEROUS_COMMAND_RES,
    _ENV_VAR_ASSIGN_RE,
    _SANDBOX_ALLOWED_CMDS,
    _SANDBOX_INLINE_CODE_RE,
    _SHELL_CD_RE,
    _SHELL_CHAINING_RE,
)

# read_handlers
from localcode.tool_handlers.read_handlers import batch_read, read

# write_handlers
from localcode.tool_handlers.write_handlers import edit, write

# patch_handlers
from localcode.tool_handlers.patch_handlers import (
    _adjust_indent,
    _apply_add_patch,
    _apply_delete_patch,
    _apply_hunks,
    _apply_update_patch,
    _find_sublist,
    _get_indent,
    _log_fuzzy_match,
    _normalize_indent,
    _parse_hunks,
    apply_patch_fn,
)

# search_handlers
from localcode.tool_handlers.search_handlers import glob_fn, grep_fn, ls_fn, search_fn

# shell_handler
from localcode.tool_handlers.shell_handler import (
    _shell_payload,
    _truncate_shell_output,
    shell,
)

# dispatch
from localcode.tool_handlers.dispatch import (
    _NUMBER_WORDS,
    _TOOL_ARG_NUMBER_FIELDS,
    _extract_patch_block,
    _parse_number_words,
    _repair_number_word_args,
    _validate_tool_args,
    display_tool_name,
    is_tool_error,
    process_tool_call,
    resolve_tool_name,
)

# schema
from localcode.tool_handlers.schema import (
    build_feedback_text,
    build_tools,
    get_tool_feedback_template,
    make_openai_tools,
    render_feedback_template,
    render_tool_description,
)
