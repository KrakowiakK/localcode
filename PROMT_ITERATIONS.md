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

## Tool Feedback Matrix (2026-02-12, 24 runs, 3-task mix)

Tasks:
- `react`
- `promises`
- `rational-numbers`

Command pattern:
- `./bin/run-benchmark.sh qwen3-coder-next-bf16 -k react,promises,rational-numbers`

Variants and outcomes (4 runs each):

| Variant | Env delta | Avg pass (/3) | React | Promises | Rational |
|---|---|---:|---:|---:|---:|
| `base` | `verbose=1,spec_focus=1,snippet=0,enforce=1` | 1.00 | 4/4 | 0/4 | 0/4 |
| `compact` | `verbose=0` | 1.00 | 4/4 | 0/4 | 0/4 |
| `contract` | `spec_contract=1` | 1.00 | 4/4 | 0/4 | 0/4 |
| `noenforce` | `enforce_read_before_write=0` | 1.00 | 4/4 | 0/4 | 0/4 |
| `snippet` | `write_snippet_success=1` | 0.00 | 0/4 | 0/4 | 0/4 |
| `nospec` | `spec_focus=0` | 0.00 | 0/4 | 0/4 | 0/4 |

Takeaways:
1. `spec_focus` is required for stable `react`.
2. `write` snippet on success is strongly harmful in this setup.
3. `spec_contract` and `enforce_read_before_write` are quality-neutral on this 3-task mix.
4. No variant improved `promises` or `rational-numbers`.

## Extra Iterations After Matrix (2026-02-12)

Experiments:
1. Aggressive `spec_focus` (edge-tag enriched) on all tasks.
2. Conditional `spec_focus` (legacy for small specs, edge-aware for large specs).
3. `review_hint` after first write on larger specs.

Results:
1. Aggressive `spec_focus` regressed to `0/3` in early runs and was rolled back.
2. Conditional `spec_focus` restored baseline stability (`1/3`, `react` stable), but no net gain on `promises`/`rational`.
3. `review_hint` also regressed (`0/3` in early runs) and was rolled back.

Current stable baseline remains:
- `LOCALCODE_WRITE_VERBOSE_STATE=1`
- `LOCALCODE_WRITE_SPEC_FOCUS=1`
- `LOCALCODE_WRITE_SNIPPET_SUCCESS=0`
- `LOCALCODE_ENFORCE_READ_BEFORE_WRITE=1`

## Edit-Only Iterations (2026-02-12)

Goal:
- Evaluate non-`write` editing behavior empirically (`edit` only) and reduce tool-call noise without task-specific hints.

Changes tested:
1. New agent profile: `mlx/qwen3-coder-next-bf16-edit-only` (tools: `ls, read, edit, glob, grep, search, finish`).
2. Dedicated prompt: `prompts/qwen3-coder-edit-only.txt`.
3. Removed `write` suggestions from `edit` tool description/feedback and `edit()` runtime messages.
4. Tried (then rolled back) line-numbered full-file snapshot on `edit` not-found path.

Runs:

| Run | Config state | Tasks | Tries | Outcome |
|---|---|---|---:|---|
| A | edit-only prompt + no `write` hints | `react,promises,rational-numbers` | 1 | `0/3` (`FFF`) |
| B | same as A | `react,promises,rational-numbers` | 3 | `3/3` at `Pass@2` (`FP,FP,FP`) |
| C | A + line-numbered snapshot on edit-not-found | `react,promises,rational-numbers` | 3 | `2/3` (`FFF,FP,FP`) + occasional unknown `run` |

Task-level pattern:
- `react`: generally stable recovery in try2 (`FAIL -> PASS`) with `read/read/edit/edit/finish` then short corrective pass.
- `rational-numbers`: similar `FAIL -> PASS` via one corrective `edit`.
- `promises`: highest variance; sometimes converges in try2, sometimes drifts into long continuation with premature `finish` and occasional unknown tool call.

Conclusions:
1. The high-value change was removing `write` references in edit-only mode (prevents invalid tool attraction).
2. Line-numbered full snapshot for not-found did **not** improve stability and increased drift/noise in `promises` (rolled back).
3. Best known edit-only behavior from this batch is config state **A/B** (without snapshot extension).
4. `edit`-only is workable for diagnostics, but not yet as stable as mixed-mode baseline on harder tasks.

## Night Iterations — Edit/Patch/Mixed (2026-02-12)

Data files:
- `optimizations/night_edit_matrix_2026-02-12.tsv`
- `optimizations/night_patch_matrix_2026-02-12.tsv`
- `optimizations/night_mixed_matrix_2026-02-12.tsv`

### 1) Edit-only re-check (3 tasks, tries=2)

Variants:
- `E0_base_r2`
- `E1_edit_verbose_r2`
- `E2_verbose_no_snippet_r2`

Observed outcome (all three variants identical):
- `pass_any=2/3`, `pass1=1/3`
- Outcomes: `promises:FF`, `rational-numbers:FP`, `react:P`

Interpretation:
1. For this model/profile, edit verbosity/snippet toggles did not change quality on this 3-task set.
2. Main bottleneck remains `promises` (no try2 recovery in this slice), not tool loop stability.

### 2) Patch-focused experiments

Initial patch-only runs showed repeated format drift:
- invalid patch envelope,
- unified diff header usage (`---/+++`),
- unknown `write` fallback calls.

Applied generic patch improvements:
1. `apply_patch` tool description now contains strict required structure and explicit "no unified diff headers" guidance.
2. Patch handler feedback no longer suggests `edit/write` directly in repeated/no-op patch messages (uses neutral "another available mutation tool").
3. Added `patch-only` prompt with explicit patch example.
4. Reduced patch-only profile limits (`max_turns=8`, `max_tokens=4000`) for faster loop diagnosis.

After fix (react, tries=1):
- `P0_patch_prompt_react_afterfix`: `react:F`
- `P2_patch_baseprompt_react_afterfix`: `react:F`

Important behavior change:
- Model moved from envelope-level failures ("missing Begin Patch") to valid envelope usage with real `apply_patch` success events.
- Patch quality still insufficient to pass task, but format compliance improved.

### 3) Mixed-profile validation (global)

Quick checks on `qwen3-coder-next-bf16`:
- `M0_base` (`tries=1`): `0/3`
- `M1_edit_verbose` (`tries=1`): `0/3`
- `M0_base_r2` (`tries=2`): `pass_any=2/3`, outcomes `promises:FP`, `rational-numbers:FP`, `react:FF`

Interpretation:
1. No evidence that the night `edit` toggles improve mixed profile quality.
2. Mixed profile still recovers in try2 on `promises` and `rational-numbers`.
3. `react` remained unstable in this particular run slice.

### Baseline decisions after night run

Keep:
1. Edit feedback cleanup that removed `write` nudges in edit-only flows.
2. Improved `apply_patch` description with strict format example.
3. Neutralized patch handler fallback wording (no forced `edit/write` suggestion).

Do not adopt as baseline:
1. Patch-only profile as primary benchmark mode (insufficient task pass quality).
2. Edit verbosity/snippet toggles as default quality levers (no measured gain in this slice).

## Night Continuation — Baseline Promotion (2026-02-12)

Data file:
- `optimizations/night_shift_2026-02-12.tsv`

### 1) React profile sweep (single-task sanity)

Key outcomes:
1. `qwen3-coder-next-bf16-v2`: `Pass@1=1/1` (`13/13` on `react`).
2. `qwen3-coder-next-bf16-v6-generic-two-pass`: `Pass@1=1/1` (`13/13` on `react`).
3. `qwen3-coder-next-bf16-edit-only`: `Pass@1=1/1` (`13/13` on `react`).
4. Current default `qwen3-coder-next-bf16` (old prompt) repeatedly stayed at `10/13` in this slice.

Interpretation:
1. Prompt quality dominates this model's behavior more than tool schema changes.
2. `v6` and `edit-only` both reach top quality on `react`, but `v6` keeps mixed-tool flexibility.

### 2) Triad comparison (react,promises,rational-numbers, tries=2)

Results:
1. `v2`: `Pass@any=2/3`, avg `94.0s/task`.
2. `v6`: `Pass@any=3/3`, avg `65.8s/task`.
3. `edit-only`: `Pass@any=3/3`, avg `80.4s/task`.

Interpretation:
1. `v6` matched top recovery while using fewer calls and lower runtime than `edit-only`.
2. `v2` underperformed on recovery and produced heavier write churn.
3. `v9` (`read-before-mutate` hard precondition) regressed to `Pass@any=2/3` and increased runtime, so it was rejected.

### 3) v6 tuning matrix (3 tasks, tries=2)

Results:
1. `base`: `Pass@any=3/3` (reference).
2. `LOCALCODE_EDIT_VERBOSE_STATE=1`: `Pass@any=2/3` (regression).
3. `LOCALCODE_WRITE_SNIPPET_SUCCESS=1`: `Pass@any=2/3` (regression, slower).
4. `LOCALCODE_EDIT_VERBOSE_STATE=1 + LOCALCODE_WRITE_SNIPPET_SUCCESS=1`: `Pass@any=2/3` (regression, slower).
5. `LOCALCODE_WRITE_SPEC_CONTRACT=1`: `Pass@any=3/3` (tie with base, slight runtime overhead).

Interpretation:
1. Added verbosity/snippet payload increases noise and degrades quality for this model.
2. `spec_contract` can help in some runs but is not clearly superior to base in repeated runs.

### 4) Baseline head-to-head on wider set (4 tasks found, tries=2)

Tasks resolved by benchmark:
- `react`, `space-age`, `promises`, `rational-numbers`

Results:
1. Old default (`qwen3-coder-next-bf16` with `prompts/qwen3-coder.txt`): `Pass@any=2/4`.
2. `v6` (`qwen3-coder-next-bf16-v6-generic-two-pass`): `Pass@any=4/4`.

Decision:
1. Promote `v6` to default baseline by pointing `mlx/qwen3-coder-next-bf16` to `prompts/qwen3-coder-v6-generic-two-pass.txt`.
2. Keep `LOCALCODE_WRITE_SNIPPET_SUCCESS=0` and keep extra verbosity toggles disabled by default.
3. Keep `spec_contract` optional (not default) until broader multi-task evidence shows consistent gain.
