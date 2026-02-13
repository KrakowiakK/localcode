#!/bin/bash
#
# Shared LOCALCODE_* runtime flag defaults for benchmark scripts.
#

LOCALCODE_FLAG_DEFAULT_SPECS=(
    "LOCALCODE_TURN_SUMMARY=1"
    "LOCALCODE_STREAM_OUTPUT=1"
    "LOCALCODE_TASK_OUTPUT_MODE=runtime"
    "LOCALCODE_BENCHMARK_OUTPUT_MODE=runtime"
    "LOCALCODE_TASK_SKIP_READONLY="
    "LOCALCODE_WRITE_VERBOSE_STATE=1"
    "LOCALCODE_EDIT_VERBOSE_STATE="
    "LOCALCODE_EDIT_SNIPPET_SUCCESS="
    "LOCALCODE_WRITE_SNIPPET_SUCCESS="
    "LOCALCODE_ENFORCE_READ_BEFORE_WRITE=1"
    "LOCALCODE_INJECT_TESTS_ON_WRITE="
    "LOCALCODE_WRITE_SPEC_FOCUS=0"
    "LOCALCODE_WRITE_SPEC_CONTRACT="
    "LOCALCODE_WRITE_FULL_DROP=state_json"
    "LOCALCODE_PATH_AUTOCORRECT_GLOBAL="
    "LOCALCODE_READ_LINE_NUMBERS=0"
    "LOCALCODE_READ_STYLE="
    "LOCALCODE_EDIT_HASH_ANCHOR=0"
    "LOCALCODE_SNIPPET_STYLE=numbered"
)

# Applies defaults only for unset vars and exports all LOCALCODE flags.
localcode_apply_flag_defaults() {
    LOCALCODE_FLAG_KEYS=()
    local spec key default_value
    for spec in "${LOCALCODE_FLAG_DEFAULT_SPECS[@]}"; do
        key="${spec%%=*}"
        default_value="${spec#*=}"
        if [ -z "${!key+x}" ]; then
            printf -v "$key" "%s" "$default_value"
        fi
        export "$key"
        LOCALCODE_FLAG_KEYS+=("$key")
    done
    LOCALCODE_FLAG_KEYS_CSV=$(IFS=,; echo "${LOCALCODE_FLAG_KEYS[*]}")
    export LOCALCODE_FLAG_KEYS_CSV
}
