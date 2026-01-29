#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  start-server.sh — Start llama-server from agent JSON config
# ══════════════════════════════════════════════════════════════
#
# Usage:
#   ./bin/start-server.sh <agent> [--port PORT] [--background]
#
# Examples:
#   ./bin/start-server.sh jan-v3-4b
#   ./bin/start-server.sh gpt-oss-120b-mxfp4 --background
#   ./bin/start-server.sh glm-4.7-flash --port 1236

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENTS_DIR="$PROJECT_DIR/localcode/agents"
PID_FILE="/tmp/benchmark-server.pid"
LLAMA_LOG="/tmp/benchmark-llama-server.log"

# ── Find llama-server binary ─────────────────────────────────
# Priority: 1) LLAMA_SERVER env var  2) project-local build  3) system PATH
if [[ -n "${LLAMA_SERVER:-}" ]] && [[ -x "$LLAMA_SERVER" ]]; then
    : # use LLAMA_SERVER from env
elif [[ -x "$SCRIPT_DIR/llama-server" ]]; then
    LLAMA_SERVER="$SCRIPT_DIR/llama-server"
elif command -v llama-server &>/dev/null; then
    LLAMA_SERVER="$(command -v llama-server)"
else
    LLAMA_SERVER=""  # will be caught later
fi

# ── Default llama-server args (from server.yaml) ─────────────
DEFAULT_ARGS=(
    "--ctx-size"      "32768"
    "--n-gpu-layers"  "99"
    "--flash-attn"    "on"
    "--batch-size"    "8192"
    "--ubatch-size"   "1024"
)

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
            echo "Start llama-server based on agent JSON configuration."
            echo ""
            echo "Arguments:"
            echo "  <agent>        Agent name (e.g. jan-v3-4b, gpt-oss-120b-mxfp4)"
            echo "  --port PORT    Override port from agent URL"
            echo "  --background   Run in background, write PID to $PID_FILE"
            echo ""
            echo "Environment:"
            echo "  LLAMA_SERVER   Path to llama-server binary (auto-detected if not set)"
            echo "                 Searches: \$LLAMA_SERVER → bin/ → system PATH"
            echo ""
            echo "Available agents:"
            for f in "$AGENTS_DIR/gguf/"*.json; do
                name="$(basename "$f" .json)"
                [[ "$name" == "code-architect" ]] && continue
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
    echo "Available agents:"
    for f in "$AGENTS_DIR/gguf/"*.json; do
        name="$(basename "$f" .json)"
        [[ "$name" == "code-architect" ]] && continue
        echo "  $name"
    done
    exit 1
fi

# ── Find agent JSON ──────────────────────────────────────────
AGENT_FILE=""
if [[ -f "$AGENTS_DIR/gguf/${AGENT_NAME}.json" ]]; then
    AGENT_FILE="$AGENTS_DIR/gguf/${AGENT_NAME}.json"
elif [[ -f "$AGENTS_DIR/mlx/${AGENT_NAME}.json" ]]; then
    AGENT_FILE="$AGENTS_DIR/mlx/${AGENT_NAME}.json"
else
    echo "Error: Agent '${AGENT_NAME}' not found."
    echo ""
    echo "Searched:"
    echo "  $AGENTS_DIR/gguf/${AGENT_NAME}.json"
    echo "  $AGENTS_DIR/mlx/${AGENT_NAME}.json"
    echo ""
    echo "Available agents:"
    for f in "$AGENTS_DIR/gguf/"*.json; do
        name="$(basename "$f" .json)"
        [[ "$name" == "code-architect" ]] && continue
        echo "  $name"
    done
    exit 1
fi

# ── Check llama-server binary ────────────────────────────────
if [[ -z "$LLAMA_SERVER" || ! -x "$LLAMA_SERVER" ]]; then
    echo "Error: llama-server binary not found."
    echo ""
    echo "Searched:"
    echo "  1. \$LLAMA_SERVER env var (not set)"
    echo "  2. $SCRIPT_DIR/llama-server (not found)"
    echo "  3. system PATH (not found)"
    echo ""
    echo "Options:"
    echo "  export LLAMA_SERVER=/path/to/llama-server"
    echo "  ./bin/build-llama.sh"
    echo "  brew install llama.cpp"
    exit 1
fi

# ── Read agent JSON & compute all config ─────────────────────
# Single Python call: reads JSON, expands paths, merges args, outputs shell variables
eval "$(python3 << PYEOF
import json, sys, os
from urllib.parse import urlparse

agent_file = "$AGENT_FILE"
project_dir = "$PROJECT_DIR"
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
hf_model = sc.get("hf_model", "")

# Expand ~ in model_path
model_path = model_path_raw.replace("~", home_dir, 1) if model_path_raw.startswith("~") else model_path_raw

# Extract port from URL
parsed = urlparse(url)
url_port = parsed.port or 1235

# Expand relative paths in extra_args (e.g. llama.cpp/models/... → absolute)
expanded_extra = []
for arg in extra_args:
    if not arg.startswith("/") and not arg.startswith("~") and "/" in arg:
        candidate = os.path.join(project_dir, arg)
        if os.path.exists(candidate):
            expanded_extra.append(candidate)
            continue
    expanded_extra.append(arg)

# Merge default args with extra_args (extra overrides defaults)
defaults = ["--ctx-size", "32768", "--n-gpu-layers", "99", "--flash-attn", "on", "--batch-size", "8192", "--ubatch-size", "1024"]

extra_flags = set()
for arg in expanded_extra:
    if arg.startswith("--"):
        extra_flags.add(arg)

merged = []
i = 0
while i < len(defaults):
    flag = defaults[i]
    if flag.startswith("--") and flag in extra_flags:
        i += 1
        if i < len(defaults) and not defaults[i].startswith("--"):
            i += 1
        continue
    merged.append(defaults[i])
    i += 1
merged.extend(expanded_extra)

# Shell-escape a string for safe eval
def sh_escape(s):
    return "'" + s.replace("'", "'\\''") + "'"

# Output shell variable assignments
print(f"AGENT_DISPLAY={sh_escape(name)}")
print(f"MODEL_ID={sh_escape(model)}")
print(f"MODEL_PATH_RAW={sh_escape(model_path_raw)}")
print(f"MODEL_PATH={sh_escape(model_path)}")
print(f"CONTEXT_WINDOW={sh_escape(str(context_window))}")
print(f"URL_PORT={url_port}")
print(f"HF_MODEL={sh_escape(hf_model)}")

# Output expanded extra as display string
if expanded_extra:
    print(f"EXTRA_DISPLAY={sh_escape(' '.join(expanded_extra))}")
else:
    print("EXTRA_DISPLAY='(none)'")

# Output merged args as array
args_str = " ".join(sh_escape(a) for a in merged)
print(f"FINAL_ARGS=({args_str})")
PYEOF
)"

# ── Resolve model: local path or HuggingFace download ────────
if [[ -f "$MODEL_PATH" ]]; then
    MODEL_SOURCE="$MODEL_PATH"
    MODEL_SOURCE_DISPLAY="local: $MODEL_PATH_RAW"
elif [[ -n "$HF_MODEL" ]]; then
    echo "Local model not found: $MODEL_PATH_RAW"
    echo "Using HuggingFace: $HF_MODEL (llama.cpp will download)"
    MODEL_SOURCE="$HF_MODEL"
    MODEL_SOURCE_DISPLAY="hf: $HF_MODEL"
else
    echo "Error: Model file not found: $MODEL_PATH"
    echo "  (from: $MODEL_PATH_RAW)"
    echo "  No hf_model configured as fallback."
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
        echo "Warning: Server already running (PID $OLD_PID)"
        echo "Run ./bin/stop-server.sh first, or remove $PID_FILE"
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

# ── Build llama-server command ───────────────────────────────
LLAMA_CMD=(
    "$LLAMA_SERVER"
    "--model" "$MODEL_SOURCE"
    "--host" "127.0.0.1"
    "--port" "$SERVER_PORT"
    "${FINAL_ARGS[@]}"
)

# ── Print summary ────────────────────────────────────────────
echo "══════════════════════════════════════════════"
echo "  Starting server..."
echo "══════════════════════════════════════════════"
echo "Agent:      $AGENT_DISPLAY"
echo "Model:      $MODEL_ID"
echo "Source:     $MODEL_SOURCE_DISPLAY"
echo "Port:       $SERVER_PORT"
echo "Context:    $CONTEXT_WINDOW"
echo "Extra args: $EXTRA_DISPLAY"
echo "Background: $BACKGROUND"
echo "Binary:     $LLAMA_SERVER"
echo "──────────────────────────────────────────────"

# ── Start llama-server ───────────────────────────────────────
echo "Starting llama-server..."
echo "Command: ${LLAMA_CMD[*]}"
echo ""

"${LLAMA_CMD[@]}" > "$LLAMA_LOG" 2>&1 &
LLAMA_PID=$!
echo "$LLAMA_PID" > "$PID_FILE"

# ── Wait for health check ────────────────────────────────────
HEALTH_URL="http://127.0.0.1:${SERVER_PORT}/health"

echo "Waiting for llama-server (PID $LLAMA_PID)..."
MAX_WAIT=300
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
    if ! kill -0 "$LLAMA_PID" 2>/dev/null; then
        echo "Error: llama-server exited unexpectedly."
        echo "Check logs: $LLAMA_LOG"
        tail -20 "$LLAMA_LOG"
        rm -f "$PID_FILE"
        exit 1
    fi
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo "llama-server is ready (${WAITED}s)."
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
    echo "Error: llama-server did not start within ${MAX_WAIT}s."
    echo "Check logs: $LLAMA_LOG"
    tail -20 "$LLAMA_LOG"
    kill "$LLAMA_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  Server started"
echo "══════════════════════════════════════════════"
echo "Agent:      $AGENT_DISPLAY"
echo "Model:      $MODEL_ID"
echo "Source:     $MODEL_SOURCE_DISPLAY"
echo "Port:       $SERVER_PORT"
echo "Extra args: $EXTRA_DISPLAY"
echo ""
echo "Health:     curl http://127.0.0.1:${SERVER_PORT}/health"
echo "Logs:       tail -f $LLAMA_LOG"
echo "Stop:       ./bin/stop-server.sh"
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
        echo "Stopping server..."
        kill "$LLAMA_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "Server stopped."
        exit 0
    }
    trap cleanup SIGINT SIGTERM

    wait "$LLAMA_PID" 2>/dev/null || true
    cleanup
fi
