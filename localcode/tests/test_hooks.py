"""Tests for localcode.hooks and middleware modules."""

import sys
import os
import json
import tempfile

# Ensure localcode package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from localcode import hooks
from localcode.middleware import feedback_hook, metrics_hook, conversation_dump, logging_hook


class TestHooksRegistry:
    """Tests for the core hook registry."""

    def setup_method(self):
        hooks.clear()

    def test_register_and_emit(self):
        results = []
        hooks.register("test_event", lambda data: results.append(data))
        hooks.emit("test_event", {"key": "value"})
        assert len(results) == 1
        assert results[0] == {"key": "value"}

    def test_emit_no_hooks(self):
        data = hooks.emit("nonexistent", {"key": "value"})
        assert data == {"key": "value"}

    def test_hook_can_mutate_data(self):
        def mutator(data):
            data["extra"] = True
            return data
        hooks.register("mut", mutator)
        result = hooks.emit("mut", {"original": True})
        assert result["original"] is True
        assert result["extra"] is True

    def test_hook_returning_none_keeps_data(self):
        def no_return(data):
            pass  # returns None
        hooks.register("nr", no_return)
        result = hooks.emit("nr", {"key": "value"})
        assert result == {"key": "value"}

    def test_multiple_hooks_chain(self):
        def hook_a(data):
            data["a"] = True
            return data
        def hook_b(data):
            data["b"] = True
            return data
        hooks.register("chain", hook_a)
        hooks.register("chain", hook_b)
        result = hooks.emit("chain", {})
        assert result["a"] is True
        assert result["b"] is True

    def test_clear_removes_all(self):
        hooks.register("ev", lambda d: None)
        assert "ev" in hooks.registered_events()
        hooks.clear()
        assert hooks.registered_events() == []

    def test_registered_events(self):
        hooks.register("alpha", lambda d: None)
        hooks.register("beta", lambda d: None)
        events = hooks.registered_events()
        assert "alpha" in events
        assert "beta" in events


class TestFeedbackHook:
    """Tests for feedback_hook rule matching."""

    def setup_method(self):
        hooks.clear()
        feedback_hook.install(tools_dict=None, display_map=None)

    def test_patch_context_not_found(self):
        data = {
            "tool_name": "apply_patch",
            "result": "error: patch context not found in file.py",
            "is_error": True,
            "path_value": "/foo/bar.py",
            "patch_fail_count": 0,
        }
        result = hooks.emit("tool_after", data)
        assert "feedback_text" in result
        assert "patch context not found" in result["feedback_text"]
        assert result["feedback_reason"] == "patch_context_not_found"

    def test_patch_context_not_found_second_failure(self):
        data = {
            "tool_name": "apply_patch",
            "result": "error: patch context not found in file.py",
            "is_error": True,
            "path_value": "/foo/bar.py",
            "patch_fail_count": 2,
        }
        result = hooks.emit("tool_after", data)
        assert "SECOND FAILURE" in result["feedback_text"]

    def test_old_string_not_found(self):
        data = {
            "tool_name": "edit",
            "result": "error: old_string not found in file",
            "is_error": True,
            "path_value": "/foo/bar.py",
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "old_string_not_found"

    def test_old_text_not_found_variant(self):
        data = {
            "tool_name": "edit",
            "result": "error: old text was not found in foo.js",
            "is_error": True,
            "path_value": "/foo/bar.py",
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "old_string_not_found"

    def test_no_match_on_success(self):
        data = {
            "tool_name": "read",
            "result": "ok: file content...",
            "is_error": False,
        }
        result = hooks.emit("tool_after", data)
        assert "feedback_text" not in result

    def test_write_noop(self):
        data = {
            "tool_name": "write",
            "result": "error: no changes - file already has this content",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "write_noop"

    def test_write_repeated_noop(self):
        data = {
            "tool_name": "write",
            "result": "error: repeated no-op write for react.js. Write different content, or call finish if implementation is already correct.",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "write_repeated_noop"

    def test_write_missing_content(self):
        data = {
            "tool_name": "write",
            "result": "error: missing required parameter(s) for tool 'write': content. Example: write({\"path\": \"...\", \"content\": \"...\"})",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "write_missing_content"

    def test_invalid_regex(self):
        data = {
            "tool_name": "grep",
            "result": "error: invalid regex pattern",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "invalid_regex"

    def test_must_read_before_patching(self):
        data = {
            "tool_name": "apply_patch",
            "result": "error: MUST READ file BEFORE PATCHING",
            "is_error": True,
            "path_value": "/foo.py",
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "must_read_before_patching"

    def test_old_equals_new(self):
        data = {
            "tool_name": "edit",
            "result": "error: no changes - old_string equals new_string",
            "is_error": True,
            "path_value": "/foo.py",
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "old_equals_new"

    def test_old_equals_new_variant(self):
        data = {
            "tool_name": "edit",
            "result": "error: no changes - old equals new in foo.js",
            "is_error": True,
            "path_value": "/foo.py",
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "old_equals_new"

    def test_glob_path_missing(self):
        data = {
            "tool_name": "glob",
            "result": "error: path does not exist",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "glob_path_missing"

    def test_ls_path_missing(self):
        data = {
            "tool_name": "ls",
            "result": "error: directory not found",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "ls_path_missing"

    def test_unknown_tool_name_feedback(self):
        data = {
            "tool_name": "run",
            "result": "error: unknown tool 'run'. Available tools: read, write",
            "is_error": True,
        }
        result = hooks.emit("tool_after", data)
        assert result["feedback_reason"] == "unknown_tool_name"
        assert "unknown tool name" in result["feedback_text"].lower()

    def test_rule_matches_function(self):
        """Test _rule_matches with tuple tool names."""
        from localcode.middleware.feedback_hook import _rule_matches
        rule = {"tool": ("search", "grep"), "match": "path does not exist", "reason": "x", "build": "y"}
        assert _rule_matches(rule, "search", "error: path does not exist")
        assert _rule_matches(rule, "grep", "error: path does not exist")
        assert not _rule_matches(rule, "read", "error: path does not exist")

    def test_rule_matches_with_match_fn(self):
        from localcode.middleware.feedback_hook import _rule_matches
        rule = {"tool": "edit", "match_fn": lambda r: "must read" in r and "before editing" in r, "reason": "x", "build": "y"}
        assert _rule_matches(rule, "edit", "error: must read file before editing")
        assert not _rule_matches(rule, "edit", "error: must read file")

    def test_rule_matches_case_insensitive_match(self):
        from localcode.middleware.feedback_hook import _rule_matches
        rule = {"tool": "read", "match": "file not found", "reason": "x", "build": "y"}
        assert _rule_matches(rule, "read", "ERROR: File Not Found")

    def test_rule_matches_wildcard_tool(self):
        from localcode.middleware.feedback_hook import _rule_matches
        rule = {"tool": "*", "match": "unknown tool", "reason": "x", "build": "y"}
        assert _rule_matches(rule, "run", "error: unknown tool 'run'")


class TestMetricsHook:
    """Tests for metrics_hook MetricsCollector."""

    def setup_method(self):
        hooks.clear()

    def test_install_returns_collector(self):
        collector = metrics_hook.install()
        assert isinstance(collector, metrics_hook.MetricsCollector)

    def test_collector_tracks_tool_calls(self):
        collector = metrics_hook.install()
        hooks.emit("agent_start", {})
        hooks.emit("tool_after", {"tool_name": "read", "is_error": False})
        hooks.emit("tool_after", {"tool_name": "write", "is_error": False})
        hooks.emit("tool_after", {"tool_name": "read", "is_error": True})
        assert collector.tool_calls_total == 3
        assert collector.tool_errors_total == 1
        assert collector.tool_call_counts == {"read": 2, "write": 1}
        assert collector.tool_error_counts == {"read": 1}

    def test_collector_resets_on_agent_start(self):
        collector = metrics_hook.install()
        hooks.emit("tool_after", {"tool_name": "read", "is_error": False})
        assert collector.tool_calls_total == 1
        hooks.emit("agent_start", {})
        assert collector.tool_calls_total == 0

    def test_summary(self):
        collector = metrics_hook.install()
        hooks.emit("agent_start", {})
        hooks.emit("tool_after", {"tool_name": "read", "is_error": False})
        hooks.emit("agent_end", {})
        s = collector.summary()
        assert s["tool_calls_total"] == 1
        assert "duration_seconds" in s

    def test_patch_fail_tracking(self):
        collector = metrics_hook.install()
        hooks.emit("agent_start", {})
        hooks.emit("tool_after", {
            "tool_name": "apply_patch",
            "is_error": True,
            "path_value": "/foo.py",
        })
        assert collector.get_patch_fail_count("/foo.py") == 1
        # Success clears the count
        hooks.emit("tool_after", {
            "tool_name": "apply_patch",
            "is_error": False,
            "path_value": "/foo.py",
        })
        assert collector.get_patch_fail_count("/foo.py") == 0


class TestConversationDump:
    """Tests for conversation_dump hook."""

    def setup_method(self):
        hooks.clear()

    def test_dump_creates_files(self):
        conversation_dump.install()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test.jsonl")
            messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            result = hooks.emit("agent_end", {
                "log_path": log_path,
                "system_prompt": "You are helpful.",
                "messages": messages,
            })
            dump = result.get("conversation_dump")
            assert dump is not None
            assert os.path.exists(dump["raw"])
            assert os.path.exists(dump["pretty"])
            # Verify raw JSON
            with open(dump["raw"], "r") as f:
                data = json.load(f)
            assert len(data) == 3  # system + 2 messages
            assert data[0]["role"] == "system"

    def test_no_dump_without_log_path(self):
        conversation_dump.install()
        result = hooks.emit("agent_end", {
            "system_prompt": "test",
            "messages": [],
        })
        assert "conversation_dump" not in result


class TestLoggingHook:
    """Tests for logging_hook."""

    def setup_method(self):
        hooks.clear()
        logging_hook._log_path = None
        logging_hook._run_context.clear()

    def test_init_logging_creates_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = logging_hook.init_logging(tmpdir, "test-agent")
            assert "test-agent" in path
            assert path.endswith(".jsonl")

    def test_log_event_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = logging_hook.init_logging(tmpdir, "test")
            logging_hook.log_event("test_event", {"key": "value"})
            with open(path, "r") as f:
                line = f.readline()
            rec = json.loads(line)
            assert rec["event"] == "test_event"
            assert rec["key"] == "value"

    def test_run_context_enrichment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = logging_hook.init_logging(tmpdir, "test")
            logging_hook.update_run_context({"run_name": "my_run", "agent": "test"})
            logging_hook.log_event("ctx_test", {})
            with open(path, "r") as f:
                line = f.readline()
            rec = json.loads(line)
            assert rec["run_name"] == "my_run"
            assert rec["agent"] == "test"

    def test_install_registers_hooks(self):
        logging_hook.install()
        events = hooks.registered_events()
        assert "agent_start" in events
        assert "tool_after" in events
        assert "api_request" in events
