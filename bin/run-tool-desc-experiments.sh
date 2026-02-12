#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-}"
REPEATS="${2:-3}"
AGENT="${3:-qwen3-coder-next-bf16}"
TASK_SPEC="${4:-report}"
REPORT_TASKS_FILE="$ROOT_DIR/optimizations/tool_desc_profiles/report_tasks_js.txt"

if [[ -z "$PROFILE" ]]; then
  echo "usage: $0 <baseline|minimal|decision|decision2> [repeats] [agent] [task_spec]" >&2
  echo "task_spec: report | react | 'task1,task2' | '-k react,triangle' | --all | --full" >&2
  exit 1
fi

cd "$ROOT_DIR"
./bin/set-tool-desc-profile.sh "$PROFILE"

resolve_task_filter() {
  local spec="$1"
  if [[ "$spec" == "--all" || "$spec" == "--full" ]]; then
    echo "$spec"
    return
  fi
  if [[ "$spec" == -k* ]]; then
    echo "$spec"
    return
  fi
  if [[ "$spec" == "report" || "$spec" == "report_core" || "$spec" == "report_core_js" ]]; then
    if [[ ! -f "$REPORT_TASKS_FILE" ]]; then
      echo "error: report tasks file missing: $REPORT_TASKS_FILE" >&2
      exit 1
    fi
    local tasks_csv
    tasks_csv="$(paste -sd, "$REPORT_TASKS_FILE")"
    echo "-k $tasks_csv"
    return
  fi
  if [[ "$spec" == *","* ]]; then
    echo "-k $spec"
    return
  fi
  echo "-k $spec"
}

TASK_FILTER="$(resolve_task_filter "$TASK_SPEC")"

OUT_DIR="$ROOT_DIR/optimizations/tool_desc_profiles/results"
mkdir -p "$OUT_DIR"
CSV="$OUT_DIR/experiments_v3.csv"
JSONL="$OUT_DIR/experiments_v3.jsonl"
EXP_TAG="${EXP_TAG:-manual}"

if [[ ! -f "$CSV" ]]; then
  cat > "$CSV" <<'EOF'
ts,exp_tag,profile,trial,task_spec,task_filter,task_count,tasks_pass,tasks_fail,pass,calls,reads,writes,edits,patches,finishes,errors,noop_hits,spec_reads,turns,duration_sec,run_dir,task_list,jsonl,pretty_log
EOF
fi

for ((i=1; i<=REPEATS; i++)); do
  echo "=== profile=$PROFILE trial=$i/$REPEATS ==="
  start_marker="$OUT_DIR/.exp_start.$$.$i"
  end_marker="$OUT_DIR/.exp_end.$$.$i"
  : > "$start_marker"
  ./bin/run-benchmark.sh "$AGENT" $TASK_FILTER >/tmp/tool_desc_run.out 2>&1 || true
  : > "$end_marker"

  run_dir="$(ls -1t benchmark/tmp.benchmark | head -n 1)"
  run_root="benchmark/tmp.benchmark/$run_dir"
  if [[ ! -d "$run_root" ]]; then
    echo "warning: missing run root for trial $i: $run_root" >&2
    rm -f "$start_marker" "$end_marker"
    continue
  fi

  result_files=()
  while IFS= read -r line; do
    result_files+=("$line")
  done < <(find "$run_root" -type f -name '.aider.results.json' | sort)
  task_count="${#result_files[@]}"
  if [[ "$task_count" -eq 0 ]]; then
    echo "warning: no .aider.results.json files for trial $i in $run_root" >&2
    rm -f "$start_marker" "$end_marker"
    continue
  fi

  tasks_pass=0
  tasks_fail=0
  duration_sec=0
  task_names=()
  for rf in "${result_files[@]}"; do
    outcome="$(jq -r '.tests_outcomes[0]' "$rf" 2>/dev/null || echo "false")"
    dur="$(jq -r '.duration // 0' "$rf" 2>/dev/null || echo "0")"
    duration_sec="$(awk "BEGIN {print $duration_sec + $dur}")"
    task_name="$(basename "$(dirname "$rf")")"
    task_names+=("$task_name")
    if [[ "$outcome" == "true" ]]; then
      tasks_pass=$((tasks_pass + 1))
    else
      tasks_fail=$((tasks_fail + 1))
    fi
  done
  if [[ "$tasks_fail" -eq 0 ]]; then
    pass="true"
  else
    pass="false"
  fi
  task_list="$(printf "%s;" "${task_names[@]}")"
  task_list="${task_list%;}"

  jsonl_files=()
  while IFS= read -r line; do
    jsonl_files+=("$line")
  done < <(
    find localcode/logs -maxdepth 1 -type f -name "*${AGENT}_*.jsonl" \
      -newer "$start_marker" ! -newer "$end_marker" | sort
  )
  if [[ "${#jsonl_files[@]}" -eq 0 ]]; then
    latest_jsonl="$(ls -1t localcode/logs/*"${AGENT}"*.jsonl 2>/dev/null | head -n 1 || true)"
    if [[ -n "$latest_jsonl" ]]; then
      jsonl_files=("$latest_jsonl")
    fi
  fi
  if [[ "${#jsonl_files[@]}" -eq 0 ]]; then
    echo "warning: no jsonl logs found for trial $i" >&2
    rm -f "$start_marker" "$end_marker"
    continue
  fi

  calls="$(jq -s '[ .[] | select(.event=="run_end") | .tool_calls_total ] | add // 0' "${jsonl_files[@]}")"
  turns="$(jq -s '[ .[] | select(.event=="agent_done") | (.turns // 0) ] | add // 0' "${jsonl_files[@]}")"
  errors="$(jq -s '[ .[] | select(.event=="run_end") | .tool_errors_total ] | add // 0' "${jsonl_files[@]}")"
  reads="$(jq -s '[ .[] | select(.event=="run_end") | (.tool_call_counts.read // 0) ] | add // 0' "${jsonl_files[@]}")"
  writes="$(jq -s '[ .[] | select(.event=="run_end") | (.tool_call_counts.write // 0) ] | add // 0' "${jsonl_files[@]}")"
  edits="$(jq -s '[ .[] | select(.event=="run_end") | (.tool_call_counts.edit // 0) ] | add // 0' "${jsonl_files[@]}")"
  patches="$(jq -s '[ .[] | select(.event=="run_end") | (.tool_call_counts.apply_patch // 0) ] | add // 0' "${jsonl_files[@]}")"
  finishes="$(jq -s '[ .[] | select(.event=="run_end") | (.tool_call_counts.finish // 0) ] | add // 0' "${jsonl_files[@]}")"

  noop_hits="0"
  spec_reads="0"
  pretty_logs=()
  for jf in "${jsonl_files[@]}"; do
    maybe_log="${jf%.jsonl}.log"
    if [[ -f "$maybe_log" ]]; then
      pretty_logs+=("$maybe_log")
    fi
  done
  if [[ "${#pretty_logs[@]}" -gt 0 ]]; then
    rg_noop_counts="$(rg -c 'no changes - file already has this content|repeated no-op|patch produced no changes' "${pretty_logs[@]}" 2>/dev/null || true)"
    if [[ -n "$rg_noop_counts" ]]; then
      noop_hits="$(printf "%s\n" "$rg_noop_counts" | awk -F: '{s+=$NF} END{print s+0}')"
    else
      noop_hits="0"
    fi
  fi
  spec_reads="$(jq -s -r '
    [ .[]
      | select(.event=="tool_before" and .tool_name=="read")
      | (.tool_args | (fromjson? // {}))
      | (.path // "")
      | select(test("\\.(spec|test)\\.[A-Za-z0-9]+$"))
    ] | length
  ' "${jsonl_files[@]}")"

  ts="$(date '+%Y-%m-%dT%H:%M:%S')"
  jsonl_ref="$(printf "%s;" "${jsonl_files[@]}")"
  jsonl_ref="${jsonl_ref%;}"
  pretty_ref="$(printf "%s;" "${pretty_logs[@]:-}")"
  pretty_ref="${pretty_ref%;}"
  echo "$ts,$EXP_TAG,$PROFILE,$i,$TASK_SPEC,\"$TASK_FILTER\",$task_count,$tasks_pass,$tasks_fail,$pass,$calls,$reads,$writes,$edits,$patches,$finishes,$errors,$noop_hits,$spec_reads,$turns,$duration_sec,$run_dir,\"$task_list\",\"$jsonl_ref\",\"$pretty_ref\"" >> "$CSV"
  jq -nc \
    --arg ts "$ts" \
    --arg exp_tag "$EXP_TAG" \
    --arg profile "$PROFILE" \
    --arg trial "$i" \
    --arg task_spec "$TASK_SPEC" \
    --arg task_filter "$TASK_FILTER" \
    --arg task_count "$task_count" \
    --arg tasks_pass "$tasks_pass" \
    --arg tasks_fail "$tasks_fail" \
    --arg pass "$pass" \
    --arg calls "$calls" \
    --arg reads "$reads" \
    --arg writes "$writes" \
    --arg edits "$edits" \
    --arg patches "$patches" \
    --arg finishes "$finishes" \
    --arg errors "$errors" \
    --arg noop_hits "$noop_hits" \
    --arg spec_reads "$spec_reads" \
    --arg turns "$turns" \
    --arg duration_sec "$duration_sec" \
    --arg run_dir "$run_dir" \
    --arg task_list "$task_list" \
    --arg jsonl "$jsonl_ref" \
    --arg pretty_log "$pretty_ref" \
    '{
      ts: $ts,
      exp_tag: $exp_tag,
      profile: $profile,
      trial: ($trial|tonumber),
      task_spec: $task_spec,
      task_filter: $task_filter,
      task_count: ($task_count|tonumber),
      tasks_pass: ($tasks_pass|tonumber),
      tasks_fail: ($tasks_fail|tonumber),
      pass: ($pass == "true"),
      calls: ($calls|tonumber),
      reads: ($reads|tonumber),
      writes: ($writes|tonumber),
      edits: ($edits|tonumber),
      patches: ($patches|tonumber),
      finishes: ($finishes|tonumber),
      errors: ($errors|tonumber),
      noop_hits: ($noop_hits|tonumber),
      spec_reads: ($spec_reads|tonumber),
      turns: ($turns|tonumber),
      duration_sec: ($duration_sec|tonumber),
      run_dir: $run_dir,
      task_list: ($task_list | if length == 0 then [] else split(";") end),
      jsonl: ($jsonl | if length == 0 then [] else split(";") end),
      pretty_log: ($pretty_log | if length == 0 then [] else split(";") end)
    }' >> "$JSONL"

  echo "trial=$i pass=$pass tasks=$task_count pass_tasks=$tasks_pass fail_tasks=$tasks_fail calls=$calls reads=$reads writes=$writes edits=$edits errors=$errors noop=$noop_hits spec_reads=$spec_reads duration=$duration_sec"
  rm -f "$start_marker" "$end_marker"
done

echo "results appended to $CSV and $JSONL"
