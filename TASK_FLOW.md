# Task Flow Proposal (CLI / Benchmark + Interactive)

## Goals
- Keep the main conversation small and predictable.
- Enforce process discipline for local models (e.g., gpt-oss-120b).
- Make benchmark output deterministic without relying on model text.

## Entities
### Task
Required:
- `id`
- `description`
Optional:
- `goal`
- `status` (pending | in_progress | completed | failed)
- `priority`
Runtime-only:
- `summary`
- `files_changed`

### Subtask (logical phase)
No human-facing summary. Used only to enforce flow and tool gating.

## Modes
### CLI / Benchmark
- Subtasks: no human output.
- Main task: runtime ends when `status=completed`.
- Runtime prints benchmark-required line (e.g., `Finished Try1`) without model help.

### Interactive
- Subtasks: no human output.
- Main task: runtime prints a short human summary (optional).

## Core Flow (staged)
Phases:
1) **explore**  
   - Tools: read-only (ls/read/search).
   - Exit: required files read (tests + target file).

2) **plan**  
   - Tools: reasoning + `plan_tasks`.
   - Exit: `plan_tasks(action="create")` succeeds.

3) **implement**  
   - Tools: read + write/patch.
   - Exit: code change detected or max turns reached.

Final state:
- `completed` if no errors, else `failed`.

## Output Policy
### Subtasks
- No human summary.
- Optional structured report via tool call (runtime-controlled).

### Main Task
- Benchmark: runtime prints `Finished Try1` or `Finished Try2`.
- Interactive: runtime prints short summary (1–3 lines).

## Structured Task Report (runtime)
Emitted to logs as `task_report` event (no human output).
Fields:
- `task_id`, `description`, `status`
- `attempts`, `replan_max`
- `files_changed`
- `files_read`
- `plan_steps`
- `tool_calls_total`, `tool_errors_total`
- `tool_call_counts`, `tool_error_counts`
- `analysis_retries`, `feedback_counts`
- `error` (only on failure, truncated)

Validation:
- Runtime validates required fields and basic types.
- Invalid reports emit `task_report_invalid` to logs.

## Tool Gating Rules
- Before plan: block write tools (patch/write/edit).
- During plan: allow only `plan_tasks` + reasoning.
- During implement: allow write tools.

## Suggested Config Knobs
- `task_flow_mode`: `staged` | `flat` | `branched`
- `task_flow_mode=staged3` (fixed context → plan → implement pipeline)
- `task_output_mode`: `runtime` | `human`
- `benchmark_output_mode`: `runtime` | `model`
- `task_plan_mode`: `first` | `explore` | `none`
- `task_skip_readonly`: boolean (skip read-only tasks from plan)
- `task_replan_max`: integer
- `task_branching`: boolean
- `flow`: array of stage configs (dynamic flow mode)
- `flow_stage_retries`: integer
- `flow_stage_required`: boolean
- `flow_history_mode`: `full` | `tail`
- `flow_history_max_messages`: integer (messages to keep per stage)
- `flow_history_keep_first`: boolean (keep stage prompt in history)
- `flow_history_strip_thinking`: boolean (remove reasoning fields from history)
- `flow_history_tool_truncate_chars`: integer (truncate tool outputs in history)
- `flow_history_tool_truncate_keep_last`: integer (keep last N tool outputs untruncated)
- `flow_context_window`: integer (how many prior stage notes to inject; 0 disables)
- `flow_retry_hints`: boolean (inject guidance when stage retry is needed)
- `history_strip_thinking`: boolean (global)
- `history_tool_truncate_chars`: integer (global)
- `history_tool_truncate_keep_last`: integer (global)
- `tools_allow`: list of tool names allowed in a stage (flow stages)
- `tools_block`: list of tool names blocked in a stage (flow stages)

## Runtime Responsibilities (no model dependency)
- Maintain file cache for read tools (avoid rereads).
- Invalidate cache on write/patch or external changes.
- Enforce tool gating per phase.
- Generate benchmark-required final output.
- Emit structured task reports to logs (machine-readable).
- Preserve internal task context (status + files) between subtasks without human summary.
- Record structured metadata in logs.
- In benchmark mode, block edits to test files (override with `LOCALCODE_BLOCK_TEST_EDITS=0`).

## Notes
- `goal` is optional but recommended for clarity.
- For local models with 1 tool-call/turn, staged flow improves stability.

## Flow Mode (dynamic prompts)
`flow` is a configurable sequence of stages that re-prompts the model with
stage-specific instructions while keeping a minimal top-level conversation.
No tool gating is applied by default.

### Example
```
"flow": [
  {"id": "context", "label": "CONTEXT", "prompt": "Gather context and locate files."},
  {"id": "architect", "label": "ARCHITECT", "prompt": "Propose approach and risks."},
  {"id": "implement", "label": "IMPLEMENT", "prompt": "Make changes and verify."}
],
"flow_stage_retries": 2,
"flow_stage_required": true
```

### Completion Signal
Each stage must call `flow_stage_done(stage=..., ...)` to advance. The tool
accepts structured metadata (`summary`, `decisions`, `next_actions`, etc.).

### Notes
- Flow stages run as separate sub-runs; only short summaries feed the next stage.
- If `flow_stage_required=true` and the tool is not called, the flow fails.
- Stage configs may override history settings per stage:
  `history_mode`, `history_max_messages`, `history_keep_first`,
  `history_strip_thinking`, `history_tool_truncate_chars`,
  `history_tool_truncate_keep_last`, and `context_window`.
- Stage configs may also restrict tools with `tools_allow` / `tools_block`.
- Stage configs may enforce minimum activity:
  `require_non_flow_tool` (min non-`flow_stage_done` tool calls),
  `require_files_read` (min read_file/read_files count),
  `allow_missing_done` (permit stage completion without `flow_stage_done` if requirements met).

## Side-Channel Phase Control (optional)
Phase control is an internal state machine that does **not** write to conversation history.
It can inject lightweight phase-specific prompt hints and optionally run a classifier
probe after each turn.

Config (agent JSON):
```
"phase_log": "off" | "stdout" | "log" | "both",
"phase_control": {
  "mode": "off" | "rules" | "llm" | "hybrid",
  "states": ["context","plan","implement"],
  "default": "context",
  "rules": {
    "min_files_read_for_plan": 1,
    "auto_plan_after_read": true,
    "write_to_implement": true
  },
  "probe": {
    "temperature": 0,
    "max_tokens": 64,
    "system_prompt": "...",
    "user_prompt": "..."
  },
  "prompts": {
    "context": "Before making changes, read the relevant files.",
    "plan": "Outline a short plan before editing.",
    "implement": "Make the code changes now."
  }
}
```

Notes:
- `mode=rules` uses deterministic signals only (no extra model calls).
- `mode=llm` runs a separate classifier call each turn (no tools, no history).
- `mode=hybrid` uses rules first, then falls back to the classifier.
- Phase prompts are appended to the system prompt and are not stored in conversation history.
- `phase_log=stdout` prints phase events to stdout, `phase_log=log` appends them to the `.log` dump, `both` does both.
