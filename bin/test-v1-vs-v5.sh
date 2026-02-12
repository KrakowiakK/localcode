#!/bin/bash
# Compare V1 (stealth) vs V5 (rich ALL) on full 10-exercise benchmark
HANDLER="/Users/pimpc181/Desktop/BENCHMARK/localcode/tool_handlers/write_handlers.py"
BENCHMARK_DIR="/Users/pimpc181/Desktop/BENCHMARK"

collect_results() {
    local label="$1"
    local dir="$2"
    local pass=0 fail=0 total_time=0
    for ex in binary complex-numbers grade-school phone-number pig-latin react simple-linked-list space-age tournament triangle; do
        rf="$dir/javascript/exercises/practice/$ex/.aider.results.json"
        if [ -f "$rf" ]; then
            res=$(python3 -c "import json; d=json.load(open('$rf')); ok='PASS' if all(d['tests_outcomes']) else 'FAIL'; t=round(d['duration'],1); print(f'{ok} {t}s')")
            status=$(echo "$res" | cut -d' ' -f1)
            time=$(echo "$res" | cut -d' ' -f2)
            if [ "$status" = "PASS" ]; then pass=$((pass+1)); else fail=$((fail+1)); fi
            echo "  $label | $ex: $res"
        else
            fail=$((fail+1))
            echo "  $label | $ex: NO RESULTS"
        fi
    done
    echo "  $label TOTAL: $pass/10 PASS, $fail FAIL"
}

echo "=========================================="
echo "  FULL BENCHMARK: V1 (stealth baseline)"
echo "=========================================="
sed -i '' 's/^_EDIT_STRATEGY = "V[0-9]*"/_EDIT_STRATEGY = "V1"/' "$HANDLER"
grep "^_EDIT_STRATEGY" "$HANDLER"
cd "$BENCHMARK_DIR" && ./bin/run-benchmark.sh qwen3-coder-next-mxfp4 -k binary,complex-numbers,grade-school,phone-number,pig-latin,react,simple-linked-list,space-age,tournament,triangle 2>&1 | tail -5
V1_DIR=$(ls -dt "$BENCHMARK_DIR/benchmark/tmp.benchmark/"*qwen3-coder-next-mxfp4* 2>/dev/null | head -1)
echo ""
collect_results "V1" "$V1_DIR"
echo ""

echo "=========================================="
echo "  FULL BENCHMARK: V5 (rich ALL)"
echo "=========================================="
sed -i '' 's/^_EDIT_STRATEGY = "V[0-9]*"/_EDIT_STRATEGY = "V5"/' "$HANDLER"
grep "^_EDIT_STRATEGY" "$HANDLER"
cd "$BENCHMARK_DIR" && ./bin/run-benchmark.sh qwen3-coder-next-mxfp4 -k binary,complex-numbers,grade-school,phone-number,pig-latin,react,simple-linked-list,space-age,tournament,triangle 2>&1 | tail -5
V5_DIR=$(ls -dt "$BENCHMARK_DIR/benchmark/tmp.benchmark/"*qwen3-coder-next-mxfp4* 2>/dev/null | head -1)
echo ""
collect_results "V5" "$V5_DIR"
echo ""

# Restore V1
sed -i '' 's/^_EDIT_STRATEGY = "V[0-9]*"/_EDIT_STRATEGY = "V1"/' "$HANDLER"
echo "Restored V1 as default."
echo ""
echo "=========================================="
echo "  COMPARISON DONE"
echo "=========================================="
