# Middleware / Hook Architecture

Event-driven hook system for `localcode.py`. Cross-cutting concerns (logging,
error feedback, metrics, conversation dumps) are extracted into drop-in middleware
modules that register callbacks on lifecycle events.

## Structure

```
localcode/
├── hooks.py                  # Hook registry (register/emit/clear)
├── middleware/
│   ├── __init__.py           # install_defaults() entry point
│   ├── logging_hook.py       # JSONL event logging
│   ├── feedback_hook.py      # Tool error feedback rules (20 rules)
│   ├── metrics_hook.py       # MetricsCollector (counters, timing)
│   └── conversation_dump.py  # .raw.json + .log conversation export
└── tests/
    └── test_hooks.py         # 30 tests (hooks + all middleware)
```

## Hook Registry

```python
from localcode import hooks

# Register a callback for an event
hooks.register("tool_after", my_callback)

# Emit an event — data flows through all registered callbacks
data = hooks.emit("tool_after", {"tool_name": "read", "is_error": False})

# Callbacks receive a dict, optionally return a modified dict
def my_callback(data):
    data["extra"] = True
    return data
```

## Lifecycle Events

| Event | When | Mutable |
|-------|------|---------|
| `agent_start` | Beginning of `run_agent()` | no |
| `agent_end` | End of `run_agent()` | no |
| `turn_start` | Top of main loop iteration | no |
| `turn_end` | After processing tool results | no |
| `api_request` | Before API call | yes |
| `api_response` | After API response | no |
| `api_error` | On API error | no |
| `tool_before` | Before tool execution | yes |
| `tool_after` | After tool execution | yes |
| `tool_feedback` | Error feedback generated | yes |
| `response_content` | Model returns content | no |
| `session_save` | Before session save | no |

## Middleware Modules

### logging_hook

JSONL structured logging with run context enrichment.

```python
from localcode.middleware import logging_hook
logging_hook.init_logging(log_dir, agent_name)
logging_hook.log_event("my_event", {"key": "value"})
```

### feedback_hook

Declarative rule table for tool error feedback. Replaces ~360 lines of
inline if/elif logic.

```python
from localcode.middleware import feedback_hook
feedback_hook.install(tools_dict=tools, display_map=display_map)
# Automatically sets feedback_text/feedback_reason on tool_after events
```

### metrics_hook

Counters and timing via `MetricsCollector`.

```python
from localcode.middleware import metrics_hook
collector = metrics_hook.install()
# ... after agent runs ...
print(collector.summary())  # {tool_calls_total, tool_errors_total, duration_seconds, ...}
```

### conversation_dump

Exports full conversation to `.raw.json` (machine-readable) and `.log`
(human-readable) on `agent_end`.

## Adding a New Hook

1. Create `localcode/middleware/my_hook.py`
2. Define callback functions matching `(data: dict) -> dict | None`
3. Add `install()` function that calls `hooks.register(event, callback)`
4. Import and call `install()` in `middleware/__init__.py`
5. Add tests in `tests/test_hooks.py`
