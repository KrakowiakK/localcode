"""
Metrics middleware — tool call counting, error counting, timing.

Extracts counter variables from run_agent into a MetricsCollector class.
"""

import time
from collections import Counter
from typing import Any, Dict, Optional

from localcode import hooks


class MetricsCollector:
    """Collects tool call metrics throughout an agent run."""

    def __init__(self):
        self.tool_calls_total: int = 0
        self.tool_errors_total: int = 0
        self.tool_call_counts: Dict[str, int] = {}
        self.tool_error_counts: Dict[str, int] = {}
        self.feedback_counts: Counter = Counter()
        self.patch_fail_count: Dict[str, int] = {}
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.analysis_retries: int = 0

    def reset(self) -> None:
        """Reset all counters for a new run."""
        self.tool_calls_total = 0
        self.tool_errors_total = 0
        self.tool_call_counts.clear()
        self.tool_error_counts.clear()
        self.feedback_counts.clear()
        self.patch_fail_count.clear()
        self.start_time = None
        self.end_time = None
        self.analysis_retries = 0

    def on_agent_start(self, data: Dict[str, Any]) -> None:
        self.reset()
        self.start_time = time.time()

    def on_tool_after(self, data: Dict[str, Any]) -> None:
        tool_name = data.get("tool_name", "unknown")
        self.tool_calls_total += 1
        self.tool_call_counts[tool_name] = self.tool_call_counts.get(tool_name, 0) + 1

        if data.get("is_error"):
            self.tool_errors_total += 1
            self.tool_error_counts[tool_name] = self.tool_error_counts.get(tool_name, 0) + 1

        feedback_reason = data.get("feedback_reason")
        if feedback_reason:
            self.feedback_counts[feedback_reason] += 1

        # Track patch failures per file
        if tool_name in ("apply_patch", "patch_files") and data.get("is_error"):
            path = data.get("path_value")
            if path:
                self.patch_fail_count[path] = self.patch_fail_count.get(path, 0) + 1

    def on_tool_success(self, data: Dict[str, Any]) -> None:
        """Clear patch fail count on successful patch."""
        tool_name = data.get("tool_name", "")
        if tool_name in ("apply_patch", "patch_files") and not data.get("is_error"):
            path = data.get("path_value")
            if path:
                self.patch_fail_count.pop(path, None)

    def on_agent_end(self, data: Dict[str, Any]) -> None:
        self.end_time = time.time()

    def summary(self) -> Dict[str, Any]:
        """Return summary dict compatible with LAST_RUN_SUMMARY."""
        result: Dict[str, Any] = {
            "tool_calls_total": self.tool_calls_total,
            "tool_errors_total": self.tool_errors_total,
            "tool_call_counts": dict(self.tool_call_counts),
            "tool_error_counts": dict(self.tool_error_counts),
            "analysis_retries": self.analysis_retries,
        }
        if self.start_time and self.end_time:
            result["duration_seconds"] = round(self.end_time - self.start_time, 2)
        if self.feedback_counts:
            result["feedback_counts"] = dict(self.feedback_counts)
        return result

    def get_patch_fail_count(self, path: str) -> int:
        """Get patch failure count for a specific file path."""
        return self.patch_fail_count.get(path, 0)

    def record_patch_fail(self, path: str) -> int:
        """Record a patch failure for a path, return new count."""
        self.patch_fail_count[path] = self.patch_fail_count.get(path, 0) + 1
        return self.patch_fail_count[path]

    def clear_patch_fail(self, path: str) -> None:
        """Clear patch failure count on success."""
        self.patch_fail_count.pop(path, None)


def install() -> MetricsCollector:
    """Register metrics hooks and return the collector instance."""
    collector = MetricsCollector()
    hooks.register("agent_start", collector.on_agent_start)
    hooks.register("tool_after", collector.on_tool_after)
    hooks.register("tool_after", collector.on_tool_success)
    hooks.register("agent_end", collector.on_agent_end)
    return collector
