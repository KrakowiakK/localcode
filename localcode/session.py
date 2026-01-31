"""Session management — persistence, logging initialization, run inference.

Functions for saving/loading conversation sessions, initializing JSONL logging,
and inferring run metadata from file paths.
"""

import glob as globlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from localcode import hooks
from localcode.middleware import logging_hook


def infer_run_name_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:
        p = Path(path)
    bench_root = os.environ.get("AIDER_BENCHMARK_DIR")
    if bench_root:
        try:
            root = Path(bench_root).resolve()
            if root in p.parents:
                rel = p.relative_to(root)
                if rel.parts:
                    return rel.parts[0]
        except Exception:
            pass
    for part in p.parts:
        if "--localcode-" in part or "--nanocode-" in part:
            return part
    for part in p.parts:
        if len(part) >= 10 and part[4] == "-" and part[7] == "-" and "--" in part:
            return part
    return None


def infer_task_id_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:
        p = Path(path)
    if p.name.startswith("prompt_try"):
        return p.parent.name
    return None


def summarize_messages(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    roles = [m.get("role") for m in messages]
    tool_messages = sum(1 for m in messages if m.get("role") == "tool")
    total_chars = sum(len(m.get("content") or "") for m in messages)
    reasoning_chars = sum(len(m.get("reasoning_content") or "") for m in messages)
    reasoning_messages = sum(1 for m in messages if m.get("reasoning_content"))
    return {
        "message_count": len(messages),
        "roles": roles,
        "tool_message_count": tool_messages,
        "total_chars": total_chars,
        "reasoning_message_count": reasoning_messages,
        "reasoning_chars": reasoning_chars,
    }


def create_new_session_path(agent_name: str, session_dir: str) -> str:
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(session_dir, f"{timestamp}_{agent_name}.json")


def find_latest_session(agent_name: str, session_dir: str) -> Optional[str]:
    os.makedirs(session_dir, exist_ok=True)
    pattern = os.path.join(session_dir, f"*_{agent_name}.json")
    files = globlib.glob(pattern)
    if not files:
        return None
    files.sort(reverse=True)
    return files[0]


def init_logging(
    log_dir: str,
    agent_name: Optional[str],
    model: str,
    agent_settings: Dict[str, Any],
    run_name: Optional[str] = None,
    task_id: Optional[str] = None,
    task_index: Optional[int] = None,
    task_total: Optional[int] = None,
) -> None:
    """Initialize JSONL logging via logging_hook."""
    if logging_hook.get_log_path():
        return
    logging_hook.init_logging(log_dir, agent_name)
    sync_logging_context(
        agent_name=agent_name,
        run_name=run_name,
        task_id=task_id,
        task_index=task_index,
        task_total=task_total,
    )
    logging_hook.log_event("session_start", {
        "model": model,
        "cwd": os.getcwd(),
        "log_path": logging_hook.get_log_path(),
        "mode": "single_agent_native_tools",
        "agent": agent_name,
        "agent_settings": agent_settings,
    })


def sync_logging_context(
    agent_name: Optional[str] = None,
    run_name: Optional[str] = None,
    task_id: Optional[str] = None,
    task_index: Optional[int] = None,
    task_total: Optional[int] = None,
) -> None:
    """Sync global state into logging_hook run context."""
    ctx: Dict[str, Any] = {}
    if run_name:
        ctx["run_name"] = run_name
    if task_id:
        ctx["task_id"] = task_id
    if task_index:
        ctx["task_index"] = task_index
    if task_total:
        ctx["task_total"] = task_total
    if agent_name:
        ctx["agent"] = agent_name
    logging_hook.update_run_context(ctx)


def save_session(
    agent_name: str,
    messages: List[Dict[str, Any]],
    model: str,
    session_path: str,
) -> None:
    os.makedirs(os.path.dirname(session_path), exist_ok=True)

    created = None
    if os.path.exists(session_path):
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            created = existing.get("created")
        except Exception:
            created = None

    session_data = {
        "agent": agent_name,
        "model": model,
        "messages": messages,
        "created": created or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Hook: session_save (read-only notification)
    hooks.emit("session_save", {"messages": messages, "path": session_path})

    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    logging_hook.log_event("session_saved", {"path": session_path, "message_count": len(messages)})


def load_session(agent_name: str, session_dir: str) -> tuple:
    """Load the latest session for the given agent.

    Returns (messages, session_path) tuple. If no session found,
    returns ([], None).
    """
    latest = find_latest_session(agent_name, session_dir)
    if not latest:
        return [], None
    try:
        with open(latest, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        msgs = session_data.get("messages", [])
        logging_hook.log_event("session_loaded", {"path": latest, "message_count": len(msgs)})
        return msgs, latest
    except Exception as e:
        logging_hook.log_event("session_load_error", {"path": latest, "error": str(e)})
        return [], None


def init_new_session(agent_name: str, session_dir: str) -> str:
    """Create a new session path and return it."""
    return create_new_session_path(agent_name, session_dir)
