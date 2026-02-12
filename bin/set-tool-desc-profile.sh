#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/localcode/tools"
BASELINE_DIR="$ROOT_DIR/optimizations/tool_desc_profiles/baseline"

profile="${1:-}"
if [[ -z "$profile" ]]; then
  echo "usage: $0 <baseline|minimal|decision|decision2>" >&2
  exit 1
fi

apply_desc() {
  local file="$1"
  local desc="$2"
  local tmp
  tmp="$(mktemp)"
  jq --arg d "$desc" '.description = $d' "$file" > "$tmp"
  mv "$tmp" "$file"
}

restore_baseline() {
  cp "$BASELINE_DIR/read.json" "$TOOLS_DIR/read.json"
  cp "$BASELINE_DIR/write.json" "$TOOLS_DIR/write.json"
  cp "$BASELINE_DIR/edit.json" "$TOOLS_DIR/edit.json"
  cp "$BASELINE_DIR/apply_patch.json" "$TOOLS_DIR/apply_patch.json"
  cp "$BASELINE_DIR/finish.json" "$TOOLS_DIR/finish.json"
}

set_minimal() {
  apply_desc "$TOOLS_DIR/read.json" "Read file content with line numbers. Use when you need exact current code or tests."
  apply_desc "$TOOLS_DIR/write.json" "Create or overwrite a full file. Use for initial implementation or full rewrite."
  apply_desc "$TOOLS_DIR/edit.json" "Replace exact text in an existing file. Prefer this for small targeted changes."
  apply_desc "$TOOLS_DIR/apply_patch.json" "Apply a unified patch with context lines. Use for structured multi-line changes."
  apply_desc "$TOOLS_DIR/finish.json" "End the run when implementation is complete and no more code changes are needed."
}

set_decision() {
  apply_desc "$TOOLS_DIR/read.json" "Read a file with line numbers. Use when text context is missing or stale. Do not repeat read on unchanged file unless new context is needed."
  apply_desc "$TOOLS_DIR/write.json" "Create or replace a full file. Use for first full implementation. After a successful write, prefer edit for small follow-up fixes instead of another full write."
  apply_desc "$TOOLS_DIR/edit.json" "Replace one exact snippet in an existing file. Use this after read/write for small corrective changes. If old text is not found, read current file and retry with exact text."
  apply_desc "$TOOLS_DIR/apply_patch.json" "Apply patch blocks with exact context lines. Use for precise structured edits, especially multi-file or hunk-style updates. If patch fails, re-read and patch current content."
  apply_desc "$TOOLS_DIR/finish.json" "Call when code is complete. Set status to one of: done, blocked, incomplete (default: done). Example: finish({\"status\":\"done\",\"summary\":\"implemented and verified\"}). If recent tool output says no change and requirements are already implemented, finish instead of repeating the same write/edit call."
}

set_decision2() {
  apply_desc "$TOOLS_DIR/read.json" "Read a file with line numbers. Use when text context is missing or stale. Do not repeat read on unchanged file unless new context is needed."
  apply_desc "$TOOLS_DIR/write.json" "Create or replace a full file. Use for first implementation or large rewrite. Do not repeat write with same content. After one successful write, use edit for focused fixes."
  apply_desc "$TOOLS_DIR/edit.json" "Replace exact snippet in existing file. Preferred after write for small corrective changes. Use exact old text from latest file state. If replacement is ambiguous, refine old snippet."
  apply_desc "$TOOLS_DIR/apply_patch.json" "Apply patch blocks with exact context lines. Use for precise structured edits, especially multi-file or hunk-style updates. If patch fails, re-read and patch current content."
  apply_desc "$TOOLS_DIR/finish.json" "Call when implementation is complete and consistent with requirements. Set status to one of: done, blocked, incomplete (default: done). Example: finish({\"status\":\"done\",\"summary\":\"implemented and verified\"}). If recent mutation changed code, prefer one verification read before finish. If recent call was no-op, choose finish or a different edit, never same write again."
}

case "$profile" in
  baseline)
    restore_baseline
    ;;
  minimal)
    restore_baseline
    set_minimal
    ;;
  decision)
    restore_baseline
    set_decision
    ;;
  decision2)
    restore_baseline
    set_decision2
    ;;
  *)
    echo "unknown profile: $profile" >&2
    echo "usage: $0 <baseline|minimal|decision|decision2>" >&2
    exit 1
    ;;
esac

echo "tool description profile set: $profile"
