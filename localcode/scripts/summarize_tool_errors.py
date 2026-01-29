#!/usr/bin/env python3
"""Summarize tool errors per localcode run."""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _format_counts(counts: Optional[Dict[str, Any]]) -> str:
    if not counts:
        return "-"
    parts = []
    for key in sorted(counts.keys()):
        parts.append(f"{key}:{counts[key]}")
    return ", ".join(parts)


def _load_run_end(path: Path) -> Optional[Dict[str, Any]]:
    run_end = None
    fallback = None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = obj.get("event")
        if event == "run_end":
            run_end = obj
        elif event in ("agent_done", "agent_abort"):
            fallback = obj
    if run_end:
        run_end["_source"] = "run_end"
        return run_end
    if fallback:
        fallback["_source"] = "agent_done"
        return fallback
    return None


def _collect_logs(log_dir: Path, pattern: str) -> List[Tuple[Path, Dict[str, Any]]]:
    rows = []
    for path in sorted(log_dir.glob(pattern)):
        run = _load_run_end(path)
        if not run:
            continue
        rows.append((path, run))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize tool errors per run.")
    parser.add_argument(
        "--logs-dir",
        default=str(Path(__file__).resolve().parents[1] / "logs"),
        help="Path to localcode logs directory.",
    )
    parser.add_argument(
        "--pattern",
        default="localcode_benchmark_*.jsonl",
        help="Glob pattern for log files.",
    )
    parser.add_argument(
        "--sort",
        choices=["mtime", "name"],
        default="mtime",
        help="Sort order for logs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of rows (0 = no limit).",
    )
    args = parser.parse_args()

    log_dir = Path(args.logs_dir)
    rows = _collect_logs(log_dir, args.pattern)

    if args.sort == "mtime":
        rows.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    else:
        rows.sort(key=lambda item: item[0].name)

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    table = []
    for path, run in rows:
        table.append(
            {
                "file": path.name,
                "ts": run.get("ts", ""),
                "tool_calls_total": run.get("tool_calls_total"),
                "tool_errors_total": run.get("tool_errors_total"),
                "tool_error_counts": _format_counts(run.get("tool_error_counts")),
                "tool_call_counts": _format_counts(run.get("tool_call_counts")),
                "source": run.get("_source", ""),
            }
        )

    if not table:
        print("No run summaries found.")
        return 1

    headers = [
        "file",
        "ts",
        "tool_calls_total",
        "tool_errors_total",
        "tool_error_counts",
        "tool_call_counts",
        "source",
    ]
    widths = {h: len(h) for h in headers}
    for row in table:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))

    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    sep_line = "-+-".join("-" * widths[h] for h in headers)
    print(header_line)
    print(sep_line)
    for row in table:
        print(" | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
