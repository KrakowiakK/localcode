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

# Domyślne wartości
AGENT="${AGENT:-localcode}"
LOCALCODE_AGENT_CONFIG="${LOCALCODE_AGENT_CONFIG:-localcode}"
NAME="${NAME:-localcode-bench}"
LOCALCODE_TURN_SUMMARY="${LOCALCODE_TURN_SUMMARY:-1}"
LOCALCODE_STREAM_OUTPUT="${LOCALCODE_STREAM_OUTPUT:-1}"
export LOCALCODE_TURN_SUMMARY
export LOCALCODE_STREAM_OUTPUT
export LOCALCODE_AGENT_CONFIG

# Wykryj port serwera z konfiguracji agenta lub użyj domyślnego
SERVER_PORT="${BENCHMARK_SERVER_PORT:-}"
if [ -z "$SERVER_PORT" ]; then
    # Spróbuj odczytać port z URL w konfiguracji agenta
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

# Sprawdź czy serwer działa
echo "Sprawdzam serwer na porcie $SERVER_PORT..."
if ! curl -s "http://127.0.0.1:${SERVER_PORT}/health" >/dev/null 2>&1; then
    echo "BŁĄD: Serwer nie odpowiada na localhost:${SERVER_PORT}"
    echo "Uruchom: ./bin/start-server.sh <agent> --background"
    exit 1
fi
echo "Serwer działa (port $SERVER_PORT)"

# Sprawdź czy localcode istnieje
if [ ! -d "$LOCALCODE_DIR" ]; then
    echo "BŁĄD: Nie znaleziono $LOCALCODE_DIR"
    exit 1
fi

# Sprawdź czy benchmark dir istnieje
if [ ! -d "$BENCHMARK_DIR" ]; then
    echo "BŁĄD: Nie znaleziono $BENCHMARK_DIR"
    echo "Uruchom: ./bin/setup-benchmark.sh"
    exit 1
fi

# Sprawdź czy obraz Docker istnieje
if ! docker images --format '{{.Repository}}' | grep -q "^benchmark-localcode$"; then
    echo "BŁĄD: Docker image 'benchmark-localcode' nie znaleziony."
    echo "Uruchom: ./bin/setup-benchmark.sh"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Localcode Benchmark (Docker)"
echo "  Agent: $AGENT"
echo "  Agent config: $LOCALCODE_AGENT_CONFIG"
echo "═══════════════════════════════════════════════════════"
echo ""

# Uruchom benchmark w Docker
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
    -e BENCHMARK_SERVER_PORT \
    benchmark-localcode \
    python3 /benchmark/benchmark.py \
        --exercises-dir /benchmarks/polyglot-benchmark \
        --results-dir /results \
        --tries ${BENCHMARK_TRIES:-1} \
        --new "$NAME" \
        "$@"

echo ""
echo "Benchmark zakończony!"
