#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$AGENT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVER_URL="${SERVER_URL:-http://localhost:1234}"
MODEL="${MODEL:-gpt-oss-120b@8bit}"
AGENT="${AGENT:-benchmark}"
PROMPT="${PROMPT:-Call the ls tool.}"

LOG_DIR="$AGENT_DIR/logs"
mkdir -p "$LOG_DIR"

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required for server health check" >&2
  exit 1
fi

if ! curl -fsS "$SERVER_URL/health" >/dev/null; then
  echo "error: server not healthy at $SERVER_URL/health" >&2
  exit 1
fi

timestamp="$(date +%Y-%m-%d_%H-%M-%S)"
summary_log="$LOG_DIR/smoke_matrix_${timestamp}.log"
echo "localcode smoke matrix (${timestamp})" | tee "$summary_log"
echo "Server: $SERVER_URL | Model: $MODEL | Agent: $AGENT" | tee -a "$summary_log"
echo "" | tee -a "$summary_log"

cases=(
  "base|true|low|false|auto|false"
  "no_think|false|low|false|auto|false"
  "native_thinking|true|low|true|auto|false"
  "tool_required|true|low|false|required|false"
  "cache_on|true|low|false|auto|true"
  "reason_high|true|high|false|auto|false"
)

fail_count=0

analyze_log() {
  local log_path="$1"
  local name="$2"
  local expect_think="$3"
  local expect_effort="$4"
  local expect_native_thinking="$5"
  local expect_tool_choice="$6"
  local expect_cache="$7"

  "$PYTHON_BIN" - "$log_path" "$name" "$expect_think" "$expect_effort" "$expect_native_thinking" \
    "$expect_tool_choice" "$expect_cache" <<'PY'
import json
import sys

log_path, name, expect_think, expect_effort, expect_native_thinking, expect_tool_choice, expect_cache = sys.argv[1:8]
expect_think = expect_think.lower()
expect_cache = expect_cache.lower()
expect_native_thinking = expect_native_thinking.lower()

request = None
tool_results = 0
agent_done = False
format_retries = 0

with open(log_path, "r", encoding="utf-8") as f:
    for line in f:
        data = json.loads(line)
        event = data.get("event")
        if event == "request" and request is None:
            request = data.get("request_params", {})
        if event == "tool_result":
            tool_results += 1
        if event == "agent_done":
            agent_done = True
        if event == "format_retry":
            format_retries += 1

errors = []
if request is None:
    errors.append("missing_request")
else:
    tool_choice = request.get("tool_choice")
    if tool_choice != expect_tool_choice:
        errors.append(f"tool_choice={tool_choice} expected={expect_tool_choice}")
    reasoning = request.get("reasoning_effort")
    if expect_think == "true" and reasoning != expect_effort:
        errors.append(f"reasoning_effort={reasoning} expected={expect_effort}")
    think_val = request.get("think")
    if expect_think == "true" and think_val is not True:
        errors.append(f"think={think_val} expected=True")
    if expect_think == "false" and think_val is not False:
        errors.append(f"think={think_val} expected=False")
    cache_val = request.get("cache")
    if expect_cache == "true" and cache_val is not True:
        errors.append(f"cache={cache_val} expected=True")
    if expect_cache == "false" and cache_val is not False:
        errors.append(f"cache={cache_val} expected=False")
    if expect_think == "true" or expect_native_thinking == "true":
        if request.get("return_thinking") is not True:
            errors.append("return_thinking not enabled")

if tool_results == 0:
    errors.append("no_tool_calls")
if not agent_done:
    errors.append("agent_done_missing")

status = "OK" if not errors else "FAIL"
print(f"{name}: {status} | tool_calls={tool_results} format_retries={format_retries} errors={','.join(errors)}")
sys.exit(0 if not errors else 1)
PY
}

for entry in "${cases[@]}"; do
  IFS="|" read -r name think effort native_thinking tool_choice cache <<< "$entry"
  echo "==> $name (think=$think effort=$effort native_thinking=$native_thinking tool_choice=$tool_choice cache=$cache)" | tee -a "$summary_log"

  before_log="$(ls -t "$LOG_DIR"/localcode_* 2>/dev/null | head -n 1 || true)"

  set +e
  "$PYTHON_BIN" "$AGENT_DIR/localcode.py" \
    --agent "$AGENT" \
    --url "$SERVER_URL/v1/chat/completions" \
    --model "$MODEL" \
    --max_tokens 256 \
    --temperature 0 \
    --top_p 0.1 \
    --top_k 1 \
    --think "$think" \
    --think_level "$effort" \
    --native_thinking "$native_thinking" \
    --tool_choice "$tool_choice" \
    --cache "$cache" \
    "$PROMPT" >> "$summary_log" 2>&1
  run_status=$?
  set -e

  sleep 1
  after_log="$(ls -t "$LOG_DIR"/localcode_* 2>/dev/null | head -n 1 || true)"
  if [ -z "$after_log" ] || [ "$after_log" = "$before_log" ]; then
    echo "  $name: FAIL (no new log found)" | tee -a "$summary_log"
    fail_count=$((fail_count + 1))
    continue
  fi

  set +e
  analyze_log "$after_log" "$name" "$think" "$effort" "$native_thinking" "$tool_choice" "$cache" \
    | tee -a "$summary_log"
  analyze_status=${PIPESTATUS[0]}
  set -e
  if [ $analyze_status -ne 0 ] || [ $run_status -ne 0 ]; then
    fail_count=$((fail_count + 1))
  fi
  echo "" | tee -a "$summary_log"
done

echo "Smoke matrix completed. Failures: $fail_count" | tee -a "$summary_log"
echo "Summary log: $summary_log"

if [ "$fail_count" -ne 0 ]; then
  exit 1
fi
