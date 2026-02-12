# Prompt Iterations Log

Date: 2026-02-12
Model: `qwen3-coder-next-bf16` (MLX)
Task: `react`

## Baseline and Historical

| Variant | Prompt | Runs | Pass@1 | Typical tests | Avg calls | Avg total tokens | Typical flow | Notes |
|---|---|---:|---:|---|---:|---:|---|---|
| base | `qwen3-coder.txt` + compact write feedback | 10 | 0/10 | 12/13 | 4 | 19091 | read -> read -> write -> finish | Stable, low noise, misses final callback-edge case |
| v1 | `qwen3-coder-v1.txt` | 10 | 0/10 | 11/13 | 5 | 26647 | read -> read -> write -> read -> finish | More verification, still fails edge behavior |
| v2 | `qwen3-coder-v2.txt` | 20 | 20/20 | 13/13 | 5 | 26986 | read -> read -> write -> write -> finish | Highest accuracy; likely task-aware bias |
| v3-generic | `qwen3-coder-v3-generic.txt` | 10 | 0/10 | 4/13 | 7 | 42372 | read -> read -> write -> edit x3 -> finish | Fair but too weak; runtime/semantic issues |
| v3-balanced | `qwen3-coder-v3-balanced.txt` | 10 | 0/10 | 11/13 | 7 | 42540 | read -> read -> write -> read -> edit -> finish | Better than v3-generic, still below baseline |

## Key Findings So Far

1. Compact write feedback (`LOCALCODE_WRITE_VERBOSE_STATE=0`) reduces noise and token usage.
2. Fully generic prompts lose too much task performance on this benchmark.
3. Best performer (`v2`) appears to encode strong domain assumptions.
4. Current exploration goal: find a more generic formulation that retains high pass rate.

## Next Iterations

Pending:
- `v4-generic-stable-effects`: generic two-pass/evidence checklist with stable-state + side-effect delta constraints.
- Compare against `base`, `v3-balanced`, and `v2`.

## Baseline Reset + Neutral A/B (2026-02-12)

Context:
- Default agent `mlx/qwen3-coder-next-bf16` restored to `prompts/qwen3-coder.txt` (stable fair baseline).
- `write` snippet feedback is now opt-in via env (`LOCALCODE_WRITE_SNIPPET_SUCCESS`), default OFF.

| Experiment | Env | Runs | Pass@1 | Typical tests | Avg calls | Avg total tokens | Typical flow |
|---|---|---:|---:|---|---:|---:|---|
| baseline_off | `LOCALCODE_WRITE_SNIPPET_SUCCESS=0` | 6 | 0/6 | 12/13 | 4 | 19091 | read -> read -> write -> finish |
| baseline_on | `LOCALCODE_WRITE_SNIPPET_SUCCESS=1` | 6 | 0/6 | 12/13 | 4 | 20031 | read -> read -> write -> finish |

Takeaways:
1. Rollback to baseline worked: stable `12/13` profile is back.
2. `write` snippet ON does not improve test outcome in this setup.
3. `write` snippet ON increases token usage (~+940 avg total tokens) with no quality gain.
4. Keep snippet OFF by default; use ON only for targeted diagnostics.

## Neutral Experiment: Write Verbosity (2026-02-12)

Experiment:
- Baseline prompt (`qwen3-coder.txt`), no task-specific guidance changes.
- Toggle only one neutral runtime flag:
  - `LOCALCODE_WRITE_VERBOSE_STATE=1`
  - `LOCALCODE_WRITE_SNIPPET_SUCCESS=0`

Observed results:

| Experiment | Runs | Pass@1 | Typical flow | Avg calls | Avg total tokens |
|---|---:|---:|---|---:|---:|
| baseline (verbose=0) | 6 | 0/6 | read -> read -> write -> finish | 4 | 19091 |
| baseline + verbose write feedback | 6 | 6/6 | read -> read -> write -> write -> finish | 5 | 27958 |

Interpretation:
1. Richer post-write state feedback appears to help the model do one corrective pass instead of finishing too early.
2. Improvement came from a neutral tooling signal, not from task-specific prompt hints.
3. Tradeoff: higher token cost and one extra tool call on average.

## Rollout Check: Runner Default + 20 Iterations (2026-02-12)

Change:
- Benchmark runner default set to `LOCALCODE_WRITE_VERBOSE_STATE=1` (can still be overridden by env).

Command:
- `./bin/run-benchmark.sh qwen3-coder-next-bf16 -k react` repeated 20 times.

Results:

| Experiment | Runs | Pass@1 | Avg calls | Avg tool mix | Avg total tokens | Dominant flow |
|---|---:|---:|---:|---|---:|---|
| baseline (runner default verbose=1) | 20 | 20/20 | 5.00 | read=2, write=2, edit=0, patch=0 | 27958 | read -> read -> write -> write -> finish |

Notes:
1. No tool loops observed in this batch.
2. Behavior is highly stable across all 20 runs.
