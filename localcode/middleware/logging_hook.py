"""
Logging middleware — writes JSONL event log entries for all lifecycle events.

Replaces the log_event() function and init_logging() from localcode.py.
Registers hooks for every lifecycle event and writes structured JSONL entries.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from localcode import hooks

# Module state
_log_path: Optional[str] = None
_run_context: Dict[str, Any] = {}

# All events we listen to
_ALL_EVENTS = [
    "agent_start", "agent_end",
    "turn_start", "turn_end",
    "api_request", "api_response", "api_error",
    "tool_before", "tool_after", "tool_feedback",
    "response_content",
    "session_save",
    "format_retry",
]


def get_log_path() -> Optional[str]:
    """Return current log path (for use by other modules)."""
    return _log_path


def set_log_path(path: str) -> None:
    """Set log path explicitly (e.g. from init_logging in localcode.py)."""
    global _log_path
    _log_path = path


def _write_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Write a single JSONL log entry. Compatible with original log_event() format."""
    if not _log_path:
        return
    rec: Dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event_type}
    # Enrich with run context
    for key in ("run_name", "task_id", "task_index", "task_total", "agent"):
        val = _run_context.get(key)
        if val:
            rec[key] = val
    if payload:
        rec.update(payload)
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    with open(_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _on_event(event_name: str):
    """Create a hook callback that logs the event data."""
    def callback(data: Dict[str, Any]) -> None:
        # Extract log-specific payload, filtering out large/internal fields
        payload = {}
        for k, v in data.items():
            # Skip very large fields from being logged directly
            if k in ("messages", "request_data", "response"):
                continue
            payload[k] = v
        _write_event(event_name, payload)
    return callback


def log_event(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Direct log_event call — backward compatible wrapper.

    Can be used by code that hasn't been migrated to hooks.emit() yet.
    """
    _write_event(event_type, payload)


def init_logging(log_dir: str, agent_name: Optional[str] = None) -> str:
    """Initialize logging, create log file path. Returns the log path.

    This replaces the original init_logging() from localcode.py.
    """
    global _log_path
    if _log_path:
        return _log_path
    os.makedirs(log_dir, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    safe_agent = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_name or "agent")
    _log_path = os.path.join(log_dir, f"localcode_{safe_agent}_{timestamp}.jsonl")
    return _log_path


def update_run_context(context: Dict[str, Any]) -> None:
    """Update run context fields (run_name, task_id, agent, etc.)."""
    _run_context.update(context)


def install(log_path: Optional[str] = None, run_context: Optional[Dict[str, Any]] = None) -> None:
    """Register logging hooks for all lifecycle events."""
    global _log_path
    if log_path:
        _log_path = log_path
    if run_context:
        _run_context.update(run_context)

    for event in _ALL_EVENTS:
        hooks.register(event, _on_event(event))
