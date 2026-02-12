"""
Tool dispatch: process_tool_call(), argument validation, name resolution.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from localcode.tool_handlers._state import (
    TOOL_ALIAS_MAP,
    TOOL_DISPLAY_MAP,
    UNSUPPORTED_TOOLS,
)
from localcode.tool_handlers import _state as _state_mod
from localcode.middleware import logging_hook


# ---------------------------
# Tool-arg repair for number words
# ---------------------------

_NUMBER_WORDS = {
    "zero": 0,
    "a": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

_TOOL_ARG_NUMBER_FIELDS = {
    "read": {"line_start", "line_end", "offset", "limit"},
    "search": {"max_results"},
    "ask_agent": {"timeout"},
}


# ---------------------------
# Argument alias mapping (small-model friendly)
# ---------------------------
# Maps common alternative parameter names to the canonical names.
# This allows small models to use names like "file" instead of "path"
# without getting validation errors.

_ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "read": {
        "file": "path", "filename": "path", "file_path": "path",
        "filepath": "path", "file_name": "path",
    },
    "write": {
        "file": "path", "filename": "path", "file_path": "path",
        "filepath": "path", "file_name": "path",
        "text": "content", "data": "content", "code": "content",
        "body": "content", "source": "content",
    },
    "edit": {
        "file": "path", "filename": "path", "file_path": "path",
        "filepath": "path", "file_name": "path",
        "old_string": "old", "old_text": "old", "search": "old",
        "find": "old", "original": "old", "before": "old",
        "new_string": "new", "new_text": "new", "replace": "new",
        "replacement": "new", "after": "new",
    },
    "glob": {
        "pattern": "pat", "glob_pattern": "pat", "search": "pat",
        "file": "path", "dir": "path", "directory": "path",
        "folder": "path", "root": "path",
    },
    "grep": {
        "query": "pat", "search": "pat", "text": "pat",
        "regex": "pat", "pattern": "pat",
        "file": "path", "dir": "path", "directory": "path",
        "folder": "path",
    },
    "search": {
        "query": "pattern", "text": "pattern", "regex": "pattern",
        "pat": "pattern",
        "file": "path", "dir": "path",
        "max": "max_results", "limit": "max_results", "maxresults": "max_results",
    },
    "ask_agent": {
        "question": "prompt", "query": "prompt", "text": "prompt",
        "secs": "timeout", "seconds": "timeout", "time_limit": "timeout",
    },
    "plan_solution": {
        "question": "prompt", "query": "prompt", "request": "prompt",
        "text": "prompt", "content": "prompt", "thought": "prompt",
    },
}


def _normalize_arg_names(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Remap alternative argument names to canonical names for a given tool."""
    aliases = _ARG_ALIASES.get(tool_name)
    if not aliases:
        return args
    normalized = {}
    for key, value in args.items():
        canonical = aliases.get(key.lower(), key)
        # Don't overwrite if canonical key already present
        if canonical in normalized:
            continue
        normalized[canonical] = value
    return normalized


# ---------------------------
# JSON repair for malformed tool arguments
# ---------------------------

def _repair_json(raw: str) -> str:
    """Try to fix common JSON errors from small models."""
    if not raw or not raw.strip():
        return raw
    s = raw.strip()
    # Fix trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # Fix single quotes → double quotes (simple heuristic: only if no double quotes in values)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')
    # Fix missing closing brace
    opens = s.count('{') - s.count('}')
    if opens > 0:
        s += '}' * opens
    opens_bracket = s.count('[') - s.count(']')
    if opens_bracket > 0:
        s += ']' * opens_bracket
    return s


def _parse_number_words(text: str) -> Optional[int]:
    if not text:
        return None
    words = [w for w in re.split(r"\s+", text.strip().lower()) if w and w != "and"]
    if not words:
        return None
    if len(words) == 1:
        return _NUMBER_WORDS.get(words[0])

    if len(words) == 2 and words[1] == "hundred":
        base = _NUMBER_WORDS.get(words[0])
        return None if base is None else base * 100

    if len(words) >= 3 and words[1] == "hundred":
        base = _NUMBER_WORDS.get(words[0])
        if base is None:
            return None
        remainder = " ".join(words[2:])
        rv = _parse_number_words(remainder)
        return None if rv is None else base * 100 + rv

    if len(words) == 2:
        first = _NUMBER_WORDS.get(words[0])
        second = _NUMBER_WORDS.get(words[1])
        return None if (first is None or second is None) else first + second

    return None


def _repair_number_word_args(raw_args: str, fields: set) -> str:
    if not raw_args or not fields:
        return raw_args
    field_pattern = "|".join(re.escape(f) for f in sorted(fields))
    pattern = rf'"({field_pattern})"\s*:\s*([A-Za-z_-]+(?:\s+[A-Za-z_-]+)*)'

    def repl(m: re.Match) -> str:
        v = _parse_number_words(m.group(2))
        if v is not None:
            return f"\"{m.group(1)}\": {v}"
        return m.group(0)

    return re.sub(pattern, repl, raw_args)


def _coerce_integer_like_value(value: Any) -> Tuple[Optional[int], bool]:
    """Convert integer-like values (10, 10.0, '10', '10.0', number-words) to int."""
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, float):
        if value.is_integer():
            return int(value), True
        return None, False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, False
        word_num = _parse_number_words(text)
        if word_num is not None:
            return word_num, True
        if re.fullmatch(r"[-+]?\d+", text):
            try:
                return int(text), True
            except ValueError:
                return None, False
        if re.fullmatch(r"[-+]?\d+\.0+", text):
            try:
                return int(float(text)), True
            except ValueError:
                return None, False
    return None, False


def _coerce_integer_like_fields(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    fields = _TOOL_ARG_NUMBER_FIELDS.get(tool_name)
    if not fields:
        return None
    for field in fields:
        if field not in args:
            continue
        coerced, ok = _coerce_integer_like_value(args[field])
        if not ok:
            return (
                f"error: invalid type for parameter '{field}' on tool '{tool_name}': "
                "expected whole number (e.g., 10 or 10.0)"
            )
        args[field] = coerced
    return None


def _extract_patch_block(text: str) -> Optional[str]:
    if not text:
        return None
    start = text.find("*** Begin Patch")
    if start < 0:
        return None
    end = text.find("*** End Patch", start)
    if end < 0:
        return None
    end += len("*** End Patch")
    return text[start:end]


def _validate_tool_args(tool_name: str, args: Any, params: Optional[Dict[str, str]]) -> Optional[str]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return f"error: invalid arguments for tool '{tool_name}': expected object"
    if not params:
        return None

    required = []
    type_map: Dict[str, Tuple[str, bool]] = {}
    for key, param_type in params.items():
        base_type = None
        optional = False
        if isinstance(param_type, str):
            optional = param_type.endswith("?")
            base_type = param_type.rstrip("?")
        elif isinstance(param_type, dict):
            type_val = param_type.get("type")
            if isinstance(type_val, str):
                optional = bool(param_type.get("optional", False))
                if type_val.endswith("?"):
                    optional = True
                    type_val = type_val.rstrip("?")
                base_type = type_val
        if not base_type:
            continue
        type_map[key] = (base_type, optional)
        if not optional:
            required.append(key)

    unknown = sorted(set(args.keys()) - set(params.keys()))
    if unknown:
        valid_params = sorted(params.keys())
        return (
            f"error: unknown parameter(s) for tool '{tool_name}': {', '.join(unknown)}. "
            f"Valid parameters: {', '.join(valid_params)}"
        )

    missing = sorted(set(required) - set(args.keys()))
    if missing:
        example_args = {p: "..." for p in required}
        return (
            f"error: missing required parameter(s) for tool '{tool_name}': {', '.join(missing)}. "
            f"Example: {tool_name}({json.dumps(example_args)})"
        )

    for key, value in args.items():
        base_type, optional = type_map.get(key, (None, False))
        if value is None:
            if optional:
                continue
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected {base_type}"
        if base_type == "string" and not isinstance(value, str):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected string"
        if base_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected number"
        if base_type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected integer"
        if base_type == "boolean" and not isinstance(value, bool):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected boolean"
        if base_type == "array" and not isinstance(value, list):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected array"
        if base_type == "object" and not isinstance(value, dict):
            return f"error: invalid type for parameter '{key}' on tool '{tool_name}': expected object"

    return None


def resolve_tool_name(name: str) -> str:
    raw = (name or "").strip()
    if raw:
        raw = raw.splitlines()[0]
    if "<|" in raw:
        raw = raw.split("<|", 1)[0]
    key = raw.strip().lower()
    return TOOL_ALIAS_MAP.get(key, key)


def display_tool_name(name: str) -> str:
    return TOOL_DISPLAY_MAP.get(name, name)


def is_tool_error(tool_name: str, result: Any) -> bool:
    if not isinstance(result, str):
        return False
    if result.startswith("error:"):
        return True
    if tool_name == "shell":
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        exit_code = (payload.get("metadata") or {}).get("exit_code")
        return isinstance(exit_code, int) and exit_code != 0
    return False


# ToolsDict type alias (matches localcode.py)
ToolTuple = Tuple[str, Dict[str, Any], Any, Dict[str, Any], Dict[str, Any]]
ToolsDict = Dict[str, ToolTuple]


def process_tool_call(tools_dict: ToolsDict, tc: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, str]:
    func = tc.get("function", {}) or {}
    tool_name = func.get("name", "") or ""
    if not tool_name.strip():
        return "", {}, "error: missing tool name", ""

    raw_args = func.get("arguments", "{}")
    resolved = resolve_tool_name(tool_name)

    if isinstance(raw_args, dict):
        tool_args = raw_args
    else:
        original_raw_args = raw_args
        try:
            tool_args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            # Stage 1: try JSON repair (trailing commas, single quotes, missing braces)
            repaired_json = _repair_json(str(raw_args))
            json_repaired = False
            if repaired_json != raw_args:
                try:
                    tool_args = json.loads(repaired_json)
                    json_repaired = True
                    logging_hook.log_event("format_repair", {
                        "tool": resolved,
                        "reason": "json_repair",
                    })
                except json.JSONDecodeError:
                    pass

            if not json_repaired:
                # Stage 2: try patch block extraction for apply_patch
                if resolved == "apply_patch":
                    patch = _extract_patch_block(str(raw_args))
                    if patch:
                        tool_args = {"patch": patch}
                        json_repaired = True
                        logging_hook.log_event("format_repair", {
                            "tool": "apply_patch",
                            "reason": "patch_block_recover",
                        })

            if not json_repaired:
                # Stage 3: try number word repair
                repaired = _repair_number_word_args(
                    str(raw_args),
                    _TOOL_ARG_NUMBER_FIELDS.get(resolved, set()),
                )
                if repaired != raw_args:
                    try:
                        tool_args = json.loads(repaired)
                        json_repaired = True
                    except json.JSONDecodeError:
                        pass

            if not json_repaired:
                return resolved, {}, f"error: invalid JSON in tool arguments: {exc}. Raw: {str(original_raw_args)[:100]}", tool_name

    unsupported_key = tool_name.strip().lower()
    unsupported_resolved = resolve_tool_name(tool_name)
    if unsupported_key in UNSUPPORTED_TOOLS:
        return resolved, tool_args, UNSUPPORTED_TOOLS[unsupported_key], tool_name
    if unsupported_resolved in UNSUPPORTED_TOOLS:
        return resolved, tool_args, UNSUPPORTED_TOOLS[unsupported_resolved], tool_name

    if resolved not in tools_dict:
        available = sorted(TOOL_DISPLAY_MAP.get(k, k) for k in tools_dict)
        return resolved, tool_args, f"error: unknown tool '{tool_name}'. Available tools: {', '.join(available)}. Use one of these.", tool_name

    # Normalize argument names (alias mapping for small-model friendliness)
    if isinstance(tool_args, dict):
        original_keys = set(tool_args.keys())
        tool_args = _normalize_arg_names(resolved, tool_args)
        remapped_keys = set(tool_args.keys()) - original_keys
        if remapped_keys:
            logging_hook.log_event("format_repair", {
                "tool": resolved,
                "reason": "arg_alias_remap",
                "remapped": sorted(remapped_keys),
            })

    # Post-remap repair/coercion for integer-like fields.
    if isinstance(tool_args, dict):
        integer_like_error = _coerce_integer_like_fields(resolved, tool_args)
        if integer_like_error:
            return resolved, tool_args, integer_like_error, tool_name

    params = tools_dict[resolved][1]
    validation_error = _validate_tool_args(resolved, tool_args, params)
    if validation_error:
        return resolved, tool_args, validation_error, tool_name

    # Increment global tool call counter (for urgency escalation in hints)
    _state_mod.TOOL_CALL_COUNT += 1

    try:
        result = tools_dict[resolved][2](tool_args)
    except Exception as err:
        result = f"error: {err}"

    return resolved, tool_args, result, tool_name
