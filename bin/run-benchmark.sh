#!/bin/bash
#
# Localcode Benchmark Runner (no server management)
#
# The user starts the server manually. This script only:
#   1. Checks server health
#   2. Runs the Docker benchmark
#   3. Parses logs from localcode/logs/*.jsonl
#   4. Prints statistics
#
# Usage:
#   ./bin/run-benchmark.sh <agent> -k space-age
#   ./bin/run-benchmark.sh <agent> -k react,leap
#   ./bin/run-benchmark.sh <agent> --all
#   ./bin/run-benchmark.sh <agent> --full
#   ./bin/run-benchmark.sh <agent> -k react --tries 2
#   ./bin/run-benchmark.sh <agent> -k react --port 1235
#
# Examples:
#   ./bin/run-benchmark.sh jan-v3-4b -k react
#   ./bin/run-benchmark.sh glm-4.7-flash -k react,space-age --tries 2
#   ./bin/run-benchmark.sh gpt-oss-120b --all
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BENCHMARK_DIR="$PROJECT_DIR/benchmark"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check if setup has been run
if [ ! -d "$PROJECT_DIR/benchmark" ]; then
    echo -e "${RED}ERROR: benchmark/ directory does not exist.${NC}"
    echo "Run first: ./bin/setup-benchmark.sh"
    exit 1
fi

if ! docker images | grep -q "benchmark-localcode"; then
    echo -e "${RED}ERROR: Docker image 'benchmark-localcode' not found.${NC}"
    echo "Run first: ./bin/setup-benchmark.sh"
    exit 1
fi

# Check if agent name was provided as the first argument
if [ $# -eq 0 ]; then
    echo -e "${RED}ERROR: Agent name is required as the first argument${NC}"
    echo ""
    echo "Usage: $0 <agent> [options] [tasks]"
    echo ""
    echo "Available GGUF agents:"
    ls -1 "$PROJECT_DIR/localcode/agents/gguf/"*.json 2>/dev/null | xargs -n1 basename | sed 's/.json$//' || true
    echo ""
    echo "Available MLX agents:"
    ls -1 "$PROJECT_DIR/localcode/agents/mlx/"*.json 2>/dev/null | xargs -n1 basename | sed 's/.json$//' || true
    echo ""
    echo "Examples:"
    echo "  $0 jan-v3-4b -k react"
    echo "  $0 glm-4.7-flash -k space-age,leap"
    echo "  $0 gpt-oss-120b --all"
    exit 1
fi

# First argument is the agent name
AGENT_NAME_ARG="$1"
shift

# Auto-detect prefix (gguf/mlx) and agent file
AGENT_FILE=""
AGENT_PREFIX=""
if [ -f "$PROJECT_DIR/localcode/agents/gguf/${AGENT_NAME_ARG}.json" ]; then
    AGENT_FILE="$PROJECT_DIR/localcode/agents/gguf/${AGENT_NAME_ARG}.json"
    AGENT_PREFIX="gguf"
elif [ -f "$PROJECT_DIR/localcode/agents/mlx/${AGENT_NAME_ARG}.json" ]; then
    AGENT_FILE="$PROJECT_DIR/localcode/agents/mlx/${AGENT_NAME_ARG}.json"
    AGENT_PREFIX="mlx"
else
    echo -e "${RED}ERROR: Agent not found: ${AGENT_NAME_ARG}${NC}"
    echo "Searched in:"
    echo "  $PROJECT_DIR/localcode/agents/gguf/${AGENT_NAME_ARG}.json"
    echo "  $PROJECT_DIR/localcode/agents/mlx/${AGENT_NAME_ARG}.json"
    echo ""
    echo "Available GGUF agents:"
    ls -1 "$PROJECT_DIR/localcode/agents/gguf/"*.json 2>/dev/null | xargs -n1 basename | sed 's/.json$//' || true
    echo ""
    echo "Available MLX agents:"
    ls -1 "$PROJECT_DIR/localcode/agents/mlx/"*.json 2>/dev/null | xargs -n1 basename | sed 's/.json$//' || true
    exit 1
fi

# Auto-detect port from agent config ("url" field)
AUTO_PORT=$(AGENT_FILE="$AGENT_FILE" python3 -c "
import json, os, re
try:
    data = json.loads(open(os.environ['AGENT_FILE']).read())
    url = data.get('url', '')
    m = re.search(r':(\d+)/', url)
    print(m.group(1) if m else '1234')
except Exception:
    print('1234')
")

# Handle flags
RUN_ALL=false
RUN_FULL=false
TASKS=""
AGENT_ARGS=""
BENCHMARK_TRIES="${BENCHMARK_TRIES:-1}"
PORT_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --all)
            RUN_ALL=true
            shift
            ;;
        --full)
            RUN_FULL=true
            shift
            ;;
        --tries)
            BENCHMARK_TRIES="$2"
            shift 2
            ;;
        --agent-args)
            AGENT_ARGS="$2"
            shift 2
            ;;
        --agent-arg)
            AGENT_ARGS="$AGENT_ARGS $2"
            shift 2
            ;;
        --port)
            PORT_OVERRIDE="$2"
            shift 2
            ;;
        *)
            TASKS="$TASKS $1"
            shift
            ;;
    esac
done

export BENCHMARK_TRIES

TASKS="${TASKS# }"

# Require tasks unless --all/--full
if [ -z "$TASKS" ] && [ "$RUN_ALL" = false ] && [ "$RUN_FULL" = false ]; then
    echo -e "${RED}ERROR: Tasks required or use --all/--full${NC}"
    echo ""
    echo "Usage: $0 $AGENT_NAME_ARG [options] <tasks>"
    echo ""
    echo "Examples:"
    echo "  $0 $AGENT_NAME_ARG -k react"
    echo "  $0 $AGENT_NAME_ARG -k space-age,leap"
    echo "  $0 $AGENT_NAME_ARG --all"
    exit 1
fi

# Set port (override > auto > fallback)
SERVER_PORT="${PORT_OVERRIDE:-$AUTO_PORT}"

echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Localcode Benchmark Runner${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"

# 1. Check if the server is running
echo -e "\n${YELLOW}[1/2] Checking server on port ${SERVER_PORT}...${NC}"

if ! curl -sf "http://localhost:${SERVER_PORT}/health" >/dev/null 2>&1; then
    echo -e "${RED}ERROR: Server is not running on port ${SERVER_PORT}.${NC}"
    echo ""
    echo "Start the server manually before running the benchmark:"
    if [ "$AGENT_PREFIX" = "gguf" ]; then
        MODEL_NAME=$(AGENT_FILE="$AGENT_FILE" python3 -c "
import json, os
try:
    data = json.loads(open(os.environ['AGENT_FILE']).read())
    print(data.get('model', ''))
except Exception:
    print('')
")
        echo "  ./bin/start-server.sh ${AGENT_NAME_ARG} --background"
    else
        echo "  cd server_mlx && source mlx_env/bin/activate && python main.py"
    fi
    exit 1
fi

# Show loaded models
echo -e "${GREEN}Server is up!${NC}"
curl -s "http://localhost:${SERVER_PORT}/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Loaded models:', ', '.join(d.get('models', [])))" 2>/dev/null || true

# 2. Check model and run benchmark
echo -e "\n${YELLOW}[2/2] Running benchmark...${NC}"

AGENT_PATH="$AGENT_FILE"
MODEL_FROM_AGENT=$(AGENT_PATH="$AGENT_PATH" python3 -c "
import json, os
from pathlib import Path
path = Path(os.environ.get('AGENT_PATH', ''))
if path.is_file():
    try:
        data = json.loads(path.read_text())
        model = data.get('model')
        if model:
            print(model)
    except Exception:
        pass
")
MODEL_OVERRIDE=$(AGENT_ARGS="$AGENT_ARGS" python3 -c "
import os, shlex
args = shlex.split(os.environ.get('AGENT_ARGS', ''))
model = ''
for i, arg in enumerate(args):
    if arg in ('--model', '-m') and i + 1 < len(args):
        model = args[i + 1]
        break
print(model)
")
if [ -n "$MODEL_OVERRIDE" ]; then
    MODEL_INFO="${MODEL_OVERRIDE} (override)"
elif [ -n "$MODEL_FROM_AGENT" ]; then
    MODEL_INFO="$MODEL_FROM_AGENT"
else
    MODEL_INFO="unknown"
fi

cd "$BENCHMARK_DIR"

TIMESTAMP=$(date +%Y-%m-%d-%H-%M-%S)
NAME="${AGENT_NAME_ARG}-${AGENT_PREFIX}-$TIMESTAMP"

echo -e "Agent: ${GREEN}${AGENT_PREFIX}/${AGENT_NAME_ARG}${NC}"
echo -e "Model: ${GREEN}$MODEL_INFO${NC}"
echo -e "Port: ${GREEN}$SERVER_PORT${NC}"

if [ "$RUN_FULL" = true ]; then
    echo -e "Tasks: ${GREEN}ALL (all languages)${NC}"
elif [ "$RUN_ALL" = true ]; then
    echo -e "Tasks: ${GREEN}ALL (49)${NC}"
else
    echo -e "Tasks: ${GREEN}$TASKS${NC}"
fi
if [ -n "$AGENT_ARGS" ]; then
    export AGENT_ARGS
    echo -e "Agent args: ${GREEN}$AGENT_ARGS${NC}"
fi
if [ "$BENCHMARK_TRIES" != "1" ]; then
    echo -e "Tries: ${GREEN}$BENCHMARK_TRIES${NC}"
fi

# Export with gguf/ or mlx/ prefix
export LOCALCODE_AGENT_CONFIG="${AGENT_PREFIX}/${AGENT_NAME_ARG}"
export BENCHMARK_SERVER_PORT=$SERVER_PORT
echo -e "Results: ${GREEN}$NAME${NC}"
echo ""

# Run benchmark
LANG_ARGS="--languages javascript"
if [ "$RUN_FULL" = true ]; then
    LANG_ARGS=""
fi
LOCALCODE_TURN_SUMMARY=${LOCALCODE_TURN_SUMMARY:-1} LOCALCODE_STREAM_OUTPUT=${LOCALCODE_STREAM_OUTPUT:-1} AGENT="${AGENT:-localcode}" NAME="$NAME" "$SCRIPT_DIR/run-localcode-benchmark.sh" $LANG_ARGS $TASKS

# Post-benchmark stats
RESULTS_DIR="$BENCHMARK_DIR/tmp.benchmark"
RUN_DIR=$(ls -dt "$RESULTS_DIR"/*"$NAME"* 2>/dev/null | head -n 1)
if [ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ]; then
    echo -e "\n${GREEN}════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  STATISTICS${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
RUN_DIR="$RUN_DIR" LOG_DIR="$PROJECT_DIR/localcode/logs" RUN_FULL="$RUN_FULL" LOCALCODE_AGENT_CONFIG="$LOCALCODE_AGENT_CONFIG" python3 - <<'PY'
import json
import os
import re
import textwrap
from pathlib import Path
from collections import Counter, defaultdict

run_dir = Path(os.environ["RUN_DIR"])
run_full = str(os.environ.get("RUN_FULL", "")).lower() == "true"
log_dir = Path(os.environ.get("LOG_DIR", ""))
agent_config = os.environ.get("LOCALCODE_AGENT_CONFIG", "localcode")

results = {}
entries = []
langs = set()
glob_pattern = "*/exercises/practice/*/.aider.results.json" if run_full else "javascript/exercises/practice/*/.aider.results.json"
for p in run_dir.glob(glob_pattern):
    task = p.parent.name
    lang = p.parents[3].name if len(p.parents) >= 4 else "unknown"
    data = json.loads(p.read_text())
    outcomes = data.get("tests_outcomes") or []
    entries.append((lang, task, outcomes, data.get("duration")))
    langs.add(lang)

multi_lang = run_full or len(langs) > 1
for lang, task, outcomes, duration in entries:
    key = f"{lang}/{task}" if multi_lang else task
    results[key] = {
        "pass1": bool(outcomes[0]) if outcomes else False,
        "pass2": bool(outcomes[1]) if len(outcomes) > 1 else None,
        "duration": duration,
    }

count = len(results)
pass1 = sum(1 for r in results.values() if r["pass1"])
pass2 = sum(1 for r in results.values() if r["pass2"] is True)
pass_any = sum(1 for r in results.values() if r["pass1"] or r["pass2"])
durations = [r["duration"] for r in results.values() if isinstance(r["duration"], (int, float))]
avg_duration = sum(durations) / len(durations) if durations else None

per_task = defaultdict(lambda: {"tool_calls": 0, "tool_errors": 0, "sessions": 0})
per_task_tool_calls = defaultdict(Counter)
per_task_tool_errors = defaultdict(Counter)
per_task_flows = defaultdict(list)
per_task_req_ids = defaultdict(lambda: {"try1": None, "try2": None})
per_tool_calls = Counter()
per_tool_errors = Counter()
error_categories = Counter()
def new_usage_bucket():
    return {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "prefill_sum": 0.0,
        "prefill_weight": 0.0,
        "decode_sum": 0.0,
        "decode_weight": 0.0,
    }

per_task_usage = defaultdict(new_usage_bucket)
per_task_usage_try = defaultdict(lambda: {"try1": new_usage_bucket(), "try2": new_usage_bucket()})
estimated_tps_used = False
per_task_try = defaultdict(lambda: {
    "try1": {"calls": 0, "errs": 0},
    "try2": {"calls": 0, "errs": 0},
})
per_task_tool_calls_try = defaultdict(lambda: {"try1": Counter(), "try2": Counter()})
per_task_tool_errors_try = defaultdict(lambda: {"try1": Counter(), "try2": Counter()})
request_param_sets = []

def classify_tool_error(preview):
    if "must read" in preview and "before patching" in preview:
        return "must_read_before_patching"
    if "must read" in preview and "before editing" in preview:
        return "must_read_before_editing"
    if "patch context not found" in preview:
        return "patch_context_not_found"
    if "patch context not unique" in preview:
        return "patch_context_not_unique"
    if "invalid patch format" in preview:
        return "invalid_patch_format"
    if "unexpected patch line" in preview:
        return "unexpected_patch_line"
    if "invalid add line" in preview:
        return "invalid_add_line"
    if "file not found in patch header" in preview:
        return "patch_file_not_found"
    if "missing required parameter" in preview:
        return "missing_required_param"
    if "old_string not found" in preview:
        return "old_string_not_found"
    if "old_string appears" in preview or "old_string is not unique" in preview:
        return "old_string_not_unique"
    if "file not found" in preview:
        return "read_file_not_found"
    if "outside sandbox root" in preview or "access denied" in preview:
        return "read_outside_sandbox"
    if "diff cannot be combined" in preview:
        return "diff_with_range"
    return "other"

def assign_step_file(flow_steps, tool_call_id, tool_name, tool_path):
    if not tool_path or not isinstance(tool_path, str):
        return None
    basename = os.path.basename(tool_path)
    if tool_call_id:
        for step in flow_steps:
            if step.get("tool_call_id") == tool_call_id and not step.get("file_set"):
                base = step.get("tool_name") or step.get("step", "")
                step["step"] = "{}[{}]".format(base, basename)
                step["file_set"] = True
                return step
    for step in reversed(flow_steps):
        if step.get("tool_name") == tool_name and not step.get("file_set"):
            base = step.get("tool_name") or step.get("step", "")
            step["step"] = "{}[{}]".format(base, basename)
            step["file_set"] = True
            return step
    return None

def find_step_for_error(flow_steps, tool_call_id, tool_name):
    if tool_call_id:
        for idx, step in enumerate(flow_steps):
            if step.get("tool_call_id") == tool_call_id and not step.get("error_set"):
                return idx
    for idx in range(len(flow_steps) - 1, -1, -1):
        step = flow_steps[idx]
        if step.get("tool_name") == tool_name and not step.get("error_set"):
            return idx
    return None

if log_dir.is_dir():
    log_patterns = [f"localcode_{agent_config}_*.jsonl", "localcode_*.jsonl"]
    seen_logs = set()
    for pattern in log_patterns:
        for log_path in log_dir.glob(pattern):
            if log_path in seen_logs:
                continue
            seen_logs.add(log_path)
            try:
                lines = log_path.read_text().splitlines()
            except Exception:
                continue
            cwd = None
            request_params = None
            for line in lines:
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if evt.get("event") == "session_start":
                    cwd = evt.get("cwd")
                    if not cwd or run_dir.name not in cwd:
                        cwd = None
                    break
            if not cwd:
                continue
            cwd_path = Path(cwd)
            task = cwd_path.name
            lang = cwd_path.parents[2].name if len(cwd_path.parents) >= 3 else "unknown"
            task_key = f"{lang}/{task}" if multi_lang else task
            flow_steps = []
            flow_active = False
            flow_label = "try"
            current_try = None
            current_turn_id = 0
            for line in lines:
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if evt.get("event") == "request" and request_params is None:
                    request_params = evt.get("request_params") or {}
                if evt.get("event") == "run_start":
                    flow_label = "try2" if evt.get("continue_session") else "try1"
                    current_turn_id = 0
                    flow_steps = [{"step": "prompt", "turn_id": current_turn_id}]
                    flow_active = True
                    current_try = flow_label
                if evt.get("event") == "run_end":
                    per_task[task_key]["tool_calls"] += int(evt.get("tool_calls_total", 0))
                    per_task[task_key]["tool_errors"] += int(evt.get("tool_errors_total", 0))
                    per_task[task_key]["sessions"] += 1
                    if current_try in ("try1", "try2"):
                        per_task_try[task_key][current_try]["calls"] += int(evt.get("tool_calls_total", 0))
                        per_task_try[task_key][current_try]["errs"] += int(evt.get("tool_errors_total", 0))
                    for k, v in (evt.get("tool_call_counts") or {}).items():
                        per_tool_calls[k] += int(v)
                        per_task_tool_calls[task_key][k] += int(v)
                        if current_try in ("try1", "try2"):
                            per_task_tool_calls_try[task_key][current_try][k] += int(v)
                    for k, v in (evt.get("tool_error_counts") or {}).items():
                        per_tool_errors[k] += int(v)
                        per_task_tool_errors[task_key][k] += int(v)
                        if current_try in ("try1", "try2"):
                            per_task_tool_errors_try[task_key][current_try][k] += int(v)
                    if flow_active and flow_steps:
                        per_task_flows[task_key].append({
                            "label": flow_label,
                            "steps": flow_steps,
                            "req_id": per_task_req_ids[task_key].get(current_try),
                        })
                    flow_active = False
                if evt.get("event") == "tool_result":
                    preview = (evt.get("result_preview") or "").strip()
                    tool_name = evt.get("tool") or ""
                    tool_call_id = evt.get("tool_call_id")
                    tool_path = evt.get("path")
                    if flow_active:
                        assign_step_file(flow_steps, tool_call_id, tool_name, tool_path)
                    if preview.startswith("error:"):
                        category = classify_tool_error(preview)
                        error_categories[category] += 1
                        if flow_active and tool_name:
                            idx = find_step_for_error(flow_steps, tool_call_id, tool_name)
                            if idx is not None:
                                step = flow_steps[idx]
                                base = step.get("step", "").split(" (", 1)[0]
                                step["step"] = "{} ({})".format(base, category)
                                step["error_set"] = True
                            else:
                                flow_steps.append({
                                    "step": "{} ({})".format(tool_name, category),
                                    "turn_id": current_turn_id,
                                })
                if evt.get("event") == "response":
                    tool_calls = evt.get("tool_calls") or []
                    req_id = evt.get("request_id")
                    tool_call_ids = evt.get("tool_call_ids") or []
                    if flow_active and current_try in ("try1", "try2") and req_id:
                        if per_task_req_ids[task_key].get(current_try) is None:
                            per_task_req_ids[task_key][current_try] = req_id
                    if flow_active and tool_calls:
                        current_turn_id += 1
                        for idx, tool_name in enumerate(tool_calls):
                            step = {
                                "step": str(tool_name),
                                "tool_name": str(tool_name),
                                "req_id": req_id,
                                "turn_id": current_turn_id,
                                "file_set": False,
                                "error_set": False,
                            }
                            if idx < len(tool_call_ids):
                                step["tool_call_id"] = tool_call_ids[idx]
                            flow_steps.append(step)
                    elif flow_active:
                        content_preview = (evt.get("content_preview") or "").strip()
                        if content_preview:
                            current_turn_id += 1
                            flow_steps.append({
                                "step": "final",
                                "req_id": req_id,
                                "turn_id": current_turn_id,
                            })
                if evt.get("event") == "response_meta":
                    usage = evt.get("usage") or {}
                    nonlocal_estimated = False
                    if evt.get("timings_estimated") is True:
                        nonlocal_estimated = True
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")
                    total_tokens = usage.get("total_tokens")
                    # TPS: top-level fields from localcode (llama-server timings)
                    # Fallback to legacy usage.timing for backward compat
                    prefill_tps = evt.get("prefill_tps")
                    decode_tps = evt.get("decode_tps")
                    if prefill_tps is None or decode_tps is None:
                        timing = usage.get("timing") or {}
                        if timing.get("estimated") is True:
                            nonlocal_estimated = True
                        prefill_tps = prefill_tps or timing.get("prefill_tps")
                        decode_tps = decode_tps or timing.get("decode_tps")
                    if isinstance(evt.get("timings"), dict) and evt["timings"].get("estimated") is True:
                        nonlocal_estimated = True
                    if nonlocal_estimated:
                        estimated_tps_used = True
                    if flow_active and current_try in ("try1", "try2"):
                        bucket = per_task_usage[task_key]
                        bucket_try = per_task_usage_try[task_key][current_try]
                        for key, val in (
                            ("prompt", prompt_tokens),
                            ("completion", completion_tokens),
                            ("total", total_tokens),
                        ):
                            if isinstance(val, (int, float)):
                                bucket[key] += int(val)
                                bucket_try[key] += int(val)
                        if isinstance(prefill_tps, (int, float)) and isinstance(prompt_tokens, (int, float)) and prompt_tokens > 0:
                            weight = float(prompt_tokens)
                            bucket["prefill_sum"] += prefill_tps * weight
                            bucket["prefill_weight"] += weight
                            bucket_try["prefill_sum"] += prefill_tps * weight
                            bucket_try["prefill_weight"] += weight
                        if isinstance(decode_tps, (int, float)) and isinstance(completion_tokens, (int, float)) and completion_tokens > 0:
                            weight = float(completion_tokens)
                            bucket["decode_sum"] += decode_tps * weight
                            bucket["decode_weight"] += weight
                            bucket_try["decode_sum"] += decode_tps * weight
                            bucket_try["decode_weight"] += weight
            if request_params is not None:
                request_param_sets.append(request_params)

print('Run dir: {}'.format(run_dir.name))
print('Tasks with results: {}'.format(count))
print('Pass@1: {}/{} ({:.1f}%)'.format(pass1, count, (pass1 / count * 100) if count else 0.0))
print('Pass@2: {}/{} ({:.1f}%)'.format(pass2, count, (pass2 / count * 100) if count else 0.0))
print('Pass@any: {}/{} ({:.1f}%)'.format(pass_any, count, (pass_any / count * 100) if count else 0.0))
if avg_duration is not None:
    print('Avg time per task: {:.1f}s'.format(avg_duration))

def print_table(title, rows, headers):
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(str(cell)))
    sep = "+".join("-" * (w + 2) for w in col_widths)
    print('')
    print('{}'.format(title))
    print('+{}+'.format(sep))
    print('| ' + ' | '.join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + ' |')
    print('+{}+'.format(sep))
    for row in rows:
        print('| ' + ' | '.join(str(row[i]).ljust(col_widths[i]) for i in range(len(headers))) + ' |')
    print('+{}+'.format(sep))

def summarize_param(params_list, key):
    values = []
    for params in params_list:
        if key in params:
            values.append(params[key])
    unique = []
    for val in values:
        if val not in unique:
            unique.append(val)
    if not unique:
        return "-"
    if len(unique) == 1:
        return unique[0]
    return "mixed: " + ", ".join(str(v) for v in unique)

if request_param_sets:
    model = summarize_param(request_param_sets, "model")
    temp = summarize_param(request_param_sets, "temperature")
    top_p = summarize_param(request_param_sets, "top_p")
    top_k = summarize_param(request_param_sets, "top_k")
    min_p = summarize_param(request_param_sets, "min_p")
    max_tokens = summarize_param(request_param_sets, "max_tokens")
    tool_choice = summarize_param(request_param_sets, "tool_choice")
    think_val = summarize_param(request_param_sets, "think")
    effort = summarize_param(request_param_sets, "reasoning_effort")
    budget_tokens = summarize_param(request_param_sets, "budget_tokens")
    return_thinking = summarize_param(request_param_sets, "return_thinking")
    cache_val = summarize_param(request_param_sets, "cache")
    stream_val = summarize_param(request_param_sets, "stream")

    think_summary = think_val
    if think_val in (True, "True", "true"):
        # Show budget_tokens if available, otherwise effort
        budget_info = "budget={}".format(budget_tokens) if budget_tokens != "-" else "effort={}".format(effort)
        think_summary = "enabled ({})".format(budget_info)
    elif think_val in (False, "False", "false"):
        think_summary = "disabled"

    rows = [
        ("model", model),
        ("temperature", temp),
        ("top_p", top_p),
        ("top_k", top_k),
        ("min_p", min_p),
        ("max_tokens", max_tokens),
        ("tool_choice", tool_choice),
        ("thinking", think_summary),
        ("cache", cache_val),
        ("stream", stream_val),
    ]
    print_table("Model & inference (from localcode request logs)", rows, ["param", "value"])

def summarize_usage(buckets):
    total = new_usage_bucket()
    for bucket in buckets:
        total["prompt"] += bucket.get("prompt", 0)
        total["completion"] += bucket.get("completion", 0)
        total["total"] += bucket.get("total", 0)
        total["prefill_sum"] += bucket.get("prefill_sum", 0.0)
        total["prefill_weight"] += bucket.get("prefill_weight", 0.0)
        total["decode_sum"] += bucket.get("decode_sum", 0.0)
        total["decode_weight"] += bucket.get("decode_weight", 0.0)
    return total

def format_tps(sum_val, weight):
    if weight and weight > 0:
        return "{:.1f}".format(sum_val / weight)
    return "NA"

if per_task_usage:
    overall = summarize_usage(per_task_usage.values())
    rows = [
        ("prompt_tokens", overall["prompt"]),
        ("completion_tokens", overall["completion"]),
        ("total_tokens", overall["total"]),
        ("prefill_tps", format_tps(overall["prefill_sum"], overall["prefill_weight"])),
        ("decode_tps", format_tps(overall["decode_sum"], overall["decode_weight"])),
    ]
    print_table("Token & speed summary (from response_meta)", rows, ["metric", "value"])
    if estimated_tps_used:
        print("Note: TPS values are estimated from request duration for servers that do not return native timings.")

tool_rows = [(name, count) for name, count in per_tool_calls.most_common()]
print_table("Tool calls (total)", tool_rows, ["tool", "count"])

tool_err_rows = [(name, count) for name, count in per_tool_errors.most_common()]
print_table("Tool errors (total)", tool_err_rows or [("-", 0)], ["tool", "count"])

if error_categories:
    err_rows = [(name, count) for name, count in error_categories.most_common()]
    print_table("Tool error categories", err_rows, ["category", "count"])

def task_sort_key(task):
    calls = per_task.get(task, {}).get("tool_calls", 0)
    return (calls, task)

sorted_tasks = sorted(results.keys(), key=task_sort_key)
task_ids = {task: idx + 1 for idx, task in enumerate(sorted_tasks)}

def task_request_id(task, fallback=True):
    req_id = per_task_req_ids.get(task, {}).get("try1")
    if req_id:
        return req_id
    return str(task_ids.get(task, "-")) if fallback else "-"

print('\nPer-task results:')
rows = []
for task in sorted_tasks:
    r = results[task]
    t = per_task.get(task, {})
    def fmt(val):
        if val is True:
            return "PASS"
        if val is False:
            return "FAIL"
        return "NA"
    dur = r["duration"]
    dur_s = '{:.1f}'.format(dur) if isinstance(dur, (int, float)) else 'NA'
    t1_calls = per_task_try.get(task, {}).get("try1", {}).get("calls", 0)
    t1_errs = per_task_try.get(task, {}).get("try1", {}).get("errs", 0)
    if r["pass2"] is None:
        t2_calls = "NA"
        t2_errs = "NA"
    else:
        t2_calls = per_task_try.get(task, {}).get("try2", {}).get("calls", 0)
        t2_errs = per_task_try.get(task, {}).get("try2", {}).get("errs", 0)
    rows.append((
        task_request_id(task),
        task,
        fmt(r["pass1"]),
        fmt(r["pass2"]),
        dur_s,
        t.get("tool_calls", 0),
        t.get("tool_errors", 0),
        t1_calls,
        t1_errs,
        t2_calls,
        t2_errs,
    ))
print_table(
    "Per-task results (sorted by tool calls)",
    rows,
    ["id", "task", "try1", "try2", "sec", "calls", "errs", "t1_calls", "t1_errs", "t2_calls", "t2_errs"],
)

def format_counts(counter, max_width):
    if not counter:
        return "-"
    items = ', '.join('{}={}'.format(k, v) for k, v in counter.most_common())
    return items if len(items) <= max_width else items[: max_width - 3] + "..."

rows = []
for task in sorted_tasks:
    tools_str = format_counts(per_task_tool_calls.get(task, Counter()), 50)
    errs_str = format_counts(per_task_tool_errors.get(task, Counter()), 30)
    t1_tools = format_counts(per_task_tool_calls_try.get(task, {}).get("try1", Counter()), 40)
    t1_errs = format_counts(per_task_tool_errors_try.get(task, {}).get("try1", Counter()), 30)
    t2_tools = "-"
    t2_errs = "-"
    if results.get(task, {}).get("pass2") is not None:
        t2_tools = format_counts(per_task_tool_calls_try.get(task, {}).get("try2", Counter()), 40)
        t2_errs = format_counts(per_task_tool_errors_try.get(task, {}).get("try2", Counter()), 30)
    rows.append((task_request_id(task), task, tools_str, errs_str, t1_tools, t1_errs, t2_tools, t2_errs))
print_table(
    "Per-task tool breakdown (sorted by tool calls)",
    rows,
    ["id", "task", "tools", "tool_errs", "t1_tools", "t1_errs", "t2_tools", "t2_errs"],
)

rows = []
for task in sorted_tasks:
    bucket = per_task_usage.get(task, new_usage_bucket())
    prompt_tokens = bucket.get("prompt", 0)
    completion_tokens = bucket.get("completion", 0)
    total_tokens = bucket.get("total", 0)
    prefill_tps = format_tps(bucket.get("prefill_sum", 0.0), bucket.get("prefill_weight", 0.0))
    decode_tps = format_tps(bucket.get("decode_sum", 0.0), bucket.get("decode_weight", 0.0))
    rows.append((task_request_id(task), task, prompt_tokens, completion_tokens, total_tokens, prefill_tps, decode_tps))
print_table(
    "Per-task tokens & speed (sorted by tool calls)",
    rows,
    ["id", "task", "prompt_tok", "completion_tok", "total_tok", "prefill_tps", "decode_tps"],
)

def format_flow(entries):
    if not entries:
        return "-"
    order = {"try1": 0, "try2": 1}
    parts = []
    def entry_sort_key(entry):
        label_raw = entry.get("label") or ""
        label_norm = label_raw.strip().lower()
        return order.get(label_norm, 99)
    for entry in sorted(entries, key=entry_sort_key):
        label_raw = entry.get("label") or ""
        label_norm = label_raw.strip().lower()
        label = label_norm or label_raw.strip()
        req_id = entry.get("req_id")
        if req_id:
            label = "{}[{}]".format(label, req_id)
        steps = entry.get("steps", [])
        flow_parts = []
        current_turn = None
        turn_parts = []
        for step in steps:
            text = step.get("step", "")
            turn_id = step.get("turn_id")
            if current_turn is None:
                current_turn = turn_id
            if turn_id != current_turn:
                if turn_parts:
                    flow_parts.append(" * ".join(turn_parts))
                turn_parts = []
                current_turn = turn_id
            turn_parts.append(text)
        if turn_parts:
            flow_parts.append(" * ".join(turn_parts))
        parts.append("{}: {}".format(label, " -> ".join(flow_parts)))
    return " | ".join(parts)

def wrap_text(value, width):
    if not value or value == "-":
        return ["-"]
    wrapped = textwrap.wrap(value, width=width, break_long_words=False, break_on_hyphens=False)
    return wrapped or [value]

rows = []
for task in sorted_tasks:
    flow_str = format_flow(per_task_flows.get(task, []))
    for idx, line in enumerate(wrap_text(flow_str, 120)):
        task_id = task_request_id(task) if idx == 0 else ""
        task_name = task if idx == 0 else ""
        rows.append((task_id, task_name, line))
print_table("Per-task flow (sorted by tool calls)", rows, ["id", "task", "flow"])
PY
else
    echo -e "${RED}No results directory for: $NAME${NC}"
fi

# Summary
echo -e "\n${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  BENCHMARK COMPLETE${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "Results in: ${YELLOW}$BENCHMARK_DIR/tmp.benchmark/$NAME*${NC}"
