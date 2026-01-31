# localcode

Local coding agent + benchmark harness for evaluating LLMs on programming tasks.
Agents run against local servers (llama.cpp GGUF or MLX) and solve Exercism tasks
inside Docker using the polyglot-benchmark dataset.

Repository: https://github.com/KrakowiakK/localcode

## How It Works

```
┌──────────────┐     ┌──────────────────┐     ┌────────────────┐
│  llama.cpp   │◄────│  Docker          │────►│  Results       │
│  or MLX      │     │  (benchmark.py)  │     │  benchmark/    │
│  server      │     │                  │     │  tmp.benchmark │
│  port 1235   │     │  localcode agent │     │                │
└──────────────┘     │  (mounted)       │     └────────────────┘
                     └──────────────────┘
```

1. **Server** — llama.cpp serves a GGUF model on port 1235 (or MLX on port 1234)
2. **Docker** — `benchmark.py` runs Exercism tests inside a container
3. **Agent** — `localcode` (Python, native tool calls) solves the task via an OpenAI‑compatible API
4. **Results** — pass/fail per task, tool call statistics, tokens, TPS

## Requirements

- macOS with Apple Silicon (for Metal acceleration)
- Docker Desktop
- Python 3.10+
- cmake (to build llama.cpp)
- A GGUF model file (e.g. in `~/.lmstudio/models/`)

## Quick Start

### 1. Clone

```bash
git clone https://github.com/KrakowiakK/localcode.git
cd localcode
```

### 2. Setup (first time)

```bash
# Clones polyglot-benchmark, builds Docker image
./bin/setup-benchmark.sh

# Builds llama.cpp → bin/llama-server
./bin/build-llama.sh
```

### 3. Run a benchmark

```bash
# Start server (background)
./bin/start-server.sh glm-4.7-flash --background

# Run benchmark on a single task
./bin/run-benchmark.sh glm-4.7-flash -k space-age

# Multiple tasks
./bin/run-benchmark.sh glm-4.7-flash -k space-age,leap,react

# All JavaScript tasks (49)
./bin/run-benchmark.sh glm-4.7-flash --all

# All languages
./bin/run-benchmark.sh glm-4.7-flash --full

# Stop server
./bin/stop-server.sh
```

### 4. Results

After completion the script prints a results table:

```
Pass@1: 35/49 (71.4%)
Pass@2: 38/49 (77.6%)
Avg time per task: 42.3s
```

Raw results are stored in: `benchmark/tmp.benchmark/<run-name>/`

## Project Structure

```
localcode/
├── bin/                            # Scripts
│   ├── llama-server                # Compiled binary
│   ├── setup-benchmark.sh          # Setup: clones polyglot + builds Docker
│   ├── build-llama.sh              # Builds llama.cpp
│   ├── start-server.sh             # Starts llama-server
│   ├── stop-server.sh              # Stops server
│   ├── run-benchmark.sh            # Main runner + statistics
│   └── run-localcode-benchmark.sh  # Docker runner (called by run-benchmark)
│
├── benchmark/                      # Standalone benchmark code
│   ├── benchmark.py                # Benchmark runner (stdlib only)
│   ├── Dockerfile                  # Docker image (runtimes only)
│   ├── npm-test.sh                 # JS test runner
│   ├── cpp-test.sh                 # C++ test runner
│   ├── tmp.benchmarks/
│   │   └── polyglot-benchmark/     # Exercism exercises (cloned by setup)
│   └── tmp.benchmark/              # Benchmark results
│
├── localcode/                      # Agent that solves tasks
│   ├── localcode.py                # Main agent runner
│   ├── agents/
│   │   └── gguf/                   # Agent configs (JSON per model)
│   ├── prompts/                    # System prompt templates
│   ├── tools/                      # Tool schemas (JSON)
│   └── tests/                      # Unit tests
│
└── llama.cpp/                      # llama.cpp source (from build-llama.sh)
```

## Scripts in `bin/`

### `setup-benchmark.sh`

One-time setup of the benchmark environment.

```bash
./bin/setup-benchmark.sh             # Full setup
./bin/setup-benchmark.sh --rebuild   # Rebuild Docker image
./bin/setup-benchmark.sh --update    # git pull + rebuild
```

What it does:
1. Clones [polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark) into `benchmark/tmp.benchmarks/`
2. Builds Docker image `benchmark-localcode` from `benchmark/Dockerfile`

### `build-llama.sh`

Clones/updates and compiles llama.cpp with Metal support (Apple GPU).

```bash
./bin/build-llama.sh              # Clone + build
./bin/build-llama.sh --update     # git pull + rebuild
./bin/build-llama.sh --clean      # Clean build from scratch
```

Output: `bin/llama-server`

### `start-server.sh`

Starts llama-server using configuration from an agent JSON file.

```bash
./bin/start-server.sh <agent> [--port PORT] [--background]

# Examples:
./bin/start-server.sh glm-4.7-flash --background
./bin/start-server.sh gpt-oss-120b-mxfp4 --port 1236
```

### `stop-server.sh`

Stops the server started by `start-server.sh`.

```bash
./bin/stop-server.sh
```

### `run-benchmark.sh`

Main script that runs the benchmark. Checks server health, launches Docker,
and displays statistics after completion.

```bash
./bin/run-benchmark.sh <agent> [options] <tasks>

# Options:
#   -k <task1,task2>   Filter tasks (like pytest -k)
#   --all              All JavaScript tasks (49)
#   --full             All languages
#   --tries N          Number of attempts per task (default 1)
#   --port PORT        Override server port
#   --agent-args "..." Extra arguments for the agent

# Examples:
./bin/run-benchmark.sh glm-4.7-flash -k space-age
./bin/run-benchmark.sh gpt-oss-120b-mxfp4 -k react,leap --tries 2
./bin/run-benchmark.sh glm-4.7-flash --all
```

### `run-localcode-benchmark.sh`

Docker runner — called internally by `run-benchmark.sh`.
Mounts `benchmark/` and `localcode/` into the container and runs `benchmark.py`.

---

## Localcode Agent

Lightweight CLI agent for automated coding benchmarks. Uses native tool calling
with JSON agent configs.

### Usage

```bash
# Run with prompt file
python3 localcode/localcode.py --agent gguf/glm-4.7-flash --file prompt.md

# With custom model/URL
python3 localcode/localcode.py --agent benchmark \
  --model gpt-oss-120b@8bit \
  --url http://localhost:1234/v1/chat/completions \
  --file prompt.md

# Continue previous session
python3 localcode/localcode.py --agent assistant --continue
```

### Benchmark Workflow

The agent runs in a two-attempt workflow:

1. **Try1**: Agent receives task, implements solution, says "Finished Try1"
2. **Try2**: If tests fail, agent receives errors, fixes bugs, says "Finished Try2"

Tests are run by the benchmark harness AFTER the agent finishes — the agent cannot run tests itself.

### Agent Config

Agent configs in `localcode/agents/*.json` define model, tools, and inference parameters:

```json
{
  "name": "gguf/glm-4.7-flash",
  "model": "glm-4.7-flash",
  "url": "http://localhost:1235/v1/chat/completions",
  "temperature": 0.7,
  "max_tokens": 16000,
  "tool_choice": "auto",
  "think": true,
  "think_level": "medium",
  "tools": ["list_dir", "read_file", "write_file", "replace_in_file", "patch_files", "find_files", "search_text"],
  "server_config": {
    "model_path": "~/.lmstudio/models/unsloth/GLM-4.7-Flash-GGUF/GLM-4.7-Flash-UD-Q4_K_XL.gguf",
    "context_window": 202752,
    "hf_model": "hf://unsloth/GLM-4.7-Flash-GGUF/GLM-4.7-Flash-UD-Q4_K_XL.gguf"
  }
}
```

| Field | Description |
|-------|-------------|
| `url` | Server endpoint (port determines GGUF vs MLX) |
| `tools` | Ordered list of available tools |
| `tool_choice` | `auto` / `required` / `none` |
| `think` | `true` / `false` — enable reasoning |
| `think_level` | `low` / `medium` / `high` — reasoning effort |
| `cache` | `true` / `false` — prompt caching |
| `auto_tool_call_on_failure` | Auto-call a tool when minimum tool calls not met |
| `require_code_change` | Require a write/edit/patch before finishing |
| `native_thinking` | Preserve thinking in assistant messages |
| `min_tool_calls` | Minimum tool calls required |
| `max_format_retries` | Retries on malformed responses |
| `max_batch_tool_calls` | Max tool calls per response (1-10) |
| `phase_log` | `off` / `stdout` / `log` / `both` |
| `server_config.model_path` | Local path to the GGUF file |
| `server_config.hf_model` | HuggingFace fallback for download |
| `server_config.extra_args` | Additional flags for llama-server |

Agent names use the JSON path relative to `agents/`:
- `agents/gguf/gpt-oss-120b.json` → `--agent gguf/gpt-oss-120b`

### Phase Control (side-channel)

Optional phase control that does not write to conversation history.
It injects small phase hints into the system prompt and can classify phase
transitions using rules or a lightweight probe call.

Agent config:

```json
"phase_log": "both",
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
    "max_tokens": 64
  },
  "prompts": {
    "context": "Before making changes, read the relevant files.",
    "plan": "Outline a short plan before editing.",
    "implement": "Make the code changes now."
  }
}
```

You can also set it globally:

```bash
LOCALCODE_PHASE_LOG=both
```

### Flow / Staged Modes

For stricter multi‑stage workflows, use `task_flow_mode` or a custom `flow` with
`flow_stage_done` and `plan_tasks`. See `TASK_FLOW.md` for details.

### Available Tools

| Tool | Aliases | Description |
|------|---------|-------------|
| `ls` | `list_dir` | List directory contents |
| `read` | `read_file` | Read file contents |
| `batch_read` | `read_files`, `read_multiple` | Read multiple files in one call |
| `write` | `write_file` | Write/create file |
| `edit` | `replace_in_file` | Edit file with search/replace |
| `apply_patch` | `patch_files` | Apply unified diff patch |
| `glob` | `find_files` | Find files by pattern |
| `grep` | - | Search file contents (regex) |
| `search` | `search_text` | Search file contents (simple) |
| `shell` | - | Execute shell command (sandboxed) |
| `think` | `reasoning` | Internal reasoning step |
| `ask_agent` | - | Delegate to sub-agent |
| `ask_questions` | - | Batch reasoning questions |
| `plan_solution` | `get_plan` | Plan before implementing |
| `plan_tasks` | - | Structured planning for staged flows |
| `flow_stage_done` | - | Mark a flow stage as complete |

### Testing

```bash
.venv/bin/python -m pytest localcode/tests/ -v
```

### Logs

Each run creates log files in `localcode/logs/`:
- `.jsonl` — structured events (requests, responses, tool calls, phase events)
- `.log` — human-readable conversation dump (includes **PHASE EVENTS** when `phase_log=log|both`)
- `.raw.json` — raw conversation in JSON

---

## Debugging

```bash
# Server logs
tail -f /tmp/benchmark-llama-server.log

# Localcode structured logs (per-run)
cat localcode/logs/localcode_*.jsonl | python3 -m json.tool

# Benchmark results for a task
cat benchmark/tmp.benchmark/<run>/javascript/exercises/practice/space-age/.aider.results.json

# Server health check
curl http://localhost:1235/health | python3 -m json.tool
```

## Cleanup

```bash
# Remove benchmark results and runtime logs
rm -rf benchmark/tmp.benchmark/* localcode/logs/* localcode/.localcode/*
```
