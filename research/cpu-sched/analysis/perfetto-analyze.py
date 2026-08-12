#!/usr/bin/env python3
"""perfetto-analyze.py — Analyze Perfetto trace files and extract CPU scheduling data.

Usage:
    perfetto-analyze.py trace_path [--output-dir PATH]
    perfetto-analyze.py --trace-dir DIR [--output-dir PATH]
    perfetto-analyze.py --help

Loads .perfetto-trace files via Perfetto Trace Processor and runs SQL queries
to extract per-thread CPU execution, per-core utilization, per-process
summary, scheduling wakeup latency, and per-task runtime samples.  Results
are saved as CSVs.

The trace is loaded with raw ftrace ingestion enabled
(``TraceProcessorConfig(ingest_ftrace_in_raw=True)``), which is required for
kernel scheduler events (``sched_waking``, ``sched_stat_runtime``) to be
queryable through the ``ftrace_event`` table.

Known limitation: the Fedora 44 kernel's ``sched_stat_runtime`` tracepoint
carries no ``vruntime`` field, so EEVDF virtual-runtime analysis cannot be
derived from Perfetto traces.  EEVDF vruntime analysis is served by
``research/bin/eevdf-observe.sh`` (``/proc/<pid>/sched``), not Perfetto.

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

# Query 4 — scheduling wakeup latency (real, from raw ftrace)
#
# sched_waking raw ftrace events carry the WAKER in ftrace_event.utid; the
# woken task is identified by the 'pid' arg, so it is mapped back through
# thread.tid.  Latency is the time until the woken utid's next sched_slice.
QUERY_SCHED_LATENCY = """
SELECT
  process.pid,
  t_woken.tid,
  COALESCE(t_woken.name, a_comm.string_value) AS thread_name,
  AVG((next_slice.ts - w.ts) / 1000000.0) AS wakeup_latency_ms,
  COUNT(*) AS count
FROM ftrace_event AS w
JOIN args AS a_comm ON w.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON w.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_prio ON w.arg_set_id = a_prio.arg_set_id AND a_prio.key = 'prio'
JOIN thread AS t_woken ON t_woken.tid = a_pid.int_value
JOIN process ON t_woken.upid = process.upid
JOIN (
  SELECT
    w2.id AS waked_id,
    MIN(s.ts) AS ts
  FROM ftrace_event AS w2
  JOIN args AS a_pid2 ON w2.arg_set_id = a_pid2.arg_set_id AND a_pid2.key = 'pid'
  JOIN thread AS t2 ON t2.tid = a_pid2.int_value
  JOIN sched_slice AS s ON s.utid = t2.utid AND s.ts >= w2.ts
  WHERE w2.name = 'sched_waking'
  GROUP BY w2.id
) AS next_slice ON next_slice.waked_id = w.id
WHERE w.name = 'sched_waking'
GROUP BY process.pid, t_woken.tid, COALESCE(t_woken.name, a_comm.string_value)
ORDER BY count DESC
"""

# Query 5 — per-task runtime samples from raw ftrace
#
# sched_stat_runtime on this kernel carries exactly comm/pid/runtime args
# (runtime is in int_value; there is NO vruntime field — see module docstring).
QUERY_SCHED_RUNTIME = """
SELECT
  r.ts,
  r.cpu,
  a_pid.int_value AS pid,
  t.tid,
  COALESCE(t.name, a_comm.string_value) AS thread_name,
  a_runtime.int_value AS runtime_ns
FROM ftrace_event AS r
JOIN args AS a_comm ON r.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON r.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_runtime ON r.arg_set_id = a_runtime.arg_set_id AND a_runtime.key = 'runtime'
LEFT JOIN thread AS t ON t.tid = a_pid.int_value
WHERE r.name = 'sched_stat_runtime'
ORDER BY r.ts
"""

# Query 6 — task migrations from raw ftrace
#
# sched_migrate_task carries comm/pid/prio/orig_cpu/dest_cpu args; the pid arg
# is the migrated task's tid, mapped back through thread.tid like the other
# scheduler queries.
QUERY_SCHED_MIGRATIONS = """
SELECT
  m.ts,
  m.cpu,
  a_pid.int_value AS pid,
  t.tid,
  COALESCE(t.name, a_comm.string_value) AS thread_name,
  a_dest.int_value AS dest_cpu
FROM ftrace_event AS m
JOIN args AS a_comm ON m.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON m.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_dest ON m.arg_set_id = a_dest.arg_set_id AND a_dest.key = 'dest_cpu'
LEFT JOIN thread AS t ON t.tid = a_pid.int_value
WHERE m.name = 'sched_migrate_task'
ORDER BY m.ts
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
    "perfetto-sched-runtime.csv": [
        "ts",
        "cpu",
        "pid",
        "tid",
        "thread_name",
        "runtime_ns",
    ],
    "perfetto-sched-migrations.csv": [
        "ts",
        "cpu",
        "pid",
        "tid",
        "thread_name",
        "dest_cpu",
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

    # TraceProcessorConfig is part of the perfetto 0.57.x API.  Some
    # environments (e.g. minimal test doubles) expose only TraceProcessor;
    # fall back to the default config there so analysis still runs.
    try:
        from perfetto.trace_processor import TraceProcessorConfig
    except ImportError:
        TraceProcessorConfig = None

    log(f"Loading trace: {trace_path}")
    # Raw ftrace ingestion is required for sched_waking / sched_stat_runtime
    # to be queryable via ftrace_event; the default config drops them.
    if TraceProcessorConfig is None:
        log(
            "  TraceProcessorConfig unavailable — using default config "
            "(raw ftrace not ingested; scheduler ftrace events invisible)",
            level="warn",
        )
        tp = TraceProcessor(file_path=trace_path)
    else:
        tp = TraceProcessor(
            file_path=trace_path,
            config=TraceProcessorConfig(ingest_ftrace_in_raw=True),
        )

    with tp:
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
    
        # Query 4 — scheduling wakeup latency (real, from raw ftrace)
        try:
            result = tp.query("""
SELECT
  process.pid,
  t_woken.tid,
  COALESCE(t_woken.name, a_comm.string_value) AS thread_name,
  AVG((next_slice.ts - w.ts) / 1000000.0) AS wakeup_latency_ms,
  COUNT(*) AS count
FROM ftrace_event AS w
JOIN args AS a_comm ON w.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON w.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_prio ON w.arg_set_id = a_prio.arg_set_id AND a_prio.key = 'prio'
JOIN thread AS t_woken ON t_woken.tid = a_pid.int_value
JOIN process ON t_woken.upid = process.upid
JOIN (
  SELECT
    w2.id AS waked_id,
    MIN(s.ts) AS ts
  FROM ftrace_event AS w2
  JOIN args AS a_pid2 ON w2.arg_set_id = a_pid2.arg_set_id AND a_pid2.key = 'pid'
  JOIN thread AS t2 ON t2.tid = a_pid2.int_value
  JOIN sched_slice AS s ON s.utid = t2.utid AND s.ts >= w2.ts
  WHERE w2.name = 'sched_waking'
  GROUP BY w2.id
) AS next_slice ON next_slice.waked_id = w.id
WHERE w.name = 'sched_waking'
GROUP BY process.pid, t_woken.tid, COALESCE(t_woken.name, a_comm.string_value)
ORDER BY count DESC
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
    
        # Query 5 — per-task runtime samples from raw ftrace
        try:
            result = tp.query("""
SELECT
  r.ts,
  r.cpu,
  a_pid.int_value AS pid,
  t.tid,
  COALESCE(t.name, a_comm.string_value) AS thread_name,
  a_runtime.int_value AS runtime_ns
FROM ftrace_event AS r
JOIN args AS a_comm ON r.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON r.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_runtime ON r.arg_set_id = a_runtime.arg_set_id AND a_runtime.key = 'runtime'
LEFT JOIN thread AS t ON t.tid = a_pid.int_value
WHERE r.name = 'sched_stat_runtime'
ORDER BY r.ts
""")
            _save_or_empty(
                os.path.join(output_dir, "perfetto-sched-runtime.csv"),
                "perfetto-sched-runtime.csv",
                result,
            )
        except Exception as exc:
            log(f"  perfetto-sched-runtime.csv — query failed: {exc}", level="warn")
            _write_empty_csv(
                os.path.join(output_dir, "perfetto-sched-runtime.csv"),
                EMPTY_HEADERS["perfetto-sched-runtime.csv"],
            )
    
        # Query 6 — task migrations from raw ftrace
        try:
            result = tp.query("""
SELECT
  m.ts,
  m.cpu,
  a_pid.int_value AS pid,
  t.tid,
  COALESCE(t.name, a_comm.string_value) AS thread_name,
  a_dest.int_value AS dest_cpu
FROM ftrace_event AS m
JOIN args AS a_comm ON m.arg_set_id = a_comm.arg_set_id AND a_comm.key = 'comm'
JOIN args AS a_pid ON m.arg_set_id = a_pid.arg_set_id AND a_pid.key = 'pid'
JOIN args AS a_dest ON m.arg_set_id = a_dest.arg_set_id AND a_dest.key = 'dest_cpu'
LEFT JOIN thread AS t ON t.tid = a_pid.int_value
WHERE m.name = 'sched_migrate_task'
ORDER BY m.ts
""")
            _save_or_empty(
                os.path.join(output_dir, "perfetto-sched-migrations.csv"),
                "perfetto-sched-migrations.csv",
                result,
            )
        except Exception as exc:
            log(f"  perfetto-sched-migrations.csv — query failed: {exc}", level="warn")
            _write_empty_csv(
                os.path.join(output_dir, "perfetto-sched-migrations.csv"),
                EMPTY_HEADERS["perfetto-sched-migrations.csv"],
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
