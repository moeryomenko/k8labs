#!/usr/bin/env python3
"""Summarize pilot scheduler trace files for the CPU-limits figures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERIES_ROOT = Path(__file__).resolve().parents[1]
if str(SERIES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERIES_ROOT))

from capture.parse_trace import parse_and_write, parse_trace_csv

TARGET_COMMS = ("stress-ng", "nginx", "ffmpeg")


def target_pids(trace_csv: Path) -> set[int]:
    """Return PIDs whose command identifies a measured workload."""

    pids: set[int] = set()
    for event in parse_trace_csv(trace_csv):
        if any(marker in event.row.get("comm", "") for marker in TARGET_COMMS):
            pids.add(event.pid)
        raw_target = event.row.get("next_pid", "")
        if (
            any(marker in event.row.get("next_comm", "") for marker in TARGET_COMMS)
            and raw_target.isdigit()
        ):
            pids.add(int(raw_target))
    return pids


def trace_rows_for_cell(cell_dir: Path) -> list[dict[str, object]]:
    """Parse one cell trace directory into figure rows."""

    raw = cell_dir / "trace" / "raw" / "sched-trace.csv"
    if not raw.is_file():
        return []
    pids = target_pids(raw)
    if not pids:
        return []
    parsed = cell_dir / "trace" / "parsed"
    summary = parse_and_write(raw, pids, parsed)
    cell_id = cell_dir.name
    rows: list[dict[str, object]] = []
    for delay in summary["run_queue_delays_ns"]:
        rows.append(
            {
                "cell_id": cell_id,
                "event": "runqueue_delay",
                "ts_ns": delay["switch_ts_ns"],
                "latency_ns": delay["delay_ns"],
                "pid": delay["pid"],
            }
        )
    for interval in summary["on_cpu_intervals_ns"]:
        rows.append(
            {
                "cell_id": cell_id,
                "event": "on_cpu_start",
                "ts_ns": interval["start_ts_ns"],
                "pid": interval["pid"],
            }
        )
        rows.append(
            {
                "cell_id": cell_id,
                "event": "on_cpu_end",
                "ts_ns": interval["end_ts_ns"],
                "pid": interval["pid"],
            }
        )
    for event in summary["throttle_events"]:
        rows.append(
            {
                "cell_id": cell_id,
                "event": event["event"],
                "ts_ns": event["ts_ns"],
                "pid": event["pid"],
            }
        )
    return rows


def collect(pilot_root: Path) -> dict[str, object]:
    """Collect every parsed trace under one pilot tree."""

    cells = pilot_root / "cells"
    rows: list[dict[str, object]] = []
    parsed_cells: list[dict[str, object]] = []
    for status in sorted(cells.glob("block-*/*/status.json")):
        cell_dir = status.parent
        payload = json.loads(status.read_text(encoding="utf-8"))
        if payload.get("status") != "complete":
            continue
        cell_rows = trace_rows_for_cell(cell_dir)
        if not cell_rows:
            continue
        rows.extend(cell_rows)
        parsed_cells.append(
            {
                "cell_id": cell_dir.name,
                "block": cell_dir.parent.name,
                "rows": len(cell_rows),
            }
        )
    return {"protocol": "cpu-limits-v1", "trace_rows": rows, "cells": parsed_cells}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = collect(args.pilot_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(result['trace_rows'])} trace rows for {len(result['cells'])} cells"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
