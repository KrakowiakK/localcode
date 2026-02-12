"""
Conversation dump middleware — writes raw JSON and pretty log files.

Extracts the conversation dump logic from run_agent's final content path.
Registers on 'agent_end' event to dump the full conversation.
"""

import json
import os
from typing import Any, Dict, List, Optional

from localcode import hooks


def _load_phase_events(log_path: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not log_path or not os.path.exists(log_path):
        return events
    try:
        with open(log_path, "r", encoding="utf-8") as lf:
            for line in lf:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") in {"phase_state", "phase_transition", "phase_probe_error", "phase_probe_parse_error"}:
                    events.append(rec)
    except Exception:
        return events
    return events


def _json_safe(value: Any) -> Any:
    """Convert arbitrary payloads to JSON-serializable data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


def _dump_conversation(
    log_path: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    phase_log_mode: Optional[str] = None,
    last_request_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """Dump last model request + full conversation snapshots and pretty log.

    Returns dict with 'raw' and 'pretty' paths, or None on failure.
    """
    base_path = log_path.rsplit(".", 1)[0]
    full_conv = [{"role": "system", "content": system_prompt}] + list(messages)
    full_conv_safe = _json_safe(full_conv)
    request_snapshot_safe = _json_safe(last_request_snapshot) if isinstance(last_request_snapshot, dict) else None

    raw_path = base_path + ".raw.json"
    history_raw_path = base_path + ".history.raw.json"
    pretty_path = base_path + ".log"

    # 1) Raw JSON (exact last request payload when available)
    request_dump_written = isinstance(request_snapshot_safe, dict) and bool(request_snapshot_safe)
    history_dump_written = False
    if request_dump_written:
        try:
            with open(raw_path, "w", encoding="utf-8") as qf:
                json.dump(request_snapshot_safe, qf, indent=2, ensure_ascii=False)
        except Exception:
            request_dump_written = False
    if request_dump_written:
        try:
            with open(history_raw_path, "w", encoding="utf-8") as hf:
                json.dump(full_conv_safe, hf, indent=2, ensure_ascii=False)
            history_dump_written = True
        except Exception:
            history_dump_written = False
    else:
        try:
            with open(raw_path, "w", encoding="utf-8") as rf:
                json.dump(full_conv_safe, rf, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # 2) Pretty human-readable
    try:
        with open(pretty_path, "w", encoding="utf-8") as cf:
            for i, msg in enumerate(full_conv):
                role = msg.get("role", "?")
                cf.write(f"{'='*60}\n")
                cf.write(f"[{i}] {role.upper()}")
                if msg.get("tool_call_id"):
                    cf.write(f"  (tool_call_id: {msg['tool_call_id']})")
                cf.write(f"\n{'='*60}\n\n")
                for tk in ("thinking", "reasoning_content"):
                    if msg.get(tk):
                        cf.write(f"--- THINKING ---\n{msg[tk]}\n--- /THINKING ---\n\n")
                content_val = msg.get("content")
                if content_val:
                    cf.write(f"{content_val}\n\n")
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    cf.write(f">>> TOOL CALL: {fn.get('name', '?')}  (id: {tc.get('id', '?')})\n")
                    args_str = fn.get("arguments", "")
                    try:
                        args_obj = json.loads(args_str) if isinstance(args_str, str) else args_str
                        cf.write(json.dumps(args_obj, indent=2, ensure_ascii=False))
                    except (json.JSONDecodeError, TypeError):
                        cf.write(str(args_str))
                    cf.write(f"\n\n")
            phase_mode = str(phase_log_mode or "").strip().lower()
            if phase_mode in {"log", "both"}:
                phase_events = _load_phase_events(log_path)
                if phase_events:
                    cf.write(f"{'='*60}\nPHASE EVENTS\n{'='*60}\n\n")
                    for ev in phase_events:
                        ts = ev.get("ts", "")
                        event = ev.get("event", "")
                        payload = {k: v for k, v in ev.items() if k not in {"ts", "event"}}
                        cf.write(f"[{ts}] {event} {json.dumps(payload, ensure_ascii=False)}\n")
                    cf.write("\n")
            cf.write(f"{'='*60}\nEND ({len(full_conv)} messages)\n{'='*60}\n")
    except Exception:
        pass

    out = {"raw": raw_path, "pretty": pretty_path, "message_count": len(full_conv)}
    if request_dump_written:
        out["request_raw"] = raw_path
    if history_dump_written:
        out["history_raw"] = history_raw_path
    return out


def on_agent_end(data: Dict[str, Any]) -> Dict[str, Any]:
    """Dump conversation on agent_end if log_path is available."""
    log_path = data.get("log_path")
    if not log_path:
        return data

    system_prompt = data.get("system_prompt", "")
    messages = data.get("messages", [])
    phase_log_mode = data.get("phase_log_mode")
    last_request_snapshot = data.get("last_request_snapshot")

    result = _dump_conversation(
        log_path,
        system_prompt,
        messages,
        phase_log_mode=phase_log_mode,
        last_request_snapshot=last_request_snapshot if isinstance(last_request_snapshot, dict) else None,
    )
    if result:
        data["conversation_dump"] = result

    return data


def install() -> None:
    """Register conversation dump hook on agent_end event."""
    hooks.register("agent_end", on_agent_end)
