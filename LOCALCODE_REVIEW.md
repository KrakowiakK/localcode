# Localcode Code Review (2026-02-13)

Scope: review of recent Localcode changes around tool feedback, read/edit formats, benchmark flag plumbing.

## Findings

### 1) Blocker: `write()` no-op path uses `full_drop_fields` before initialization

- Impact: potential `NameError` when `LOCALCODE_WRITE_VERBOSE_STATE=1` and a `write` call is a no-op (writing identical content). This can break benchmark runs in a non-obvious way because it only triggers on certain tool sequences.
- Location: `localcode/tool_handlers/write_handlers.py` in `write()` within the `old_content == content` branch.
- Fix: initialize `full_drop_fields = _write_full_drop_fields()` once near the start of `write()` (before any early returns), and remove the later redundant initialization.

### 2) Config consistency gap: shared benchmark flags file does not cover all `LOCALCODE_*` env vars used by Localcode

- Impact: the new `Localcode runtime flags (effective)` table in `bin/run-benchmark.sh` will only include keys listed in `bin/localcode-flags.sh`. Some Localcode env vars are missing from that list, so the stats output can be incomplete and misleading.
- Example missing keys: `LOCALCODE_TOOL_HINTS`, `LOCALCODE_BENCHMARK`, `LOCALCODE_BLOCK_TEST_EDITS`, `LOCALCODE_SEND_TOOL_CATEGORIES`, `LOCALCODE_HISTORY_MAX_MESSAGES`, etc.
- Suggestion: treat `bin/localcode-flags.sh` as “benchmark runner defaults” and either:
  - extend it to include additional keys (even if empty/default), or
  - in stats collection, print both:
    - the effective benchmark defaults set (from `LOCALCODE_FLAG_KEYS_CSV`)
    - and a separate “Localcode env vars detected” list derived from the log request params or a known allowlist.

### 3) Tool feedback verbosity and consistency

- Current state: `LOCALCODE_WRITE_FULL_DROP` adds a useful mechanism to reduce noise, but the output still contains multiple semi-overlapping summary lines (`file_state`, `change_summary`, `decision_hint`, plus optional preview/snippet).
- Risk: too much repeated metadata can cost context window and increase “tool panic” in small instruct models.
- Suggestion: consider a single canonical “mutation summary line” plus at most one evidence block (either changed preview or region snippet), with the rest behind verbose toggles.

## Notes

- Unit tests exist and are extensive (`localcode/tests`).
- After the fix in Finding 1, run `./.venv/bin/python -m pytest -q localcode/tests`.
