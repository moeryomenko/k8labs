#!/usr/bin/env python3
"""eevdf-analyze.py — Extract EEVDF scheduler metrics from perfetto traces and CSV inputs.

Usage:
    eevdf-analyze.py --trace <path> [--output-dir PATH]
    eevdf-analyze.py --trace-dir <dir> [--output-dir PATH]
    eevdf-analyze.py --csv-dir <dir> [--output-dir PATH]
    eevdf-analyze.py --help

Processes perfetto trace files (via the perfetto Python API) or time-series CSVs
from eevdf-observe.sh / cgroup-pid-watch.sh, extracting four EEVDF-specific data
views:

  1. eevdf-vruntime.csv  — Per-task vruntime trajectory over time (ns)
  2. eevdf-slices.csv    — CPU slice durations (us)
  3. eevdf-latency.csv   — Scheduling wakeup latency (us)
  4. eevdf-lag.csv       — Per-task lag vs min_vruntime (us)

If the perfetto package is not installed, use ``--no-perfetto`` to operate on
CSV inputs only.

The module is also importable — callers can access the processing functions
directly.
"""

from __future__ import annotations

import argparse
import csv as csv_module
import json
import os
import sys


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message: str, level: str = "info") -> None:
    """Print a timestamped log message to stderr."""
    print(f"[{level}] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output CSV column definitions
# ---------------------------------------------------------------------------

VRUNTIME_CSV_COLS = [
    "timestamp",
    "pid",
    "task",
    "vruntime",
    "cpu",
]

SLICES_CSV_COLS = [
    "timestamp_start",
    "timestamp_end",
    "duration_us",
    "pid",
    "task",
    "cpu",
]

LATENCY_CSV_COLS = [
    "timestamp",
    "pid",
    "task",
    "wakeup_latency_us",
    "cpu",
]

LAG_CSV_COLS = [
    "timestamp",
    "pid",
    "task",
    "vruntime",
    "min_vruntime",
    "lag_us",
    "cpu",
]

OUTPUT_CSV_DEFS: dict[str, list[str]] = {
    "eevdf-vruntime.csv": VRUNTIME_CSV_COLS,
    "eevdf-slices.csv": SLICES_CSV_COLS,
    "eevdf-latency.csv": LATENCY_CSV_COLS,
    "eevdf-lag.csv": LAG_CSV_COLS,
}


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _write_empty_csv(path: str, columns: list[str]) -> None:
    """Write a CSV file with only a header row."""
    with open(path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow(columns)


def _write_data_csv(path: str, columns: list[str], rows: list[list]) -> None:
    """Write rows to a CSV file with a header."""
    with open(path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def _write_dataframe_csv(path: str, df) -> None:
    """Write a pandas DataFrame to CSV, or empty headers if no rows."""
    if df is None or df.empty:
        csv_name = os.path.basename(path)
        _write_empty_csv(
            path,
            OUTPUT_CSV_DEFS.get(csv_name, list(df.columns) if df is not None else []),
        )
    else:
        df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Perfetto trace processing
# ---------------------------------------------------------------------------


def _process_perfetto_trace(tp) -> dict[str, list]:
    """Run EEVDF-specific SQL queries against a perfetto TraceProcessor.

    Args:
        tp: A perfetto.trace_processor.TraceProcessor instance.

    Returns:
        dict with keys 'vruntime', 'slices', 'latency', 'lag', each
        containing a list of row tuples.
    """
    results: dict[str, list] = {
        "vruntime": [],
        "slices": [],
        "latency": [],
        "lag": [],
    }

    # ---- Query 1: vruntime trajectory from sched_stat_runtime ----
    try:
        vr_df = tp.query("""
SELECT
  ts,
  thread.tid AS pid,
  thread.name AS task,
  cpu,
  IFNULL(value, 0) AS vruntime
FROM sched_stat_runtime
LEFT JOIN thread USING (utid)
WHERE value IS NOT NULL
ORDER BY ts
""").as_pandas_dataframe()
        if vr_df is not None and not vr_df.empty:
            # Convert ts from ns to us for the CSV
            vr_df["timestamp"] = vr_df["ts"] / 1000.0
            for _, row in vr_df.iterrows():
                results["vruntime"].append(
                    [
                        row.get("timestamp", 0),
                        int(row.get("pid", 0) if row.get("pid") else 0),
                        str(row.get("task", "")),
                        float(row.get("vruntime", 0)),
                        int(row.get("cpu", 0) if row.get("cpu") else 0),
                    ]
                )
    except Exception as exc:
        log(f"  vruntime query failed: {exc}", level="warn")

    # ---- Query 2: slice durations from sched_switch ----
    try:
        sl_df = tp.query("""
SELECT
  ts,
  dur,
  cpu,
  next_pid AS pid,
  next_comm AS task
FROM sched_switch
WHERE dur >= 0 AND next_pid > 0
ORDER BY ts
""").as_pandas_dataframe()
        if sl_df is not None and not sl_df.empty:
            for _, row in sl_df.iterrows():
                ts_start = row.get("ts", 0) / 1000.0
                duration_ns = row.get("dur", 0)
                ts_end = ts_start + duration_ns / 1000.0
                results["slices"].append(
                    [
                        ts_start,
                        ts_end,
                        duration_ns / 1000.0,
                        int(row.get("pid", 0) if row.get("pid") else 0),
                        str(row.get("task", "")),
                        int(row.get("cpu", 0) if row.get("cpu") else 0),
                    ]
                )
    except Exception as exc:
        log(f"  slices query failed: {exc}", level="warn")

    # ---- Query 3: wakeup latency from sched_waking + sched_switch ----
    try:
        lt_df = tp.query("""
SELECT
  waked.ts AS wake_ts,
  MIN(sched.ts) AS sched_ts,
  cpu,
  waked.tid AS pid,
  thread.name AS task
FROM sched_waking AS waked
LEFT JOIN thread ON waked.utid = thread.utid
LEFT JOIN sched_slice AS sched ON waked.utid = sched.utid
  AND sched.ts >= waked.ts
WHERE sched.ts IS NOT NULL
GROUP BY waked.ts, waked.tid
ORDER BY waked.ts
""").as_pandas_dataframe()
        if lt_df is not None and not lt_df.empty:
            for _, row in lt_df.iterrows():
                wake_ts = row.get("wake_ts", 0)
                sched_ts = row.get("sched_ts", 0)
                latency_us = (sched_ts - wake_ts) / 1000.0
                results["latency"].append(
                    [
                        wake_ts / 1000.0,
                        int(row.get("pid", 0) if row.get("pid") else 0),
                        str(row.get("task", "")),
                        latency_us,
                        int(row.get("cpu", 0) if row.get("cpu") else 0),
                    ]
                )
    except Exception as exc:
        log(f"  latency query failed: {exc}", level="warn")

    # ---- Query 4: lag estimation (vruntime from sched_stat_runtime,
    #      group min per CPU window) ----
    try:
        lag_df = tp.query("""
WITH vr AS (
  SELECT
    ts,
    cpu,
    thread.tid AS pid,
    thread.name AS task,
    value AS vruntime
  FROM sched_stat_runtime
  LEFT JOIN thread USING (utid)
  WHERE value IS NOT NULL
)
SELECT
  a.ts,
  a.cpu,
  a.pid,
  a.task,
  a.vruntime,
  (SELECT MIN(b.vruntime) FROM vr b
   WHERE b.cpu = a.cpu
     AND b.ts BETWEEN a.ts - 10000000 AND a.ts + 10000000) AS min_vruntime
FROM vr a
ORDER BY a.ts
""").as_pandas_dataframe()
        if lag_df is not None and not lag_df.empty:
            for _, row in lag_df.iterrows():
                vr = float(row.get("vruntime", 0))
                min_vr = float(
                    row.get("min_vruntime", 0) if row.get("min_vruntime") else 0
                )
                lag_us = (vr - min_vr) / 1000.0
                results["lag"].append(
                    [
                        row.get("ts", 0) / 1000.0,
                        int(row.get("pid", 0) if row.get("pid") else 0),
                        str(row.get("task", "")),
                        vr,
                        min_vr,
                        lag_us,
                        int(row.get("cpu", 0) if row.get("cpu") else 0),
                    ]
                )
    except Exception as exc:
        log(f"  lag query failed: {exc}", level="warn")

    return results


def process_trace_file_perfetto(trace_path: str, output_dir: str) -> int:
    """Process a single .perfetto-trace file and produce EEVDF CSVs.

    Args:
        trace_path: Path to the .perfetto-trace file.
        output_dir: Directory for output CSV files.

    Returns:
        0 on success, 1 on error.
    """
    try:
        from perfetto.trace_processor import TraceProcessor  # noqa: F811
    except ImportError:
        log(
            "perfetto package not available. Install with: pip install perfetto",
            level="error",
        )
        return 1

    log(f"Loading trace: {trace_path}")
    try:
        tp = TraceProcessor(file_path=trace_path)
    except Exception as exc:
        log(f"Failed to load trace: {exc}", level="error")
        return 1

    results = _process_perfetto_trace(tp)

    os.makedirs(output_dir, exist_ok=True)

    for csv_name, rows in [
        ("eevdf-vruntime.csv", results["vruntime"]),
        ("eevdf-slices.csv", results["slices"]),
        ("eevdf-latency.csv", results["latency"]),
        ("eevdf-lag.csv", results["lag"]),
    ]:
        csv_path = os.path.join(output_dir, csv_name)
        cols = OUTPUT_CSV_DEFS[csv_name]
        if not rows:
            _write_empty_csv(csv_path, cols)
            log(f"  {csv_name} — no data (empty)")
        else:
            _write_data_csv(csv_path, cols, rows)
            log(f"  {csv_name} — {len(rows)} rows")

    return 0


def analyze_trace_dir(
    trace_dir: str, output_dir: str, no_perfetto: bool = False
) -> int:
    """Process all .perfetto-trace files in a directory.

    Args:
        trace_dir: Directory containing .perfetto-trace files.
        output_dir: Root output directory.  Each trace gets a subdirectory.
        no_perfetto: If True, skip perfetto processing.

    Returns:
        0 on success, 1 on error.
    """
    if no_perfetto:
        log("--no-perfetto set; skipping perfetto trace processing", level="warn")
        return 0

    trace_files = sorted(
        os.path.join(trace_dir, f)
        for f in os.listdir(trace_dir)
        if f.endswith(".perfetto-trace")
    )
    if not trace_files:
        log(f"No .perfetto-trace files found in {trace_dir}", level="warn")
        return 0

    for tf in trace_files:
        basename = os.path.splitext(os.path.basename(tf))[0]
        out_subdir = os.path.join(output_dir, basename)
        os.makedirs(out_subdir, exist_ok=True)

        rc = process_trace_file_perfetto(tf, out_subdir)
        if rc != 0:
            log(f"Failed to process {tf}", level="error")
            return 1

    return 0


# ---------------------------------------------------------------------------
# CSV input processing (from eevdf-observe.sh / cgroup-pid-watch.sh)
# ---------------------------------------------------------------------------


def process_csv_input(csv_dir: str, output_dir: str) -> int:
    """Process CSV files from eevdf-observe.sh or cgroup-pid-watch.sh.

    Looks for:
      - eevdf-observe JSON output (if present as JSON files)
      - cgroup-pid-watch CSV output

    Args:
        csv_dir: Directory containing the CSV/JSON input files.
        output_dir: Directory for output CSV files.

    Returns:
        0 on success, 1 on error.
    """
    os.makedirs(output_dir, exist_ok=True)

    # ---- Check for eevdf-observe JSON snapshots ----
    vruntime_rows: list[list] = []
    lag_rows: list[list] = []
    json_files = sorted(
        os.path.join(csv_dir, f) for f in os.listdir(csv_dir) if f.endswith(".json")
    )
    for jf in json_files:
        try:
            with open(jf) as f:
                data = json.load(f)
        except (json.JSONDecodeError, Exception) as exc:
            log(f"  Skipping unreadable JSON {jf}: {exc}", level="warn")
            continue

        tasks = data.get("tasks", [])
        timestamp = data.get("timestamp", "0")
        for task in tasks:
            pid = task.get("pid", 0)
            task_name = task.get("name", task.get("comm", ""))
            se = task.get("se", {})
            vruntime_val = se.get("vruntime", 0)

            vruntime_rows.append(
                [
                    timestamp,
                    pid,
                    task_name,
                    vruntime_val,
                    "",  # cpu unknown from JSON
                ]
            )
            # Lag estimate: if min_vruntime not available, set to 0
            lag_rows.append(
                [
                    timestamp,
                    pid,
                    task_name,
                    vruntime_val,
                    0,  # min_vruntime not in observe output
                    0,  # lag not computed
                    "",
                ]
            )

    # Write vruntime CSV
    csv_path = os.path.join(output_dir, "eevdf-vruntime.csv")
    if vruntime_rows:
        _write_data_csv(csv_path, VRUNTIME_CSV_COLS, vruntime_rows)
        log(f"  eevdf-vruntime.csv — {len(vruntime_rows)} rows")
    else:
        _write_empty_csv(csv_path, VRUNTIME_CSV_COLS)
        log(f"  eevdf-vruntime.csv — no data (empty)")

    # Write lag CSV
    csv_path = os.path.join(output_dir, "eevdf-lag.csv")
    if lag_rows:
        _write_data_csv(csv_path, LAG_CSV_COLS, lag_rows)
        log(f"  eevdf-lag.csv — {len(lag_rows)} rows")
    else:
        _write_empty_csv(csv_path, LAG_CSV_COLS)
        log(f"  eevdf-lag.csv — no data (empty)")

    # ---- Check for cgroup-pid-watch CSV (slices / latency) ----
    slice_rows: list[list] = []
    latency_rows: list[list] = []
    csv_files = sorted(
        os.path.join(csv_dir, f) for f in os.listdir(csv_dir) if f.endswith(".csv")
    )
    for cf in csv_files:
        try:
            import pandas as pd  # noqa: F811

            df = pd.read_csv(cf)
        except Exception:
            df = None

        if df is not None and not df.empty:
            # Detect columns — cgroup-pid-watch produces timestamp, pid, runtime_avg, etc.
            cols_lower = [c.lower() for c in df.columns]

            # Check if this looks like a scheduling time-series CSV
            if "timestamp" in cols_lower and "pid" in cols_lower:
                # Extract slice-like data from runtime_avg if present
                if "runtime_avg" in cols_lower:
                    for _, row in df.iterrows():
                        slice_rows.append(
                            [
                                row.get("timestamp", 0),
                                row.get("timestamp", 0),
                                row.get("runtime_avg", 0),
                                int(row.get("pid", 0) if row.get("pid") else 0),
                                str(row.get("task", row.get("name", ""))),
                                "",
                            ]
                        )

                # Extract wakeup-like data from wait_sum if present
                if "wait_sum" in cols_lower:
                    for _, row in df.iterrows():
                        latency_rows.append(
                            [
                                row.get("timestamp", 0),
                                int(row.get("pid", 0) if row.get("pid") else 0),
                                str(row.get("task", row.get("name", ""))),
                                row.get("wait_sum", 0),
                                "",
                            ]
                        )

    # Write slices CSV
    csv_path = os.path.join(output_dir, "eevdf-slices.csv")
    if slice_rows:
        _write_data_csv(csv_path, SLICES_CSV_COLS, slice_rows)
        log(f"  eevdf-slices.csv — {len(slice_rows)} rows")
    else:
        _write_empty_csv(csv_path, SLICES_CSV_COLS)
        log(f"  eevdf-slices.csv — no data (empty)")

    # Write latency CSV
    csv_path = os.path.join(output_dir, "eevdf-latency.csv")
    if latency_rows:
        _write_data_csv(csv_path, LATENCY_CSV_COLS, latency_rows)
        log(f"  eevdf-latency.csv — {len(latency_rows)} rows")
    else:
        _write_empty_csv(csv_path, LATENCY_CSV_COLS)
        log(f"  eevdf-latency.csv — no data (empty)")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Extract EEVDF scheduler metrics from perfetto traces or CSV inputs."
        ),
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--trace",
        metavar="FILE",
        help="Path to a single .perfetto-trace file",
    )
    input_group.add_argument(
        "--trace-dir",
        metavar="DIR",
        help="Directory containing .perfetto-trace files",
    )
    input_group.add_argument(
        "--csv-dir",
        metavar="DIR",
        help="Directory containing CSV/JSON input from eevdf-observe.sh or cgroup-pid-watch.sh",
    )
    parser.add_argument(
        "--output-dir",
        default="./eevdf-analysis",
        help="Output directory for CSV results (default: ./eevdf-analysis)",
    )
    parser.add_argument(
        "--no-perfetto",
        action="store_true",
        help="Skip perfetto trace processing; only process CSV inputs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run EEVDF analysis.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code for the process.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    # Determine input mode
    trace_path = args.trace
    trace_dir = args.trace_dir
    csv_dir = args.csv_dir

    input_count = sum(1 for x in [trace_path, trace_dir, csv_dir] if x)

    if input_count == 0:
        log("No input specified. Use --trace, --trace-dir, or --csv-dir", level="error")
        return 1

    if trace_path:
        if not os.path.isfile(trace_path):
            log(f"Not found: {trace_path}", level="error")
            return 1
        return process_trace_file_perfetto(trace_path, output_dir)

    if trace_dir:
        if not os.path.isdir(trace_dir):
            log(f"Not found: {trace_dir}", level="error")
            return 1
        return analyze_trace_dir(trace_dir, output_dir, no_perfetto=args.no_perfetto)

    if csv_dir:
        if not os.path.isdir(csv_dir):
            log(f"Not found: {csv_dir}", level="error")
            return 1
        return process_csv_input(csv_dir, output_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
