"""
File writing tool handlers: write(), edit().
"""

import os
import hashlib
import difflib
import json
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


def _enforce_read_before_write_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_ENFORCE_READ_BEFORE_WRITE", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _edit_success_snippet_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_EDIT_SNIPPET_SUCCESS", "")).strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def _write_success_snippet_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_WRITE_SNIPPET_SUCCESS", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _write_spec_focus_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_WRITE_SPEC_FOCUS", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _write_spec_contract_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_WRITE_SPEC_CONTRACT", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _edit_verbose_state_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_EDIT_VERBOSE_STATE", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _write_verbose_state_enabled() -> bool:
    raw = str(os.environ.get("LOCALCODE_WRITE_VERBOSE_STATE", "")).strip().lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


_UNICODE_TRANSLATION_TABLE = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201A": "'",
    "\u201B": "'",
    "\u2032": "'",
    "\u2035": "'",
    "\u201C": '"',
    "\u201D": '"',
    "\u201E": '"',
    "\u201F": '"',
    "\u2033": '"',
    "\u2036": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u00A0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2004": " ",
    "\u2005": " ",
    "\u2006": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200A": " ",
    "\u202F": " ",
    "\u205F": " ",
    "\u3000": " ",
})


def _normalize_unicode_for_match(text: str) -> str:
    if not text:
        return text
    return text.translate(_UNICODE_TRANSLATION_TABLE)


def _strip_single_trailing_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n") or text.endswith("\r"):
        return text[:-1]
    return text


def _find_unique_unicode_slice(text: str, needle: str) -> Optional[str]:
    normalized_text = _normalize_unicode_for_match(text)
    normalized_needle = _normalize_unicode_for_match(needle)
    if not normalized_needle:
        return None
    positions: List[int] = []
    start = 0
    while len(positions) < 2:
        idx = normalized_text.find(normalized_needle, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    if len(positions) != 1:
        return None
    pos = positions[0]
    return text[pos: pos + len(needle)]


def _find_unique_line_window_slice(
    text: str,
    needle: str,
    transform,
) -> Optional[str]:
    haystack_lines = text.splitlines(keepends=True)
    needle_lines = needle.splitlines(keepends=True)
    if not haystack_lines or not needle_lines:
        return None
    if len(needle_lines) > len(haystack_lines):
        return None

    hay_noeol = [line.rstrip("\r\n") for line in haystack_lines]
    needle_noeol = [line.rstrip("\r\n") for line in needle_lines]

    normalized_hay = [transform(line) for line in hay_noeol]
    normalized_needle = [transform(line) for line in needle_noeol]
    window = len(normalized_needle)

    matches: List[int] = []
    for idx in range(len(normalized_hay) - window + 1):
        if normalized_hay[idx: idx + window] == normalized_needle:
            matches.append(idx)
            if len(matches) > 1:
                return None
    if not matches:
        return None

    first = matches[0]
    selected = haystack_lines[first: first + window]
    canonical = "".join(selected)
    if not needle.endswith(("\n", "\r")):
        canonical = _strip_single_trailing_newline(canonical)
    return canonical


def _resolve_old_text(text: str, old: str) -> Optional[str]:
    if old in text:
        return old

    candidates = [old]
    trimmed = _strip_single_trailing_newline(old)
    if trimmed and trimmed != old:
        candidates.append(trimmed)

    for candidate in candidates:
        if candidate in text:
            return candidate

        unicode_slice = _find_unique_unicode_slice(text, candidate)
        if unicode_slice is not None:
            return unicode_slice

        line_exact = _find_unique_line_window_slice(text, candidate, lambda value: value)
        if line_exact is not None:
            return line_exact

        line_trimmed = _find_unique_line_window_slice(text, candidate, lambda value: value.rstrip())
        if line_trimmed is not None:
            return line_trimmed

        line_unicode_trimmed = _find_unique_line_window_slice(
            text,
            candidate,
            lambda value: _normalize_unicode_for_match(value).rstrip(),
        )
        if line_unicode_trimmed is not None:
            return line_unicode_trimmed

    return None


def _build_edit_region_snippet(
    previous: str,
    current: str,
    context_lines: int = 4,
    max_changed_lines: int = 1000,
) -> Optional[Dict[str, Any]]:
    if previous == current:
        return None

    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    total_lines = len(curr_lines)
    if total_lines == 0:
        return {
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "content": "(file is now empty)",
            "too_large": False,
        }

    first_diff = 0
    limit = min(len(prev_lines), len(curr_lines))
    while first_diff < limit and prev_lines[first_diff] == curr_lines[first_diff]:
        first_diff += 1

    prev_idx = len(prev_lines) - 1
    curr_idx = len(curr_lines) - 1
    while prev_idx >= first_diff and curr_idx >= first_diff and prev_lines[prev_idx] == curr_lines[curr_idx]:
        prev_idx -= 1
        curr_idx -= 1

    changed_start = first_diff
    changed_end = max(changed_start, curr_idx)
    changed_count = changed_end - changed_start + 1

    if changed_count > max_changed_lines:
        return {
            "start_line": changed_start + 1,
            "end_line": changed_end + 1,
            "total_lines": total_lines,
            "content": "(changed region too large for inline snippet)",
            "too_large": True,
        }

    snippet_start = max(0, changed_start - context_lines)
    snippet_end = min(total_lines - 1, changed_end + context_lines)
    lines = [f"{idx + 1:4}| {curr_lines[idx]}" for idx in range(snippet_start, snippet_end + 1)]
    return {
        "start_line": snippet_start + 1,
        "end_line": snippet_end + 1,
        "total_lines": total_lines,
        "content": "\n".join(lines),
        "too_large": False,
    }


def _append_region_snippet(lines: List[str], previous: str, current: str) -> bool:
    snippet = _build_edit_region_snippet(previous, current)
    if snippet is None:
        return False
    lines.append(
        f"Showing lines {snippet['start_line']}-{snippet['end_line']} of {snippet['total_lines']}:"
    )
    lines.append(snippet["content"])
    return True


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


def _companion_spec_paths(path: str) -> List[str]:
    base, ext = os.path.splitext(path)
    if ext.lower() not in {".js", ".mjs", ".ts"}:
        return []
    candidates = [
        f"{base}.spec{ext}",
        f"{base}.test{ext}",
    ]
    return [candidate for candidate in candidates if os.path.exists(candidate)]


def _spec_focus_payload(path: str) -> Optional[Dict[str, Any]]:
    companion_specs = _companion_spec_paths(path)
    if not companion_specs:
        return None

    spec_path = companion_specs[0]
    try:
        with open(spec_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        return None

    title_re = re.compile(
        r"\b(?:x?test|x?it)\s*\(\s*([\"'])(?P<title>.+?)\1",
        re.IGNORECASE | re.DOTALL,
    )

    titles: List[str] = []
    seen = set()
    for match in title_re.finditer(content):
        title = " ".join(match.group("title").split())
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
        if len(titles) >= 48:
            break

    if not titles:
        return None

    selected: List[str] = []
    for item in titles[:2]:
        if item not in selected:
            selected.append(item)
    for item in titles[-2:]:
        if item not in selected:
            selected.append(item)

    shortened = [item[:90] + ("..." if len(item) > 90 else "") for item in selected]
    return {
        "spec": to_display_path(spec_path),
        "tests": len(titles),
        "focus": shortened,
    }


def _spec_focus_hint_from_payload(payload: Optional[Dict[str, Any]]) -> str:
    if not payload:
        return ""
    return "spec_focus: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _extract_js_imported_symbols(spec_content: str, source_basename: str) -> List[str]:
    pattern = re.compile(
        r"import\s*\{\s*(?P<names>[^}]+)\s*\}\s*from\s*['\"](?P<module>[^'\"]+)['\"]",
        re.IGNORECASE,
    )
    symbols: List[str] = []
    seen = set()
    for match in pattern.finditer(spec_content):
        module = match.group("module").strip()
        module_base = os.path.basename(module)
        if module_base != source_basename and module_base != f"{source_basename}.js":
            continue
        names_part = match.group("names")
        for raw_name in names_part.split(","):
            name = raw_name.strip()
            if not name:
                continue
            if " as " in name:
                name = name.split(" as ", 1)[1].strip()
            if not name or name in seen:
                continue
            seen.add(name)
            symbols.append(name)
    return symbols


def _extract_spec_called_api(spec_content: str, imported_symbols: List[str]) -> Tuple[List[str], List[str]]:
    if not imported_symbols:
        return [], []
    method_calls: List[str] = []
    function_calls: List[str] = []
    seen_methods = set()
    seen_functions = set()
    for symbol in imported_symbols:
        escaped = re.escape(symbol)
        method_patterns = [
            rf"\bnew\s+{escaped}\s*\([^)]*\)\s*\.\s*([A-Za-z_]\w*)\s*\(",
            rf"\b{escaped}\s*\.\s*([A-Za-z_]\w*)\s*\(",
        ]
        for pat in method_patterns:
            for m in re.finditer(pat, spec_content):
                name = m.group(1)
                if name and name not in seen_methods:
                    seen_methods.add(name)
                    method_calls.append(name)
        for m in re.finditer(rf"(?<!\.)\b{escaped}\s*\(", spec_content):
            prefix = spec_content[max(0, m.start() - 8):m.start()]
            if re.search(r"\bnew\s+$", prefix):
                continue
            full = m.group(0)
            if full and symbol not in seen_functions:
                seen_functions.add(symbol)
                function_calls.append(symbol)
    return method_calls, function_calls


def _spec_contract_hint(path: str, source_content: str) -> str:
    companion_specs = _companion_spec_paths(path)
    if not companion_specs:
        return ""
    spec_path = companion_specs[0]
    try:
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec_content = fh.read()
    except Exception:
        return ""

    source_basename = os.path.splitext(os.path.basename(path))[0]
    imported_symbols = _extract_js_imported_symbols(spec_content, source_basename)
    methods, functions = _extract_spec_called_api(spec_content, imported_symbols)
    if not methods and not functions:
        return ""

    missing_methods: List[str] = []
    for method in methods:
        if not re.search(rf"\b{re.escape(method)}\s*\(", source_content):
            missing_methods.append(method)

    missing_functions: List[str] = []
    for fn_name in functions:
        if re.search(rf"\b(?:export\s+)?class\s+{re.escape(fn_name)}\b", source_content):
            continue
        if not re.search(
            rf"\b(?:export\s+)?(?:async\s+)?function\s+{re.escape(fn_name)}\s*\(",
            source_content,
        ):
            if not re.search(
                rf"\b(?:export\s+)?(?:const|let|var)\s+{re.escape(fn_name)}\s*=\s*",
                source_content,
            ):
                missing_functions.append(fn_name)

    payload = {
        "spec": to_display_path(spec_path),
        "api_calls": len(methods) + len(functions),
        "missing_methods": missing_methods[:8],
        "missing_functions": missing_functions[:8],
    }
    return "spec_contract: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _write_read_precondition_error(path: str) -> Optional[str]:
    if not _enforce_read_before_write_enabled():
        return None
    if not os.path.exists(path):
        return None
    display_path = to_display_path(path)
    if path not in FILE_VERSIONS:
        return (
            f"error: write requires reading current file first: {display_path}.\n"
            f"Action: read({{\"path\":\"{display_path}\"}}), then retry write."
        )

    missing_specs = [spec for spec in _companion_spec_paths(path) if spec not in FILE_VERSIONS]
    if missing_specs:
        missing_display = ", ".join(to_display_path(spec) for spec in missing_specs)
        return (
            "error: write requires reading companion spec/test file first.\n"
            f"Missing: {missing_display}\n"
            "Action: read each missing spec/test file, then retry write."
        )
    return None


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


def _changed_symbols_line(symbols: List[str]) -> str:
    if not symbols:
        return "changed_symbols: -"
    return "changed_symbols: " + ", ".join(symbols[:10])


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

    precondition_error = _write_read_precondition_error(path)
    if precondition_error:
        return precondition_error

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
    spec_focus_payload = _spec_focus_payload(path) if _write_spec_focus_enabled() else None
    spec_focus = _spec_focus_hint_from_payload(spec_focus_payload)
    spec_contract = _spec_contract_hint(path, content) if _write_spec_contract_enabled() else ""
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
        symbols_line = _changed_symbols_line(list(mutation.get("changed_symbols") or []))
        if _write_verbose_state_enabled():
            lines: List[str] = [
                f"ok: created {display_path}, +{additions} lines",
                file_state,
                decision_hint,
                state_brief,
                state_line,
                symbols_line,
            ]
            if _write_success_snippet_enabled():
                _append_region_snippet(lines, "", content)
            else:
                lines.append(_changed_line_preview("", content))
            if loop_hint.strip():
                lines.append(loop_hint.strip())
            if spec_inject.strip():
                lines.append(spec_inject.strip())
            if spec_focus.strip():
                lines.append(spec_focus.strip())
            if spec_contract.strip():
                lines.append(spec_contract.strip())
            if write_hint.strip():
                lines.append(write_hint.strip())
            return "\n".join(lines)

        out: List[str] = [
            f"ok: created {display_path}, +{additions} lines",
            file_state,
            _change_summary("", content),
            symbols_line,
        ]
        if _write_success_snippet_enabled():
            _append_region_snippet(out, "", content)
        else:
            out.append(_changed_line_preview("", content))
        if loop_hint.strip():
            out.append(loop_hint.strip())
        if spec_inject.strip():
            out.append(spec_inject.strip())
        if spec_focus.strip():
            out.append(spec_focus.strip())
        if spec_contract.strip():
            out.append(spec_contract.strip())
        if write_hint.strip():
            out.append(write_hint.strip())
        return "\n".join(out)

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
    symbols_line = _changed_symbols_line(symbols)
    snippet_lines: List[str] = []
    if _write_success_snippet_enabled():
        _append_region_snippet(snippet_lines, old_content, content)
    changed_preview = _changed_line_preview(old_content, content)
    if _write_verbose_state_enabled():
        lines: List[str] = [
            f"ok: updated {display_path}, +{additions} -{removals} lines",
            file_state,
            decision_hint,
            state_brief,
            state_line,
            summary,
            symbols_line,
        ]
        if snippet_lines:
            lines.extend(snippet_lines)
        else:
            lines.append(changed_preview)
        if loop_hint.strip():
            lines.append(loop_hint.strip())
        if spec_inject.strip():
            lines.append(spec_inject.strip())
        if spec_focus.strip():
            lines.append(spec_focus.strip())
        if spec_contract.strip():
            lines.append(spec_contract.strip())
        if write_hint.strip():
            lines.append(write_hint.strip())
        return "\n".join(lines)

    out: List[str] = [
        f"ok: updated {display_path}, +{additions} -{removals} lines",
        file_state,
        summary,
        symbols_line,
    ]
    if snippet_lines:
        out.extend(snippet_lines)
    else:
        out.append(changed_preview)
    if loop_hint.strip():
        out.append(loop_hint.strip())
    if spec_inject.strip():
        out.append(spec_inject.strip())
    if spec_focus.strip():
        out.append(spec_focus.strip())
    if spec_contract.strip():
        out.append(spec_contract.strip())
    if write_hint.strip():
        out.append(write_hint.strip())
    return "\n".join(out)


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
                "Use a different 'new' value, or provide a larger old/new block for broader edits.\n"
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

    resolved_old = old
    if old not in text:
        resolved_old = _resolve_old_text(text, old)
    if resolved_old is None:
        read_hint = ""
        if path not in FILE_VERSIONS:
            read_hint = " Hint: reading the file first often avoids stale/guessed context."
        return (
            f"error: old text was not found in {basename}.\n"
            "This usually means whitespace, line-break, or Unicode punctuation mismatch.\n"
            f"Here is the current content of {basename}:\n{text}\n"
            f"Action: copy the exact text (including whitespace) from above, then retry edit with a larger exact old/new block if needed.{read_hint}"
        )

    count = text.count(resolved_old)
    if not args.get("all") and count > 1:
        return (
            f"error: 'old' text appears {count} times in {basename}; it must be unique. "
            f"Include more surrounding lines in 'old' to make it unique, or set all=true to replace all occurrences."
        )

    replacement = (
        text.replace(resolved_old, new)
        if args.get("all")
        else text.replace(resolved_old, new, 1)
    )
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

    lines: List[str] = [
        f"ok: updated {display_path}. {replacement_count} replacement(s).",
    ]

    if _edit_success_snippet_enabled():
        snippet = _build_edit_region_snippet(text, replacement)
        if snippet is not None:
            lines.append(
                f"Showing lines {snippet['start_line']}-{snippet['end_line']} of {snippet['total_lines']}:"
            )
            lines.append(snippet["content"])

    lines.append(summary)

    if _edit_verbose_state_enabled():
        file_state = (
            f"file_state: lines={_content_line_count(replacement)} "
            f"chars={len(replacement)} sha256={_content_digest(replacement)}"
        )
        lines.append(file_state)
        lines.append(decision_hint)
        lines.append(state_brief)
        lines.append(state_line)

    lines.append("Action: if this satisfies requirements, call finish; otherwise make the next targeted edit.")
    return "\n".join(lines)
