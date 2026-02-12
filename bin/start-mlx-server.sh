#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  start-mlx-server.sh — Start mlx_lm.server from agent JSON config
# ══════════════════════════════════════════════════════════════
#
# Usage:
#   ./bin/start-mlx-server.sh <agent> [--port PORT] [--background]
#
# Examples:
#   ./bin/start-mlx-server.sh qwen3-coder-next-8bit
#   ./bin/start-mlx-server.sh qwen3-coder-next-8bit --background
#   ./bin/start-mlx-server.sh qwen3-coder-next-8bit --port 1237

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENTS_DIR="$PROJECT_DIR/localcode/agents"
PID_FILE="/tmp/benchmark-mlx-server.pid"
MLX_LOG="/tmp/benchmark-mlx-server.log"

# ── Find mlx_lm.server binary ─────────────────────────────────
if [[ -n "${MLX_SERVER:-}" ]] && [[ -x "$MLX_SERVER" ]]; then
    : # use MLX_SERVER from env
elif command -v mlx_lm.server &>/dev/null; then
    MLX_SERVER="$(command -v mlx_lm.server)"
else
    MLX_SERVER=""
fi

# ── Parse CLI arguments ──────────────────────────────────────
AGENT_NAME=""
PORT_OVERRIDE=""
BACKGROUND=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT_OVERRIDE="$2"
            shift 2
            ;;
        --background)
            BACKGROUND=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 <agent> [--port PORT] [--background]"
            echo ""
            echo "Start mlx_lm.server based on agent JSON configuration."
            echo ""
            echo "Arguments:"
            echo "  <agent>        Agent name (e.g. qwen3-coder-next-8bit)"
            echo "  --port PORT    Override port from agent URL"
            echo "  --background   Run in background, write PID to $PID_FILE"
            echo ""
            echo "Environment:"
            echo "  MLX_SERVER     Path to mlx_lm.server binary (auto-detected if not set)"
            echo ""
            echo "Available MLX agents:"
            for f in "$AGENTS_DIR/mlx/"*.json; do
                [[ -f "$f" ]] || continue
                name="$(basename "$f" .json)"
                echo "  $name"
            done
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1"
            exit 1
            ;;
        *)
            AGENT_NAME="$1"
            shift
            ;;
    esac
done

if [[ -z "$AGENT_NAME" ]]; then
    echo "Error: No agent specified."
    echo "Usage: $0 <agent> [--port PORT] [--background]"
    echo ""
    echo "Available MLX agents:"
    for f in "$AGENTS_DIR/mlx/"*.json; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f" .json)"
        echo "  $name"
    done
    exit 1
fi

# ── Find agent JSON ──────────────────────────────────────────
AGENT_FILE="$AGENTS_DIR/mlx/${AGENT_NAME}.json"
if [[ ! -f "$AGENT_FILE" ]]; then
    echo "Error: Agent '${AGENT_NAME}' not found."
    echo "  Searched: $AGENT_FILE"
    echo ""
    echo "Available MLX agents:"
    for f in "$AGENTS_DIR/mlx/"*.json; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f" .json)"
        echo "  $name"
    done
    exit 1
fi

# ── Check mlx_lm.server binary ────────────────────────────────
if [[ -z "$MLX_SERVER" || ! -x "$MLX_SERVER" ]]; then
    echo "Error: mlx_lm.server binary not found."
    echo ""
    echo "Install with:"
    echo "  pip install mlx-lm"
    exit 1
fi

# ── Read agent JSON & compute config ─────────────────────────
eval "$(python3 << PYEOF
import json, sys, os
from urllib.parse import urlparse

agent_file = "$AGENT_FILE"
home_dir = os.path.expanduser("~")

with open(agent_file) as f:
    data = json.load(f)

sc = data.get("server_config")
if not sc:
    print("echo 'Error: No server_config in agent JSON'; exit 1")
    sys.exit(0)

name = data.get("name", "")
model = data.get("model", "")
url = data.get("url", "")
model_path_raw = sc.get("model_path", "")
context_window = sc.get("context_window", 32768)
extra_args = sc.get("extra_args", [])

# Expand ~ in model_path
model_path = model_path_raw.replace("~", home_dir, 1) if model_path_raw.startswith("~") else model_path_raw

# Extract port from URL
parsed = urlparse(url)
url_port = parsed.port or 1236

def sh_escape(s):
    return "'" + s.replace("'", "'\\\\''") + "'"

print(f"AGENT_DISPLAY={sh_escape(name)}")
print(f"MODEL_ID={sh_escape(model)}")
print(f"MODEL_PATH_RAW={sh_escape(model_path_raw)}")
print(f"MODEL_PATH={sh_escape(model_path)}")
print(f"CONTEXT_WINDOW={sh_escape(str(context_window))}")
print(f"URL_PORT={url_port}")

# Output extra args as array
if extra_args:
    args_str = " ".join(sh_escape(a) for a in extra_args)
    print(f"EXTRA_ARGS=({args_str})")
    print(f"EXTRA_DISPLAY={sh_escape(' '.join(extra_args))}")
else:
    print("EXTRA_ARGS=()")
    print("EXTRA_DISPLAY='(none)'")
PYEOF
)"

# ── Check model directory exists ──────────────────────────────
if [[ ! -d "$MODEL_PATH" ]]; then
    echo "Error: Model directory not found: $MODEL_PATH"
    echo "  (from: $MODEL_PATH_RAW)"
    exit 1
fi

# ── Determine port ───────────────────────────────────────────
if [[ -n "$PORT_OVERRIDE" ]]; then
    SERVER_PORT="$PORT_OVERRIDE"
else
    SERVER_PORT="$URL_PORT"
fi

# ── Check if server already running ──────────────────────────
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE")"
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Warning: MLX server already running (PID $OLD_PID)"
        echo "Run ./bin/stop-mlx-server.sh first, or remove $PID_FILE"
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

# ── Build mlx_lm.server command ──────────────────────────────
MLX_CMD=(
    "$MLX_SERVER"
    "--model" "$MODEL_PATH"
    "--host" "127.0.0.1"
    "--port" "$SERVER_PORT"
    "${EXTRA_ARGS[@]}"
)

# ── Print summary ────────────────────────────────────────────
echo "══════════════════════════════════════════════"
echo "  Starting MLX server..."
echo "══════════════════════════════════════════════"
echo "Agent:      $AGENT_DISPLAY"
echo "Model:      $MODEL_ID"
echo "Path:       $MODEL_PATH_RAW"
echo "Port:       $SERVER_PORT"
echo "Context:    $CONTEXT_WINDOW"
echo "Extra args: $EXTRA_DISPLAY"
echo "Background: $BACKGROUND"
echo "Binary:     $MLX_SERVER"
echo "──────────────────────────────────────────────"

# ── Start mlx_lm.server ─────────────────────────────────────
echo "Starting mlx_lm.server..."
echo "Command: ${MLX_CMD[*]}"
echo ""

"${MLX_CMD[@]}" > "$MLX_LOG" 2>&1 &
MLX_PID=$!
echo "$MLX_PID" > "$PID_FILE"

# ── Wait for server ready ────────────────────────────────────
# mlx_lm.server exposes /v1/models as health check
HEALTH_URL="http://127.0.0.1:${SERVER_PORT}/v1/models"

echo "Waiting for mlx_lm.server (PID $MLX_PID)..."
MAX_WAIT=300
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
    if ! kill -0 "$MLX_PID" 2>/dev/null; then
        echo "Error: mlx_lm.server exited unexpectedly."
        echo "Check logs: $MLX_LOG"
        tail -20 "$MLX_LOG"
        rm -f "$PID_FILE"
        exit 1
    fi
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo "mlx_lm.server is ready (${WAITED}s)."
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
    echo "Error: mlx_lm.server did not start within ${MAX_WAIT}s."
    echo "Check logs: $MLX_LOG"
    tail -20 "$MLX_LOG"
    kill "$MLX_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  MLX server started"
echo "══════════════════════════════════════════════"
echo "Agent:      $AGENT_DISPLAY"
echo "Model:      $MODEL_ID"
echo "Path:       $MODEL_PATH_RAW"
echo "Port:       $SERVER_PORT"
echo "Extra args: $EXTRA_DISPLAY"
echo ""
echo "Models:     curl http://127.0.0.1:${SERVER_PORT}/v1/models"
echo "Logs:       tail -f $MLX_LOG"
echo "Stop:       ./bin/stop-mlx-server.sh"
echo "══════════════════════════════════════════════"

# ── Foreground or background ─────────────────────────────────
if [[ "$BACKGROUND" == "true" ]]; then
    echo ""
    echo "Running in background. PID file: $PID_FILE"
    exit 0
else
    echo ""
    echo "Running in foreground. Press Ctrl+C to stop."

    cleanup() {
        echo ""
        echo "Stopping MLX server..."
        kill "$MLX_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "MLX server stopped."
        exit 0
    }
    trap cleanup SIGINT SIGTERM

    wait "$MLX_PID" 2>/dev/null || true
    cleanup
fi
