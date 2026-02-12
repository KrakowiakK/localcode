#!/bin/bash
# Test different edit strategy variants on grade-school and binary
HANDLER="/Users/pimpc181/Desktop/BENCHMARK/localcode/tool_handlers/write_handlers.py"
BENCHMARK_DIR="/Users/pimpc181/Desktop/BENCHMARK"

for VARIANT in V1 V2 V3 V4 V5 V6; do
    echo "=========================================="
    echo "  TESTING VARIANT: $VARIANT"
    echo "=========================================="
    
    # Change strategy in handler
    sed -i '' "s/^_EDIT_STRATEGY = \"V[0-9]*\"/_EDIT_STRATEGY = \"$VARIANT\"/" "$HANDLER"
    
    # Verify change
    grep "^_EDIT_STRATEGY" "$HANDLER"
    
    # Run grade-school and binary
    echo "--- Running grade-school + binary ---"
    cd "$BENCHMARK_DIR" && ./bin/run-benchmark.sh qwen3-coder-next-mxfp4 -k grade-school,binary 2>&1 | tail -20
    
    # Find latest results dir
    LATEST=$(ls -dt "$BENCHMARK_DIR/benchmark/tmp.benchmark/"*qwen3-coder-next-mxfp4* 2>/dev/null | head -1)
    
    # Read results
    for EX in binary grade-school; do
        RESULT_FILE="$LATEST/javascript/exercises/practice/$EX/.aider.results.json"
        if [ -f "$RESULT_FILE" ]; then
            RESULT=$(python3 -c "import json; d=json.load(open('$RESULT_FILE')); print('PASS' if all(d['tests_outcomes']) else 'FAIL', round(d['duration'],1))")
            echo "$VARIANT | $EX: $RESULT"
        else
            echo "$VARIANT | $EX: NO RESULTS"
        fi
    done
    echo ""
done

# Restore V1 as default
sed -i '' "s/^_EDIT_STRATEGY = \"V[0-9]*\"/_EDIT_STRATEGY = \"V1\"/" "$HANDLER"
echo "Done. Restored V1 as default."
