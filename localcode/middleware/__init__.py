"""
Default middleware for localcode agent.

Call install_defaults() at startup to register all built-in hooks.
"""

from localcode.middleware import logging_hook, feedback_hook, metrics_hook, conversation_dump


def install_defaults(log_path=None, run_context=None, tools_dict=None, display_map=None):
    """Register all default middleware hooks. Returns installed components.

    Args:
        log_path: Path for JSONL logging (optional, logging hook will set up its own if None).
        run_context: Dict with run_name, task_id, agent_name etc. for log enrichment.
        tools_dict: Tool definitions dict for feedback hook.
        display_map: Tool display name map for feedback hook.

    Returns:
        Dict with references to installed components (e.g. metrics collector).
    """
    logging_hook.install(log_path=log_path, run_context=run_context or {})
    feedback_hook.install(tools_dict=tools_dict, display_map=display_map)
    collector = metrics_hook.install()
    conversation_dump.install()

    return {
        "metrics": collector,
    }
