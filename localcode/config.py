"""Config loading & CLI parsing utilities.

Pure functions with no dependencies on agent state.
"""

import json
from typing import Any, Dict, List, Tuple


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def normalize_bool_auto(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() == "auto":
        return None
    raise ValueError(f"Agent config '{field_name}' must be a boolean or 'auto'")


def is_tool_choice_required(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "required"
    if isinstance(value, dict):
        return value.get("type") == "function"
    return False


def _coerce_cli_value(raw: str, existing: Any, key_name: str) -> Any:
    value = raw.strip()
    lowered = value.lower()
    if lowered in {"none", "null"}:
        return None

    if existing is None:
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    if isinstance(existing, bool):
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered == "auto":
            return "auto"
        raise SystemExit(f"Invalid boolean for --{key_name}: {raw}")

    if isinstance(existing, int) and not isinstance(existing, bool):
        try:
            return int(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid integer for --{key_name}: {raw}") from exc

    if isinstance(existing, float):
        try:
            return float(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid float for --{key_name}: {raw}") from exc

    if isinstance(existing, list):
        if value.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON array for --{key_name}: {raw}") from exc
            if not isinstance(parsed, list):
                raise SystemExit(f"Expected JSON array for --{key_name}: {raw}")
            return parsed
        return [item.strip() for item in value.split(",") if item.strip()]

    if isinstance(existing, dict):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON object for --{key_name}: {raw}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit(f"Expected JSON object for --{key_name}: {raw}")
        return parsed

    return value


def apply_cli_overrides(agent_config: Dict[str, Any], extra_args: List[str]) -> Dict[str, Any]:
    if not extra_args:
        return agent_config
    overrides: Dict[str, Any] = {}
    idx = 0
    while idx < len(extra_args):
        arg = extra_args[idx]
        if not arg.startswith("--"):
            raise SystemExit(f"Unexpected argument: {arg}")
        key = arg[2:].replace("-", "_")
        if idx + 1 >= len(extra_args) or extra_args[idx + 1].startswith("--"):
            raise SystemExit(f"Missing value for {arg}")
        raw_value = extra_args[idx + 1]
        overrides[key] = _coerce_cli_value(raw_value, agent_config.get(key), key)
        idx += 2

    merged = dict(agent_config)
    merged.update(overrides)
    return merged


def split_cli_overrides(argv: List[str]) -> Tuple[List[str], List[str]]:
    known_flags = {
        "--agent", "-a",
        "--continue", "-c",
        "--model", "-m",
        "--file", "-f",
        "--url",
        "--temperature", "--top_p", "--top_k", "--min_p",
        "--max_tokens",
        "--no-sandbox",
        "--help", "-h",
    }
    flags_with_values = {
        "--agent", "-a",
        "--model", "-m",
        "--file", "-f",
        "--url",
        "--temperature", "--top_p", "--top_k", "--min_p",
        "--max_tokens",
    }

    filtered: List[str] = []
    overrides: List[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in known_flags:
            filtered.append(arg)
            if arg in flags_with_values:
                if idx + 1 >= len(argv):
                    raise SystemExit(f"Missing value for {arg}")
                filtered.append(argv[idx + 1])
                idx += 2
            else:
                idx += 1
            continue

        if arg.startswith("--"):
            if idx + 1 >= len(argv) or argv[idx + 1].startswith("--"):
                raise SystemExit(f"Missing value for {arg}")
            overrides.extend([arg, argv[idx + 1]])
            idx += 2
            continue

        filtered.append(arg)
        idx += 1

    return filtered, overrides
