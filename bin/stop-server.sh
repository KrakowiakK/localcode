#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  stop-server.sh — Stop llama-server started by start-server.sh
# ══════════════════════════════════════════════════════════════

PID_FILE="/tmp/benchmark-server.pid"

echo "Stopping benchmark server..."

# ── Kill from PID file ────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "Sending SIGTERM to PID $PID..."
        kill "$PID" 2>/dev/null || true

        # Wait up to 10 seconds for graceful shutdown
        WAITED=0
        while [[ $WAITED -lt 10 ]]; do
            if ! kill -0 "$PID" 2>/dev/null; then
                echo "Process $PID terminated."
                break
            fi
            sleep 1
            WAITED=$((WAITED + 1))
        done

        # Force kill if still running
        if kill -0 "$PID" 2>/dev/null; then
            echo "Sending SIGKILL to PID $PID..."
            kill -9 "$PID" 2>/dev/null || true
        fi
    else
        echo "PID $PID is not running (stale PID file)."
    fi
    rm -f "$PID_FILE"
else
    echo "No PID file found at $PID_FILE."
fi

# ── Kill any remaining llama-server processes ────────────────
if pgrep -f "llama-server" > /dev/null 2>&1; then
    echo "Killing remaining llama-server processes..."
    pkill -f "llama-server" 2>/dev/null || true
    sleep 1
    if pgrep -f "llama-server" > /dev/null 2>&1; then
        pkill -9 -f "llama-server" 2>/dev/null || true
    fi
fi

# ── Clean up ─────────────────────────────────────────────────
rm -f "$PID_FILE"

echo ""
echo "Server stopped."
