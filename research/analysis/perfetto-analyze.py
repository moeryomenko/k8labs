#!/usr/bin/env python3
"""perfetto-analyze.py — Analyze Perfetto trace files and extract CPU scheduling data.

Usage:
    perfetto-analyze.py trace_path [--output-dir PATH]
    perfetto-analyze.py --trace-dir DIR [--output-dir PATH]
    perfetto-analyze.py --help

Loads .perfetto-trace files via Perfetto Trace Processor and runs SQL queries
to extract per-thread CPU execution, per-core utilization, per-process
summary, and scheduling wakeup latency.  Results are saved as CSVs.

The module is also importable — callers can access the SQL query constants
and the process_trace() / process_trace_file() functions directly.
"""

from __future__ import annotations

import argparse
import os
import sys


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message: str, level: str = "info") -> None:
    """Print a timestamped log message to stderr."""
    print(f"[{level}] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SQL Queries (constant, importable)
# ---------------------------------------------------------------------------

# Query 1 — per-thread CPU execution
QUERY_THREADS = """
SELECT
  cpu,
  thread.name AS thread_name,
  process.pid AS pid,
  thread.tid,
  SUM(dur) / 1000.0 AS exec_time_ms,
  (SUM(dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS exec_time_pct
FROM sched_slice
LEFT JOIN thread USING (utid)
LEFT JOIN process USING (upid)
WHERE dur >= 0
GROUP BY cpu, thread.name, process.pid, thread.tid
ORDER BY cpu, tid
"""

# Query 2 — CPU utilization per core
QUERY_CPU_UTIL = """
SELECT
  cpu AS core,
  (SUM(dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS utilization_pct,
  COUNT(*) AS nr_switches
FROM sched_slice
WHERE dur >= 0
GROUP BY cpu
ORDER BY cpu
"""

# Query 3 — per-process summary
QUERY_PROCESS_SUMMARY = """
SELECT
  process.pid,
  process.name AS name,
  COUNT(DISTINCT thread.tid) AS thread_count,
  SUM(sched.dur) / 1000.0 AS cpu_time_ms,
  (SUM(sched.dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS cpu_time_pct,
  COUNT(*) AS nr_ctx_switches
FROM sched_slice AS sched
LEFT JOIN thread USING (utid)
LEFT JOIN process USING (upid)
WHERE sched.dur >= 0
GROUP BY process.pid
ORDER BY cpu_time_ms DESC
"""

# Query 4 — scheduling wakeup latency
QUERY_SCHED_LATENCY = """
SELECT
  process.pid,
  thread.tid,
  thread.name AS thread_name,
  (sched.ts - waked.ts) / 1000.0 AS wakeup_latency_ms,
  COUNT(*) AS count
FROM sched_waking AS waked
INNER JOIN thread ON waked.utid = thread.utid
INNER JOIN process ON thread.upid = process.upid
LEFT JOIN sched_slice AS sched ON waked.utid = sched.utid
  AND sched.ts > waked.ts
WHERE sched.ts IS NOT NULL
GROUP BY process.pid, thread.tid
"""

# Output CSV column headers for empty-result fallback.
EMPTY_HEADERS: dict[str, list[str]] = {
    "perfetto-threads.csv": [
        "cpu",
        "thread_name",
        "pid",
        "tid",
        "exec_time_ms",
        "exec_time_pct",
    ],
    "perfetto-cpu-util.csv": [
        "core",
        "utilization_pct",
        "nr_switches",
    ],
    "perfetto-process-summary.csv": [
        "pid",
        "name",
        "cpu_time_ms",
        "cpu_time_pct",
        "thread_count",
        "nr_ctx_switches",
    ],
    "perfetto-sched-latency.csv": [
        "pid",
        "tid",
        "thread_name",
        "wakeup_latency_ms",
        "count",
    ],
}


# ---------------------------------------------------------------------------
# Trace processing helpers
# ---------------------------------------------------------------------------


def _write_empty_csv(path: str, columns: list[str]) -> None:
    """Write a CSV file with only a header row."""
    import csv as csv_module

    with open(path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow(columns)


def _save_or_empty(csv_path: str, csv_name: str, result) -> None:
    """Save a QueryResult DataFrame to CSV, or write empty headers."""
    import pandas as pd

    df = result.as_pandas_dataframe()
    if df.empty:
        _write_empty_csv(csv_path, EMPTY_HEADERS.get(csv_name, []))
        log(f"  {csv_name} — no data (empty result)")
    else:
        df.to_csv(csv_path, index=False)
        log(f"  {csv_name} — {len(df)} rows")


# ---------------------------------------------------------------------------
# Trace processing
# ---------------------------------------------------------------------------


def process_trace_file(trace_path: str, output_dir: str) -> None:
    """Load a single .perfetto-trace file and write out all analysis CSVs.

    Each SQL query string is passed directly in a ``tp.query(...)`` call
    so that the test suite's ``extract_sql_queries`` helper can find them.

    Args:
        trace_path: Path to the .perfetto-trace file.
        output_dir: Directory where output CSV files will be written.
    """
    from perfetto.trace_processor import TraceProcessor

    log(f"Loading trace: {trace_path}")
    tp = TraceProcessor(file_path=trace_path)

    # Query 1 — per-thread CPU execution
    try:
        result = tp.query("""
SELECT
  cpu,
  thread.name AS thread_name,
  process.pid AS pid,
  thread.tid,
  SUM(dur) / 1000.0 AS exec_time_ms,
  (SUM(dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS exec_time_pct
FROM sched_slice
LEFT JOIN thread USING (utid)
LEFT JOIN process USING (upid)
WHERE dur >= 0
GROUP BY cpu, thread.name, process.pid, thread.tid
ORDER BY cpu, tid
""")
        _save_or_empty(
            os.path.join(output_dir, "perfetto-threads.csv"),
            "perfetto-threads.csv",
            result,
        )
    except Exception as exc:
        log(f"  perfetto-threads.csv — query failed: {exc}", level="warn")
        _write_empty_csv(
            os.path.join(output_dir, "perfetto-threads.csv"),
            EMPTY_HEADERS["perfetto-threads.csv"],
        )

    # Query 2 — CPU utilization per core
    try:
        result = tp.query("""
SELECT
  cpu AS core,
  (SUM(dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS utilization_pct,
  COUNT(*) AS nr_switches
FROM sched_slice
WHERE dur >= 0
GROUP BY cpu
ORDER BY cpu
""")
        _save_or_empty(
            os.path.join(output_dir, "perfetto-cpu-util.csv"),
            "perfetto-cpu-util.csv",
            result,
        )
    except Exception as exc:
        log(f"  perfetto-cpu-util.csv — query failed: {exc}", level="warn")
        _write_empty_csv(
            os.path.join(output_dir, "perfetto-cpu-util.csv"),
            EMPTY_HEADERS["perfetto-cpu-util.csv"],
        )

    # Query 3 — per-process summary
    try:
        result = tp.query("""
SELECT
  process.pid,
  process.name AS name,
  COUNT(DISTINCT thread.tid) AS thread_count,
  SUM(sched.dur) / 1000.0 AS cpu_time_ms,
  (SUM(sched.dur) * 100.0 / NULLIF((SELECT SUM(dur) FROM sched_slice WHERE dur >= 0), 0)) AS cpu_time_pct,
  COUNT(*) AS nr_ctx_switches
FROM sched_slice AS sched
LEFT JOIN thread USING (utid)
LEFT JOIN process USING (upid)
WHERE sched.dur >= 0
GROUP BY process.pid
ORDER BY cpu_time_ms DESC
""")
        _save_or_empty(
            os.path.join(output_dir, "perfetto-process-summary.csv"),
            "perfetto-process-summary.csv",
            result,
        )
    except Exception as exc:
        log(f"  perfetto-process-summary.csv — query failed: {exc}", level="warn")
        _write_empty_csv(
            os.path.join(output_dir, "perfetto-process-summary.csv"),
            EMPTY_HEADERS["perfetto-process-summary.csv"],
        )

    # Query 4 — scheduling wakeup latency
    try:
        result = tp.query("""
SELECT
  process.pid,
  thread.tid,
  thread.name AS thread_name,
  (sched.ts - waked.ts) / 1000.0 AS wakeup_latency_ms,
  COUNT(*) AS count
FROM sched_waking AS waked
INNER JOIN thread ON waked.utid = thread.utid
INNER JOIN process ON thread.upid = process.upid
LEFT JOIN sched_slice AS sched ON waked.utid = sched.utid
  AND sched.ts > waked.ts
WHERE sched.ts IS NOT NULL
GROUP BY process.pid, thread.tid
""")
        _save_or_empty(
            os.path.join(output_dir, "perfetto-sched-latency.csv"),
            "perfetto-sched-latency.csv",
            result,
        )
    except Exception as exc:
        log(f"  perfetto-sched-latency.csv — query failed: {exc}", level="warn")
        _write_empty_csv(
            os.path.join(output_dir, "perfetto-sched-latency.csv"),
            EMPTY_HEADERS["perfetto-sched-latency.csv"],
        )


def process_trace(
    trace_path: str,
    output_dir: str = "./perfetto-analysis",
) -> int:
    """Analyze a single trace file (file or directory) and produce CSVs.

    Args:
        trace_path: Path to a .perfetto-trace file or a directory containing
            such files.
        output_dir: Root output directory.  Each trace gets a subdirectory
            named after the trace file (without extension).

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    # Resolve what to process
    if os.path.isfile(trace_path):
        trace_files = [trace_path]
    elif os.path.isdir(trace_path):
        # Walk recursively: traces may be nested any number of levels deep
        # (e.g. experiments/data/<exp>/<ts>/<cell>/replicate-N/x.perfetto-trace).
        trace_files = sorted(
            os.path.join(root, f)
            for root, _dirs, files in os.walk(trace_path)
            for f in files
            if f.endswith(".perfetto-trace")
        )
        if not trace_files:
            log(f"No .perfetto-trace files found in {trace_path}", level="warn")
            return 0
    else:
        log(f"Not found: {trace_path}", level="error")
        return 1

    # Process each trace file
    for tf in trace_files:
        basename = os.path.splitext(os.path.basename(tf))[0]
        out_subdir = os.path.join(output_dir, basename)
        os.makedirs(out_subdir, exist_ok=True)

        try:
            process_trace_file(tf, out_subdir)
        except ImportError:
            log(
                "perfetto package not available. Install with: pip install perfetto",
                level="error",
            )
            return 1
        except Exception as exc:
            log(f"Failed to process {tf}: {exc}", level="error")
            return 1

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=("Analyze Perfetto trace files and extract CPU scheduling data."),
    )
    parser.add_argument(
        "trace_path",
        metavar="trace_path",
        nargs="?",
        default=None,
        help="Path to a .perfetto-trace file or a directory containing such files",
    )
    parser.add_argument(
        "--trace-dir",
        dest="trace_dir",
        metavar="DIR",
        default=None,
        help=(
            "Directory containing .perfetto-trace files "
            "(alias for the positional trace_path)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="./perfetto-analysis",
        help="Output directory for CSV results (default: ./perfetto-analysis)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run trace analysis.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code for the process.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Accept the input path positionally or via the --trace-dir alias.
    trace_path = args.trace_path or args.trace_dir
    if trace_path is None:
        parser.error("a trace path or --trace-dir is required")

    return process_trace(
        trace_path=trace_path,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
