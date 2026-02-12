#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_SERVER="$SCRIPT_DIR/llama-server"
MODEL="/Users/pimpc181/.lmstudio/models/unsloth/Kimi-K2.5-GGUF/Kimi-K2.5-UD-Q2_K_XL-00001-of-00008.gguf"
PORT=1235
LOG="/tmp/benchmark-llama-server.log"
RESULTS="/tmp/kimi-server-test-results.txt"

echo "Kimi K2.5 Server Optimization Tests" > "$RESULTS"
echo "====================================" >> "$RESULTS"
echo "Date: $(date)" >> "$RESULTS"
echo "" >> "$RESULTS"

run_test() {
    local test_name="$1"
    shift
    local extra_args=("$@")

    echo ""
    echo "============================================"
    echo "TEST: $test_name"
    echo "Args: ${extra_args[*]}"
    echo "============================================"

    # Start server
    "$LLAMA_SERVER" \
        --model "$MODEL" \
        --host 127.0.0.1 --port $PORT \
        --ctx-size 32768 --n-gpu-layers 99 \
        "${extra_args[@]}" \
        > "$LOG" 2>&1 &
    local PID=$!

    # Wait for health
    local waited=0
    while [ $waited -lt 600 ]; do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "FAIL: Server crashed"
            echo "$test_name | CRASHED" >> "$RESULTS"
            return 1
        fi
        if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
            echo "Server ready (${waited}s)"
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done

    if [ $waited -ge 600 ]; then
        echo "FAIL: Timeout"
        kill "$PID" 2>/dev/null || true
        echo "$test_name | TIMEOUT" >> "$RESULTS"
        return 1
    fi

    # Run 3 test prompts: short, medium, long context
    # Test 1: Short prompt (warm-up + baseline)
    echo "  Test 1: Short prompt..."
    local r1=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"test","messages":[{"role":"user","content":"Write a Python function to check if a number is prime."}],"max_tokens":300,"temperature":0.7}')

    local pp1=$(echo "$r1" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('prompt_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local gen1=$(echo "$r1" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('predicted_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local has_rc1=$(echo "$r1" | python3 -c "import json,sys; d=json.load(sys.stdin); m=d['choices'][0]['message']; print('YES' if m.get('reasoning_content') else 'NO')" 2>/dev/null || echo "ERR")

    # Test 2: Medium prompt with system + longer user
    echo "  Test 2: Medium prompt..."
    local r2=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"test","messages":[{"role":"system","content":"You are a coding assistant. Write clean, efficient code. Think step by step before writing code."},{"role":"user","content":"Implement a binary search tree in Python with insert, delete, search, and in-order traversal methods. Include proper handling of edge cases like deleting a node with two children."}],"max_tokens":500,"temperature":0.7}')

    local pp2=$(echo "$r2" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('prompt_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local gen2=$(echo "$r2" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('predicted_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local has_rc2=$(echo "$r2" | python3 -c "import json,sys; d=json.load(sys.stdin); m=d['choices'][0]['message']; print('YES' if m.get('reasoning_content') else 'NO')" 2>/dev/null || echo "ERR")

    # Test 3: Same prompt again (cached - should be faster prefill)
    echo "  Test 3: Cached prompt..."
    local r3=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"test","messages":[{"role":"system","content":"You are a coding assistant. Write clean, efficient code. Think step by step before writing code."},{"role":"user","content":"Implement a binary search tree in Python with insert, delete, search, and in-order traversal methods. Include proper handling of edge cases like deleting a node with two children."}],"max_tokens":500,"temperature":0.7}')

    local pp3=$(echo "$r3" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('prompt_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local gen3=$(echo "$r3" | python3 -c "import json,sys; d=json.load(sys.stdin); t=d.get('timings',{}); print(f\"{t.get('predicted_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")

    echo "  Results:"
    echo "    Short:  prefill=${pp1} tok/s  decode=${gen1} tok/s  thinking=${has_rc1}"
    echo "    Medium: prefill=${pp2} tok/s  decode=${gen2} tok/s  thinking=${has_rc2}"
    echo "    Cached: prefill=${pp3} tok/s  decode=${gen3} tok/s"

    printf "%-40s | pp1=%-8s gen1=%-8s | pp2=%-8s gen2=%-8s | pp3=%-8s gen3=%-8s | think=%s\n" \
        "$test_name" "$pp1" "$gen1" "$pp2" "$gen2" "$pp3" "$gen3" "$has_rc1" >> "$RESULTS"

    # Stop server
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    sleep 3
}

echo "Starting optimization tests..."
echo ""

# Test 1: Baseline (current config)
run_test "v1-baseline-q4-deepseek" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 2048 -ub 512 \
    --cache-type-k q4_0 --cache-type-v q4_0 \
    --reasoning-format deepseek --kv-unified --parallel 1

# Test 2: KV cache q8_0
run_test "v2-kv-q8" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 2048 -ub 512 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format deepseek --kv-unified --parallel 1

# Test 3: KV cache f16 (default)
run_test "v3-kv-f16" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 2048 -ub 512 \
    --reasoning-format deepseek --kv-unified --parallel 1

# Test 4: Without --kv-unified
run_test "v4-no-kv-unified-q8" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 2048 -ub 512 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format deepseek --parallel 1

# Test 5: Larger batch sizes
run_test "v5-big-batch-q8" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 4096 -ub 1024 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format deepseek --kv-unified --parallel 1

# Test 6: Default batch (8192/1024 from start-server.sh defaults)
run_test "v6-default-batch-q8" \
    --jinja --no-context-shift --special --fit on -fa on \
    --batch-size 8192 --ubatch-size 1024 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format deepseek --kv-unified --parallel 1

# Test 7: reasoning-format none (check if thinking tags appear + speed)
run_test "v7-reason-none-q8" \
    --jinja --no-context-shift --special --fit on -fa on \
    -b 2048 -ub 512 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format none --kv-unified --parallel 1

# Test 8: reasoning-format none + no kv-unified + big batch
run_test "v8-optimal-candidate" \
    --jinja --no-context-shift --special --fit on -fa on \
    --batch-size 8192 --ubatch-size 1024 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-format none --parallel 1

echo ""
echo "============================================"
echo "ALL TESTS COMPLETE"
echo "============================================"
echo ""
echo "Results saved to: $RESULTS"
cat "$RESULTS"
