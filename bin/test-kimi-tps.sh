#!/usr/bin/env bash
set -euo pipefail

LLAMA_SERVER="/Users/pimpc181/Desktop/BENCHMARK/bin/llama-server"
MODEL="/Users/pimpc181/.lmstudio/models/unsloth/Kimi-K2.5-GGUF/Kimi-K2.5-UD-Q2_K_XL-00001-of-00008.gguf"
PORT=1235
LOG="/tmp/benchmark-llama-server.log"
RESULTS="/tmp/kimi-tps-results.txt"

echo "Kimi K2.5 TPS Optimization" > "$RESULTS"
echo "==========================" >> "$RESULTS"
printf "%-45s | %-10s | %-10s | %-10s | %-10s\n" "Variant" "Prefill1" "Decode1" "Prefill2" "Decode2" >> "$RESULTS"
echo "----------------------------------------------|------------|------------|------------|------------" >> "$RESULTS"

run_test() {
    local test_name="$1"
    shift
    local args=("$@")

    echo ""
    echo "=== TEST: $test_name ==="

    "$LLAMA_SERVER" --model "$MODEL" --host 127.0.0.1 --port $PORT "${args[@]}" > "$LOG" 2>&1 &
    local PID=$!

    for i in $(seq 1 120); do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "CRASHED"; printf "%-45s | CRASHED\n" "$test_name" >> "$RESULTS"; return 1
        fi
        curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 && break
        sleep 5
    done

    # Warm-up request (discard)
    curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"t","messages":[{"role":"user","content":"Hi"}],"max_tokens":10}' > /dev/null

    # Test 1: Medium coding prompt
    local r1=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"t","messages":[{"role":"system","content":"You are a coding assistant."},{"role":"user","content":"Write a Python function that implements binary search on a sorted array. Return the index or -1 if not found."}],"max_tokens":400,"temperature":0.7}')
    local pp1=$(echo "$r1" | python3 -c "import json,sys;t=json.load(sys.stdin).get('timings',{});print(f\"{t.get('prompt_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local gen1=$(echo "$r1" | python3 -c "import json,sys;t=json.load(sys.stdin).get('timings',{});print(f\"{t.get('predicted_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")

    # Test 2: Longer prompt with context
    local r2=$(curl -s "http://127.0.0.1:$PORT/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"t","messages":[{"role":"system","content":"You are a coding assistant. Think step by step."},{"role":"user","content":"I have the following Python class:\n\nclass Node:\n    def __init__(self, val):\n        self.val = val\n        self.left = None\n        self.right = None\n\nclass BST:\n    def __init__(self):\n        self.root = None\n\n    def insert(self, val):\n        if not self.root:\n            self.root = Node(val)\n        else:\n            self._insert(self.root, val)\n\n    def _insert(self, node, val):\n        if val < node.val:\n            if node.left is None:\n                node.left = Node(val)\n            else:\n                self._insert(node.left, val)\n        else:\n            if node.right is None:\n                node.right = Node(val)\n            else:\n                self._insert(node.right, val)\n\nNow add a delete method that handles all three cases: leaf node, node with one child, and node with two children (use in-order successor)."}],"max_tokens":500,"temperature":0.7}')
    local pp2=$(echo "$r2" | python3 -c "import json,sys;t=json.load(sys.stdin).get('timings',{});print(f\"{t.get('prompt_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")
    local gen2=$(echo "$r2" | python3 -c "import json,sys;t=json.load(sys.stdin).get('timings',{});print(f\"{t.get('predicted_per_second',0):.1f}\")" 2>/dev/null || echo "ERR")

    echo "  Prefill: ${pp1} / ${pp2}  Decode: ${gen1} / ${gen2}"
    printf "%-45s | %-10s | %-10s | %-10s | %-10s\n" "$test_name" "$pp1" "$gen1" "$pp2" "$gen2" >> "$RESULTS"

    kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; sleep 3
}

# Common base flags
BASE=(--ctx-size 32768 --n-gpu-layers 99 --jinja --reasoning-format none --parallel 1)

echo "Running TPS optimization tests..."

# 1. Current config
run_test "v1: current (b2048 ub512)" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 2048 -ub 512

# 2. Bigger ubatch
run_test "v2: b4096 ub2048" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 4096 -ub 2048

# 3. Max batch
run_test "v3: b8192 ub2048" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 8192 -ub 2048

# 4. Without --special
run_test "v4: no --special" \
    "${BASE[@]}" --no-context-shift --fit on -fa on -b 2048 -ub 512

# 5. Without --no-context-shift
run_test "v5: no --no-context-shift" \
    "${BASE[@]}" --special --fit on -fa on -b 2048 -ub 512

# 6. Without --fit (let system decide)
run_test "v6: no --fit" \
    "${BASE[@]}" --no-context-shift --special -fa on -b 2048 -ub 512

# 7. With --mlock (safe: f16 KV + 32K ctx = ~378GB total, fits 512GB)
run_test "v7: +mlock" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 2048 -ub 512 --mlock

# 8. With --mlock + bigger batch
run_test "v8: +mlock b8192 ub2048" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 8192 -ub 2048 --mlock

# 9. mlock + no-mmap (all in RAM, like original but cleaner flags)
run_test "v9: +mlock +no-mmap b8192 ub2048" \
    "${BASE[@]}" --no-context-shift --special --fit on -fa on -b 8192 -ub 2048 --mlock --no-mmap

# 10. Best candidate combo
run_test "v10: mlock b8192 ub2048 no-special" \
    "${BASE[@]}" --no-context-shift --fit on -fa on -b 8192 -ub 2048 --mlock

echo ""
echo "=== ALL TESTS COMPLETE ==="
echo ""
cat "$RESULTS"
