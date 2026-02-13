#!/bin/bash
#
# Docker runner for Localcode benchmark
#
# Runs benchmark.py inside Docker with proper mounts.
# Called by run-benchmark.sh.
#
# Usage:
#   AGENT=localcode NAME=my-run ./bin/run-localcode-benchmark.sh [benchmark.py args...]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BENCHMARK_DIR="$PROJECT_DIR/benchmark"
LOCALCODE_DIR="$PROJECT_DIR/localcode"

# Default values
AGENT="${AGENT:-localcode}"
LOCALCODE_AGENT_CONFIG="${LOCALCODE_AGENT_CONFIG:-localcode}"
NAME="${NAME:-localcode-bench}"

LOCALCODE_FLAGS_LIB="$SCRIPT_DIR/localcode-flags.sh"
if [ ! -f "$LOCALCODE_FLAGS_LIB" ]; then
    echo "ERROR: Missing shared LOCALCODE flags file: $LOCALCODE_FLAGS_LIB"
    exit 1
fi
# shellcheck source=bin/localcode-flags.sh
source "$LOCALCODE_FLAGS_LIB"
localcode_apply_flag_defaults

export LOCALCODE_AGENT_CONFIG

# Detect server port from agent config or use default
SERVER_PORT="${BENCHMARK_SERVER_PORT:-}"
if [ -z "$SERVER_PORT" ]; then
    # Try to read the port from the agent config URL
    for AGENT_DIR in "mlx" "gguf" ""; do
        if [ -n "$AGENT_DIR" ]; then
            AGENT_FILE="$LOCALCODE_DIR/agents/$AGENT_DIR/${LOCALCODE_AGENT_CONFIG}.json"
        else
            AGENT_FILE="$LOCALCODE_DIR/agents/${LOCALCODE_AGENT_CONFIG}.json"
        fi
        if [ -f "$AGENT_FILE" ]; then
            SERVER_PORT=$(python3 -c "
import json, re
try:
    data = json.load(open('$AGENT_FILE'))
    url = data.get('url', '')
    match = re.search(r':(\d+)/', url)
    if match:
        print(match.group(1))
except: pass
" 2>/dev/null)
            [ -n "$SERVER_PORT" ] && break
        fi
    done
fi
SERVER_PORT="${SERVER_PORT:-1234}"

# Check if the server is running
echo "Checking server on port $SERVER_PORT..."
if ! curl -s "http://127.0.0.1:${SERVER_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: Server not responding on localhost:${SERVER_PORT}"
    echo "Run: ./bin/start-server.sh <agent> --background"
    exit 1
fi
echo "Server is up (port $SERVER_PORT)"

# Check if localcode exists
if [ ! -d "$LOCALCODE_DIR" ]; then
    echo "ERROR: Not found $LOCALCODE_DIR"
    exit 1
fi

# Check if benchmark dir exists
if [ ! -d "$BENCHMARK_DIR" ]; then
    echo "ERROR: Not found $BENCHMARK_DIR"
    echo "Run: ./bin/setup-benchmark.sh"
    exit 1
fi

# Check if Docker image exists
if ! docker images --format '{{.Repository}}' | grep -q "^benchmark-localcode$"; then
    echo "ERROR: Docker image 'benchmark-localcode' not found."
    echo "Run: ./bin/setup-benchmark.sh"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Localcode Benchmark (Docker)"
echo "  Agent: $AGENT"
echo "  Agent config: $LOCALCODE_AGENT_CONFIG"
echo "═══════════════════════════════════════════════════════"
echo ""

# Run benchmark in Docker
docker run --rm \
    --memory=12g \
    --add-host=host.docker.internal:host-gateway \
    -v "$BENCHMARK_DIR:/benchmark:ro" \
    -v "$BENCHMARK_DIR/tmp.benchmarks:/benchmarks:ro" \
    -v "$BENCHMARK_DIR/tmp.benchmark:/results" \
    -v "$LOCALCODE_DIR:/localcode" \
    -e AIDER_DOCKER=1 \
    -e BENCHMARK_DIR=/results \
    -e PYTHONUNBUFFERED=1 \
    -e AGENT_ARGS \
    -e LOCALCODE_AGENT_ARGS \
    -e LOCALCODE_AGENT_CONFIG \
    -e LOCALCODE_TURN_SUMMARY \
    -e LOCALCODE_STREAM_OUTPUT \
    -e LOCALCODE_TASK_OUTPUT_MODE \
    -e LOCALCODE_BENCHMARK_OUTPUT_MODE \
    -e LOCALCODE_TASK_SKIP_READONLY \
    -e LOCALCODE_WRITE_VERBOSE_STATE \
    -e LOCALCODE_EDIT_VERBOSE_STATE \
    -e LOCALCODE_EDIT_SNIPPET_SUCCESS \
    -e LOCALCODE_WRITE_SNIPPET_SUCCESS \
    -e LOCALCODE_ENFORCE_READ_BEFORE_WRITE \
    -e LOCALCODE_INJECT_TESTS_ON_WRITE \
    -e LOCALCODE_WRITE_SPEC_FOCUS \
    -e LOCALCODE_WRITE_SPEC_CONTRACT \
    -e LOCALCODE_WRITE_FULL_DROP \
    -e LOCALCODE_PATH_AUTOCORRECT_GLOBAL \
    -e LOCALCODE_READ_LINE_NUMBERS \
    -e LOCALCODE_READ_STYLE \
    -e LOCALCODE_EDIT_HASH_ANCHOR \
    -e LOCALCODE_SNIPPET_STYLE \
    -e LOCALCODE_FLAG_KEYS_CSV \
    -e BENCHMARK_SERVER_PORT \
    benchmark-localcode \
    python3 /benchmark/benchmark.py \
        --exercises-dir /benchmarks/polyglot-benchmark \
        --results-dir /results \
        --tries ${BENCHMARK_TRIES:-1} \
        --new "$NAME" \
        "$@"

echo ""
echo "Benchmark finished!"
