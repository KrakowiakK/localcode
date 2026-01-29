"""Proxy module that exposes localcode.py attributes."""

_agent = None


def _load_agent():
    global _agent
    if _agent is None:
        from . import localcode as _m
        _agent = _m
    return _agent


def __getattr__(name):
    # Allow direct submodule access (hooks, middleware) without loading localcode.py
    if name in ("hooks", "middleware"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    return getattr(_load_agent(), name)


def __setattr__(name, value):
    if name == "_agent":
        # Allow setting the module-level _agent cache
        globals()["_agent"] = value
        return
    setattr(_load_agent(), name, value)
    # Sync SANDBOX_ROOT to _state (single source of truth for tool handlers)
    if name == "SANDBOX_ROOT":
        from localcode.tool_handlers import _state
        _state.SANDBOX_ROOT = value


def __dir__():
    return sorted(set(dir(_load_agent())))


# __all__ is evaluated lazily via __dir__ when needed
