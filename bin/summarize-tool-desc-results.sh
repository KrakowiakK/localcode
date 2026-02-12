#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSONL_PATH="${1:-$ROOT_DIR/optimizations/tool_desc_profiles/results/experiments_v3.jsonl}"
EXP_TAG="${2:-}"

if [[ ! -f "$JSONL_PATH" ]]; then
  echo "error: results jsonl not found: $JSONL_PATH" >&2
  exit 1
fi

jq -s -r --arg tag "$EXP_TAG" '
  def rows:
    if ($tag | length) > 0
    then map(select(.exp_tag == $tag))
    else .
    end;

  def score:
    (if .pass then 10000 else 0 end)
    + ((.tasks_pass / (if .task_count == 0 then 1 else .task_count end)) * 1000)
    - (.calls * 5)
    - (.errors * 200)
    - (.noop_hits * 20)
    - (.spec_reads * 2)
    - (.duration_sec / 10);

  rows as $rows
  | if ($rows | length) == 0 then
      "No rows for selected tag."
    else
      ($rows
      | group_by(.profile)
      | map({
          profile: .[0].profile,
          n: length,
          pass_rate: (map(if .pass then 1 else 0 end) | add / length * 100),
          avg_tasks_pass_ratio: (map((.tasks_pass / (if .task_count == 0 then 1 else .task_count end))) | add / length * 100),
          avg_calls: (map(.calls) | add / length),
          avg_reads: (map(.reads) | add / length),
          avg_writes: (map(.writes) | add / length),
          avg_edits: (map(.edits) | add / length),
          avg_errors: (map(.errors) | add / length),
          avg_noop: (map(.noop_hits) | add / length),
          avg_spec_reads: (map(.spec_reads) | add / length),
          avg_duration_sec: (map(.duration_sec) | add / length),
          avg_score: (map(score) | add / length)
        })
      | sort_by(-.avg_score)) as $agg
      | (["profile","n","pass_rate","avg_task_pass_%","avg_calls","avg_reads","avg_writes","avg_edits","avg_errors","avg_noop","avg_spec_reads","avg_dur_s","avg_score"] | @tsv),
        ($agg[] | [
          .profile,
          .n,
          (.pass_rate|tostring),
          (.avg_tasks_pass_ratio|tostring),
          (.avg_calls|tostring),
          (.avg_reads|tostring),
          (.avg_writes|tostring),
          (.avg_edits|tostring),
          (.avg_errors|tostring),
          (.avg_noop|tostring),
          (.avg_spec_reads|tostring),
          (.avg_duration_sec|tostring),
          (.avg_score|tostring)
        ] | @tsv)
    end
' "$JSONL_PATH"
