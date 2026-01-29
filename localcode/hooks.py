"""
Hook registry for localcode lifecycle events.

External modules register callbacks for named events. The core agent emits
events at key lifecycle points; registered hooks receive event data and can
optionally modify it (for mutable events).

Usage:
    from localcode import hooks

    def my_hook(data):
        print(data["turn"])
        return data  # return modified data, or None to keep original

    hooks.register("turn_start", my_hook)
"""

from typing import Any, Callable, Dict, List

HookCallback = Callable[[Dict[str, Any]], Any]

_hooks: Dict[str, List[HookCallback]] = {}


def register(event: str, callback: HookCallback) -> None:
    """Register a callback for a named event."""
    _hooks.setdefault(event, []).append(callback)


def emit(event: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Emit event, passing data through each hook. Hooks can mutate data
    by returning a dict; returning None keeps data unchanged."""
    for cb in _hooks.get(event, []):
        result = cb(data)
        if isinstance(result, dict):
            data = result
    return data


def clear() -> None:
    """Remove all registered hooks."""
    _hooks.clear()


def registered_events() -> List[str]:
    """Return list of events that have at least one hook registered."""
    return [ev for ev, cbs in _hooks.items() if cbs]
