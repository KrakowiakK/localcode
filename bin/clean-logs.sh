#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
#  clean-logs.sh — Remove logs, sessions, and benchmark results
# ══════════════════════════════════════════════════════════════
#
# Usage:
#   ./bin/clean-logs.sh            # Dry run (show what would be deleted)
#   ./bin/clean-logs.sh --force    # Actually delete
#
# What it cleans:
#   1. localcode/logs/*            — agent run logs (.jsonl, .log, .raw.json)
#   2. localcode/.localcode/sessions/* — agent session state
#   3. benchmark/tmp.benchmark/*   — benchmark task results
#   4. /tmp/benchmark-llama-server.log — llama-server log

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LOGS_DIR="$PROJECT_DIR/localcode/logs"
SESSIONS_DIR="$PROJECT_DIR/localcode/.localcode/sessions"
RESULTS_DIR="$PROJECT_DIR/benchmark/tmp.benchmark"
SERVER_LOG="/tmp/benchmark-llama-server.log"

FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            FORCE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--force]"
            echo ""
            echo "Remove logs, sessions, and benchmark results."
            echo ""
            echo "Options:"
            echo "  --force, -f   Actually delete (default is dry run)"
            echo ""
            echo "Targets:"
            echo "  localcode/logs/*                  Agent run logs"
            echo "  localcode/.localcode/sessions/*   Agent sessions"
            echo "  benchmark/tmp.benchmark/*         Benchmark results"
            echo "  /tmp/benchmark-llama-server.log   Server log"
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1"
            exit 1
            ;;
    esac
done

# ── Count files/dirs in each target ──────────────────────────
count_items() {
    local dir="$1"
    if [[ -d "$dir" ]]; then
        find "$dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' '
    else
        echo "0"
    fi
}

LOGS_COUNT="$(count_items "$LOGS_DIR")"
SESSIONS_COUNT="$(count_items "$SESSIONS_DIR")"
RESULTS_COUNT="$(count_items "$RESULTS_DIR")"
SERVER_LOG_EXISTS=false
SERVER_LOG_SIZE="0"
if [[ -f "$SERVER_LOG" ]]; then
    SERVER_LOG_EXISTS=true
    SERVER_LOG_SIZE="$(du -sh "$SERVER_LOG" 2>/dev/null | cut -f1)"
fi

# ── Calculate total size ─────────────────────────────────────
total_size() {
    local total=0
    for dir in "$LOGS_DIR" "$SESSIONS_DIR" "$RESULTS_DIR"; do
        if [[ -d "$dir" ]]; then
            local s
            s="$(du -sk "$dir" 2>/dev/null | cut -f1)"
            total=$((total + s))
        fi
    done
    if [[ -f "$SERVER_LOG" ]]; then
        local s
        s="$(du -sk "$SERVER_LOG" 2>/dev/null | cut -f1)"
        total=$((total + s))
    fi
    if [[ $total -ge 1024 ]]; then
        echo "$((total / 1024)) MB"
    else
        echo "${total} KB"
    fi
}

TOTAL="$(total_size)"

# ── Summary ──────────────────────────────────────────────────
echo "══════════════════════════════════════════════"
if [[ "$FORCE" == "true" ]]; then
    echo "  Cleaning logs & results"
else
    echo "  Dry run (use --force to delete)"
fi
echo "══════════════════════════════════════════════"
echo "Agent logs:       $LOGS_COUNT files     ($LOGS_DIR)"
echo "Agent sessions:   $SESSIONS_COUNT dirs      ($SESSIONS_DIR)"
echo "Benchmark results:$RESULTS_COUNT dirs      ($RESULTS_DIR)"
if [[ "$SERVER_LOG_EXISTS" == "true" ]]; then
    echo "Server log:       $SERVER_LOG_SIZE         ($SERVER_LOG)"
else
    echo "Server log:       (not found)"
fi
echo "──────────────────────────────────────────────"
echo "Total:            ~$TOTAL"
echo ""

if [[ "$LOGS_COUNT" -eq 0 && "$SESSIONS_COUNT" -eq 0 && "$RESULTS_COUNT" -eq 0 && "$SERVER_LOG_EXISTS" == "false" ]]; then
    echo "Nothing to clean."
    exit 0
fi

# ── Delete or dry-run ────────────────────────────────────────
if [[ "$FORCE" == "true" ]]; then
    [[ "$LOGS_COUNT" -gt 0 ]]              && rm -rf "$LOGS_DIR"/* && echo "Deleted: agent logs"
    [[ "$SESSIONS_COUNT" -gt 0 ]]          && rm -rf "$SESSIONS_DIR"/* && echo "Deleted: agent sessions"
    [[ "$RESULTS_COUNT" -gt 0 ]]           && rm -rf "$RESULTS_DIR"/* && echo "Deleted: benchmark results"
    [[ "$SERVER_LOG_EXISTS" == "true" ]]    && rm -f "$SERVER_LOG" && echo "Deleted: server log"

    echo ""
    echo "Done. All clean."
else
    echo "This is a dry run. Run with --force to actually delete."
fi
