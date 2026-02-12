#!/bin/bash
#
# Test MiniMax M2.1 Q8 variants on the React exercise
#
# Tests 5 different configurations to find optimal settings
# for reducing thinking loops while maintaining code quality.
#
# Usage:
#   ./bin/test-minimax-variants.sh
#
# Each variant:
#   1. Stops any existing server
#   2. Starts server with variant config
#   3. Runs React benchmark
#   4. Logs results
#
# Variants:
#   V1: baseline (temp=1.0, max_tokens=16000, no repeat-penalty)
#   V2: repeat-penalty=1.05
#   V3: repeat-penalty=1.1
#   V4: temp=0.7
#   V5: max_tokens=8000
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

VARIANTS=(
    "minimax-m2.1-q8-v1-baseline"
    "minimax-m2.1-q8-v2-rep105"
    "minimax-m2.1-q8-v3-rep110"
    "minimax-m2.1-q8-v4-temp07"
    "minimax-m2.1-q8-v5-maxtok8k"
)

DESCRIPTIONS=(
    "V1: BASELINE (temp=1.0, max_tokens=16k, no repeat-penalty)"
    "V2: repeat-penalty=1.05"
    "V3: repeat-penalty=1.1"
    "V4: temp=0.7"
    "V5: max_tokens=8000"
)

RESULTS_FILE="$PROJECT_DIR/localcode/logs/minimax-variant-test-$(date +%Y-%m-%d-%H-%M-%S).txt"

echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  MiniMax M2.1 Q8 — Variant Testing (React exercise)${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Variants to test: ${#VARIANTS[@]}"
echo "Results log: $RESULTS_FILE"
echo ""

# Header for results file
cat > "$RESULTS_FILE" << 'EOF'
# MiniMax M2.1 Q8 — Variant Test Results
# Task: React (JavaScript)
# Date: $(date)
#
# Variants:
#   V1: baseline (temp=1.0, max_tokens=16000, no repeat-penalty)
#   V2: repeat-penalty=1.05
#   V3: repeat-penalty=1.1
#   V4: temp=0.7
#   V5: max_tokens=8000
#
EOF

TOTAL=${#VARIANTS[@]}
CURRENT=0

for i in "${!VARIANTS[@]}"; do
    VARIANT="${VARIANTS[$i]}"
    DESC="${DESCRIPTIONS[$i]}"
    CURRENT=$((CURRENT + 1))

    echo -e "\n${YELLOW}════════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}  [$CURRENT/$TOTAL] $DESC${NC}"
    echo -e "${YELLOW}  Agent: $VARIANT${NC}"
    echo -e "${YELLOW}════════════════════════════════════════════════════════${NC}"

    # Stop existing server
    echo -e "${CYAN}Stopping existing server...${NC}"
    "$SCRIPT_DIR/stop-server.sh" 2>/dev/null || true
    sleep 3

    # Start server with this variant
    echo -e "${CYAN}Starting server for $VARIANT...${NC}"
    if ! "$SCRIPT_DIR/start-server.sh" "$VARIANT" --background; then
        echo -e "${RED}FAILED to start server for $VARIANT${NC}"
        echo "[$CURRENT] $VARIANT — SERVER START FAILED" >> "$RESULTS_FILE"
        continue
    fi

    # Give server a moment to stabilize
    sleep 5

    # Run benchmark
    echo -e "${CYAN}Running React benchmark...${NC}"
    START_TIME=$(date +%s)

    if "$SCRIPT_DIR/run-benchmark.sh" "$VARIANT" -k react 2>&1 | tee /tmp/minimax-variant-${VARIANT}.out; then
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - START_TIME))
        echo -e "${GREEN}Benchmark completed in ${ELAPSED}s${NC}"
        echo "" >> "$RESULTS_FILE"
        echo "═══════════════════════════════════════════" >> "$RESULTS_FILE"
        echo "[$CURRENT] $DESC" >> "$RESULTS_FILE"
        echo "Agent: $VARIANT" >> "$RESULTS_FILE"
        echo "Duration: ${ELAPSED}s" >> "$RESULTS_FILE"
        echo "═══════════════════════════════════════════" >> "$RESULTS_FILE"
        # Append benchmark output
        cat /tmp/minimax-variant-${VARIANT}.out >> "$RESULTS_FILE"
    else
        echo -e "${RED}Benchmark FAILED for $VARIANT${NC}"
        echo "[$CURRENT] $VARIANT — BENCHMARK FAILED" >> "$RESULTS_FILE"
    fi

    echo ""
done

# Stop server after all tests
echo -e "${CYAN}Stopping server...${NC}"
"$SCRIPT_DIR/stop-server.sh" 2>/dev/null || true

echo -e "\n${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ALL VARIANT TESTS COMPLETE${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "Results: ${YELLOW}$RESULTS_FILE${NC}"
echo ""
echo "To compare logs:"
echo "  ls -la $PROJECT_DIR/localcode/logs/localcode_gguf_minimax-m2.1-q8-v*.log"
echo ""
