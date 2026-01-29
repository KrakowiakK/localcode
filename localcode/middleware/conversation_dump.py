"""
Conversation dump middleware — writes raw JSON and pretty log files.

Extracts the conversation dump logic from run_agent's final content path.
Registers on 'agent_end' event to dump the full conversation.
"""

import json
from typing import Any, Dict, List, Optional

from localcode import hooks


def _dump_conversation(
    log_path: str,
    system_prompt: str,
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Dump full conversation to .raw.json and .log files.

    Returns dict with 'raw' and 'pretty' paths, or None on failure.
    """
    base_path = log_path.rsplit(".", 1)[0]
    full_conv = [{"role": "system", "content": system_prompt}] + list(messages)

    raw_path = base_path + ".raw.json"
    pretty_path = base_path + ".log"

    # 1) Raw JSON
    try:
        with open(raw_path, "w", encoding="utf-8") as rf:
            json.dump(full_conv, rf, indent=2, ensure_ascii=False)
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
            cf.write(f"{'='*60}\nEND ({len(full_conv)} messages)\n{'='*60}\n")
    except Exception:
        pass

    return {"raw": raw_path, "pretty": pretty_path, "message_count": len(full_conv)}


def on_agent_end(data: Dict[str, Any]) -> Dict[str, Any]:
    """Dump conversation on agent_end if log_path is available."""
    log_path = data.get("log_path")
    if not log_path:
        return data

    system_prompt = data.get("system_prompt", "")
    messages = data.get("messages", [])

    result = _dump_conversation(log_path, system_prompt, messages)
    if result:
        data["conversation_dump"] = result

    return data


def install() -> None:
    """Register conversation dump hook on agent_end event."""
    hooks.register("agent_end", on_agent_end)
