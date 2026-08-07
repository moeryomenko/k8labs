#!/usr/bin/env python3
"""dist-analyze.py — slice-level CPU execution-time distribution extraction.

Consumes staged experiment data (no cluster, no network at analysis time) for
the EEVDF CPU execution-time distribution suite and emits per-cell slice,
runtime, summary and percentile artifacts.  The pinned analysis contract
covers the slice extraction pipeline + output schemas, the pid->pod mapping
exclusively from eevdf-<pod>-pids.csv, a data integrity sanity gate,
deterministic byte-identical reruns, a trace coverage quality gate and the
output layout.

Input layout (runner convention, per cell):
    <data-dir>/<cell>/replicate-<N>/*.perfetto-trace        (glob, e.g.
                           eevdf-deep-<ts>.perfetto-trace)
    <data-dir>/<cell>/replicate-<N>/eevdf-<pod>-pids.csv
    <data-dir>/<cell>/replicate-<N>/cgroup*.csv             (cgroup-<pod>.csv
                           for co-located cells; bare cgroup.csv with a pod
                           column for single-pod cells)
    <data-dir>/<cell>/replicate-<N>/metadata.json

Output layout:
    <output-dir>/distribution/<family>/<cell>/
        dist-slices.csv        ts_start_us,ts_end_us,duration_us,cpu,tid,
                               thread_name,pod
        dist-runtime.csv       ts,cpu,pid,tid,thread_name,pod,runtime_ns
        dist-summary.csv       cell,replicate,pod,slice_count,total_exec_ms,
                               mean_us,median_us,p50_us,p95_us,p99_us,max_us,
                               throttle_ratio,cpu_weight,cpu_max,quality
        dist-percentiles.json  {replicate: {pod: {p<k>: value}}}

Usage:
    dist-analyze.py --data-dir <dir> --output-dir <dir> --family <name>
                    [--workload stress-ng|cpu-burner|api-server|db-simulator]
                    [--duration 90] [--chunk-s <seconds>]

By default the guard window is processed in one pass (the byte-identity
reference).  Passing --chunk-s splits the guard window into time-chunked
windows of that width (streaming extraction, memory pass): each chunk is queried,
accumulated per replicate and freed, so outputs are byte-identical to the
one-pass run while the Python side stays memory-bounded.

The sanity gate runs after all cells are processed: violated facts are
printed to stderr and the process exits non-zero.  A degraded cell is a
quality flag, not a gate failure.

Known limitation (from perfetto-analyze.py): the Fedora 44 kernel's
``sched_stat_runtime`` tracepoint carries no ``vruntime`` field, so no
vruntime extraction is attempted from traces.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Pinned constants (pinned contract section 4.1)
# ---------------------------------------------------------------------------

SLICES_COLUMNS = [
    "ts_start_us",
    "ts_end_us",
    "duration_us",
    "cpu",
    "tid",
    "thread_name",
    "pod",
]
# The trace-layer query returns the SOURCE columns; ``pod`` is added later by
# assign_pods().
SLICES_SOURCE_COLUMNS = SLICES_COLUMNS[:6]

RUNTIME_COLUMNS = [
    "ts",
    "cpu",
    "pid",
    "tid",
    "thread_name",
    "pod",
    "runtime_ns",
]
# Note: cannot reuse RUNTIME_COLUMNS[:6] — ``pod`` sits at index 5 there;
# the source columns replace it with ``runtime_ns``.
RUNTIME_SOURCE_COLUMNS = ["ts", "cpu", "pid", "tid", "thread_name", "runtime_ns"]

SUMMARY_COLUMNS = [
    "cell",
    "replicate",
    "pod",
    "slice_count",
    "total_exec_ms",
    "mean_us",
    "median_us",
    "p50_us",
    "p95_us",
    "p99_us",
    "max_us",
    "throttle_ratio",
    "cpu_weight",
    "cpu_max",
    "quality",
]

# 1..99 covered at 1-decile steps (pinned interpretation).
PERCENTILE_STEPS = (1, 11, 21, 31, 41, 51, 61, 71, 81, 91, 99)

COVERAGE_THRESHOLD = 0.80
SYSTEM_POD = "system"
GUARD_S = 2.0
DIST_DEMAND_MILLICORES = 2000  # saturating demand on the 2-vCPU w1 worker

# ---------------------------------------------------------------------------
# SQL queries (constant, importable)
# ---------------------------------------------------------------------------

# Trace bounds: first/last sched event timestamps, in nanoseconds.
QUERY_BOUNDS = """
SELECT MIN(ts) AS first_ts_ns, MAX(ts) AS last_ts_ns
FROM sched_slice
"""

# Per-slice rows from sched_slice joined to thread, restricted to the guard
# window.  ts/dur are emitted in microseconds; the window is passed in
# microseconds and converted to nanoseconds before formatting, so the WHERE
# clause compares raw nanosecond timestamps (``{start_ns}``/``{end_ns}``).
QUERY_SLICES = """
SELECT
  s.ts / 1000.0 AS ts_start_us,
  (s.ts + s.dur) / 1000.0 AS ts_end_us,
  s.dur / 1000.0 AS duration_us,
  s.cpu,
  t.tid,
  COALESCE(t.name, '') AS thread_name
FROM sched_slice AS s
LEFT JOIN thread AS t ON t.utid = s.utid
WHERE s.ts >= {start_ns} AND s.ts < {end_ns} AND s.dur >= 0
ORDER BY s.ts, t.tid
"""

# Per-task runtime samples from raw ftrace (same ingestion pattern as
# perfetto-analyze.py::QUERY_SCHED_RUNTIME).  sched_stat_runtime on this
# kernel carries comm/pid/runtime args only — no vruntime field.
QUERY_RUNTIME = """
SELECT
  r.ts / 1000.0 AS ts,
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
WHERE r.name = 'sched_stat_runtime' AND r.ts >= {start_ns} AND r.ts < {end_ns}
ORDER BY r.ts
"""

# ---------------------------------------------------------------------------
# pid->pod mapping from eevdf-<pod>-pids.csv
# ---------------------------------------------------------------------------


def load_pid_map(cell_dir: Path) -> dict[int, str]:
    """Build the pid->pod map EXCLUSIVELY from the eevdf-*-pids.csv files.

    Files are globbed in sorted order and later entries overwrite earlier
    ones (deterministic).  Only the ``pod`` and ``pid`` columns are read;
    a replicate dir with no eevdf files yields an empty map.
    """
    pid_map: dict[int, str] = {}
    for pids_path in sorted(cell_dir.glob("eevdf-*-pids.csv")):
        df = pd.read_csv(pids_path, usecols=["pod", "pid"])
        for pid, pod in zip(df["pid"], df["pod"]):
            if pd.notna(pod):
                pid_map[int(pid)] = str(pod)
    return pid_map


def assign_pods(slices_df: pd.DataFrame, pid_map: dict[int, str]) -> pd.DataFrame:
    """Add a ``pod`` column to sched_slice rows: tid->pod, unmapped -> system.

    Returns exactly ``SLICES_COLUMNS`` in order.
    """
    out = slices_df.copy()
    out["pod"] = out["tid"].map(pid_map).fillna(SYSTEM_POD)
    return out[SLICES_COLUMNS]


def assign_runtime_pods(
    runtime_df: pd.DataFrame, pid_map: dict[int, str]
) -> pd.DataFrame:
    """Add a ``pod`` column to sched_stat_runtime rows (same rule as assign_pods).

    Returns exactly ``RUNTIME_COLUMNS`` in order.
    """
    out = runtime_df.copy()
    out["pod"] = out["tid"].map(pid_map).fillna(SYSTEM_POD)
    return out[RUNTIME_COLUMNS]


# ---------------------------------------------------------------------------
# Summary statistics and percentile table
# ---------------------------------------------------------------------------


def compute_stats(durations_us) -> dict[str, float]:
    """Compute mean/median/p50/p95/p99/max over slice durations (us).

    Uses pandas linear interpolation for the quantiles.  Empty input yields
    an all-zero table.
    """
    if not durations_us:
        return {
            "mean_us": 0.0,
            "median_us": 0.0,
            "p50_us": 0.0,
            "p95_us": 0.0,
            "p99_us": 0.0,
            "max_us": 0.0,
        }
    series = pd.Series(list(durations_us), dtype="float64")
    return {
        "mean_us": float(series.mean()),
        "median_us": float(series.median()),
        "p50_us": float(series.quantile(0.50)),
        "p95_us": float(series.quantile(0.95)),
        "p99_us": float(series.quantile(0.99)),
        "max_us": float(series.max()),
    }


def compute_percentiles(durations_us) -> dict[str, float]:
    """Compute the full percentile table p<k> for k in PERCENTILE_STEPS.

    Empty input yields an all-zero table with the pinned key set.
    """
    if not durations_us:
        return {f"p{k}": 0.0 for k in PERCENTILE_STEPS}
    series = pd.Series(list(durations_us), dtype="float64")
    return {f"p{k}": float(series.quantile(k / 100.0)) for k in PERCENTILE_STEPS}


def compute_throttle_ratio(cgroup_csv: Path) -> float:
    """Read throttle_ratio = nr_throttled / nr_periods from the LAST cgroup sample.

    A missing file, empty CSV, or nr_periods == 0 yields 0.0.
    """
    if not cgroup_csv.is_file():
        return 0.0
    df = pd.read_csv(cgroup_csv)
    if df.empty:
        return 0.0
    last = df.iloc[-1]
    nr_periods = int(last["nr_periods"])
    nr_throttled = int(last["nr_throttled"])
    if nr_periods == 0:
        return 0.0
    return nr_throttled / nr_periods


def compute_cpu_limits(cgroup_csv: Path) -> tuple[int, int]:
    """Read (cpu_weight, cpu_max_quota) from the LAST cgroup sample.

    A missing file or empty CSV yields (0, 0).  An unlimited quota — the
    cgroup v2 ``cpu.max`` literal ``max`` — maps to 0, the pinned "no
    quota" sentinel (same as system rows in dist-summary.csv).
    """
    if not cgroup_csv.is_file():
        return (0, 0)
    df = pd.read_csv(cgroup_csv)
    if df.empty:
        return (0, 0)
    last = df.iloc[-1]
    quota = last["cpu_max_quota"]
    if str(quota).strip() == "max":
        quota = 0
    return (int(last["cpu_weight"]), int(quota))


# ---------------------------------------------------------------------------
# Trace coverage quality gate
# ---------------------------------------------------------------------------


def compute_coverage(first_ts_us, last_ts_us, duration_s) -> float:
    """Retained coverage = (last - first retained event ts) / duration.

    A reversed span (or zero span) yields 0.0; the value is NOT clamped at
    1.0 (capture may start before the measurement window).
    """
    if last_ts_us < first_ts_us:
        return 0.0
    total_us = duration_s * 1_000_000
    if total_us == 0:
        return 0.0
    return (last_ts_us - first_ts_us) / total_us


def quality_for(coverage: float) -> str:
    """Map retained coverage to ``good`` (>= 0.80) or ``degraded``."""
    return "good" if coverage >= COVERAGE_THRESHOLD else "degraded"


def retained_window(
    first_ts_ns, last_ts_ns, guard_s: float = GUARD_S
) -> tuple[int, int]:
    """Compute the guard-window [start, end) in microseconds.

    Drops ``guard_s`` seconds from each end of the trace.  A trace shorter
    than 2*guard yields an empty window (start == end, matches nothing).
    """
    guard_us = int(guard_s * 1_000_000)
    start_us = first_ts_ns // 1000 + guard_us
    end_us = last_ts_ns // 1000 - guard_us
    if start_us >= end_us:
        return (start_us, start_us)
    return (start_us, end_us)


# ---------------------------------------------------------------------------
# Chunked guard-window processing (streaming extraction)
# ---------------------------------------------------------------------------


def chunk_windows(
    start_us: int, end_us: int, chunk_s: float = 5.0
) -> list[tuple[int, int]]:
    """Partition the guard window [start_us, end_us) into chunk_s-wide windows.

    Returns consecutive half-open windows, each ``chunk_s`` seconds wide
    except the last (which ends at ``end_us`` and may be shorter).  Windows
    begin at ``start_us``, are contiguous (``w[i][1] == w[i+1][0]``) and cover
    the window exactly with no gaps or overlaps.  An empty window
    (start >= end) yields an empty list; a chunk wider than the window yields
    the single window (start_us, end_us).  Raises ValueError when ``chunk_s``
    is non-positive or too small to form a whole-microsecond window.
    """
    if chunk_s <= 0:
        raise ValueError(f"chunk_s must be positive, got {chunk_s}")
    chunk_us = int(chunk_s * 1_000_000)
    if chunk_us <= 0:
        raise ValueError(f"chunk_s too small for a microsecond window: {chunk_s}")
    if start_us >= end_us:
        return []
    windows: list[tuple[int, int]] = []
    current = start_us
    while current < end_us:
        nxt = min(current + chunk_us, end_us)
        windows.append((current, nxt))
        current = nxt
    return windows


def merge_per_replicate_slices(cell_out: Path) -> pd.DataFrame:
    """Regenerate the merged slice rows from the per-replicate files.

    Reads every ``dist-slices-replicate-<n>.csv`` in *cell_out* (globbed with
    ``sorted()`` — the same deterministic replicate order the analyzer
    concatenates in), concatenates with ``ignore_index=True`` and sorts by
    ``["ts_start_us", "tid"]`` with ``kind="mergesort"`` — the pinned key and
    stable kind used for dist-slices.csv (pinned merge semantics).  Returns
    the merged DataFrame (string columns restored to object
    dtype — the pinned schema) for the main flow to write to dist-slices.csv.
    A cell with no per-replicate files returns an empty DataFrame with the
    pinned ``SLICES_COLUMNS`` schema (matches today's empty-cell output).

    Memory note: the per-replicate files are read with the string
    columns as pandas ``category`` so re-reading the compact CSVs does not
    materialize one Python string object per row (the raw trace-derived
    DataFrames are freed before this runs); the returned frame carries the
    pinned object dtypes and serializes byte-identically.

    The MAIN FLOW no longer calls this helper — it uses
    ``write_merged_windowed``, the memory-bounded chunked ts-window merge
    (byte-identical output, O(chunk) peak RSS).  This function stays as the
    pinned reference API (TestMergePerReplicateSlices).
    """
    files = sorted(cell_out.glob("dist-slices-replicate-*.csv"))
    if not files:
        return pd.DataFrame(columns=SLICES_COLUMNS)
    frames = [
        pd.read_csv(path, dtype={"thread_name": "category", "pod": "category"})
        for path in files
    ]
    merged = pd.concat(frames, ignore_index=True).sort_values(
        ["ts_start_us", "tid"], kind="mergesort"
    )
    del frames
    gc.collect()
    return merged.astype({"thread_name": "object", "pod": "object"})


# ---------------------------------------------------------------------------
# Memory-bounded merged-file regeneration (chunked ts-window merge)
# ---------------------------------------------------------------------------

# Width (seconds) of the ts-windows the chunked merge iterates.  Any positive
# width produces byte-identical output (a ts value falls entirely in one
# half-open window); this only bounds the per-window peak RSS.
MERGE_CHUNK_S = 5.0

# Rows read per CSV chunk from a per-replicate file.  This bounds each
# reader's in-memory buffer regardless of file size.
_MERGE_READ_CHUNK = 250_000

# String columns are read as category so the re-read stays compact
# (to_csv serializes categorical values identically to object).
_MERGE_STRING_DTYPE = {"thread_name": "category", "pod": "category"}


class _WindowReader:
    """Chunked reader over a per-replicate CSV sorted ascending by *ts_col*.

    Serves half-open ts windows: ``take(lo, hi)`` returns the rows with
    ``lo <= ts < hi``, advancing through the file in bounded chunks and
    keeping the tail (rows >= hi) for the next window.  The whole file is
    read exactly once across all windows; peak memory is one chunk per
    reader.
    """

    def __init__(self, path: Path, ts_col: str, chunksize: int = _MERGE_READ_CHUNK):
        self._ts_col = ts_col
        self._chunks = pd.read_csv(path, dtype=_MERGE_STRING_DTYPE, chunksize=chunksize)
        self._buf: pd.DataFrame | None = None
        self._exhausted = False

    def _load(self) -> None:
        if self._exhausted:
            return
        try:
            self._buf = next(self._chunks)
        except StopIteration:
            self._buf = None
            self._exhausted = True

    def peek_min(self):
        """First (minimum) ts value still available, or None when empty."""
        if self._buf is None:
            self._load()
        if self._buf is None or len(self._buf) == 0:
            return None
        return self._buf[self._ts_col].iloc[0]

    def has_rows(self) -> bool:
        """True when the file still has rows for a future window."""
        if self._buf is not None and len(self._buf) > 0:
            return True
        if not self._exhausted:
            return True
        return False

    def take(self, lo, hi):
        """Rows with ``lo <= ts < hi``, or None when the window has none.

        The file must be sorted ascending by ts.  Rows below the window are
        consumed silently (they belong to earlier windows); rows at/above
        ``hi`` stay buffered for a later window.
        """
        parts: list[pd.DataFrame] = []
        while True:
            if self._buf is None:
                if self._exhausted:
                    break
                self._load()
                if self._buf is None:
                    break
            ts = self._buf[self._ts_col]
            if ts.iloc[0] >= hi:
                break  # entire buffer at/after the window: keep for later
            lo_idx = int(ts.searchsorted(lo, side="left"))
            hi_idx = int(ts.searchsorted(hi, side="left"))
            if lo_idx < hi_idx:
                parts.append(self._buf.iloc[lo_idx:hi_idx])
            # Keep the tail (rows >= hi) for the next window.
            self._buf = self._buf.iloc[hi_idx:].reset_index(drop=True)
            if len(self._buf) == 0:
                self._buf = None
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return pd.concat(parts, ignore_index=True)


def write_merged_windowed(
    files: list[Path],
    out_csv: Path,
    ts_col: str,
    sort_cols: list[str],
    columns: list[str],
    chunk_s: float = MERGE_CHUNK_S,
) -> None:
    """Write the merged CSV with a memory-bounded chunked ts-window merge.

    Each per-replicate file must be sorted ascending by *ts_col*.  The ts
    range is covered by contiguous half-open windows; per window, every
    file's rows in ``[lo, hi)`` are read (skip-ahead + take), concat'd in
    sorted filename order and ``sort_values(*sort_cols*, kind="mergesort")``
    — the pinned key/kind of the reference full merge.  Because a ts value
    falls entirely in one window and the concat order is unchanged, the
    window concatenation is byte-identical to the reference concat + global
    mergesort (byte-identical reruns).

    Peak RSS is O(chunk): each reader holds at most ``_MERGE_READ_CHUNK``
    rows, plus one window's rows across all files.

    A cell with no rows in any file yields a header-only CSV (the pinned
    empty-schema output).

    The target is unlinked up front so a re-run over an existing
    output tree regenerates the merged file from scratch instead of
    appending a second copy (with an embedded header) to the previous
    run's output — the window writes below use ``mode="a"`` and must never
    see stale bytes (byte-identical reruns).
    """
    out_csv.unlink(missing_ok=True)
    readers = [_WindowReader(path, ts_col) for path in files]
    try:
        mins = [r.peek_min() for r in readers]
        mins = [m for m in mins if m is not None]
        if not mins:
            pd.DataFrame(columns=columns).to_csv(out_csv, index=False)
            return
        lo = min(mins)
        chunk_us = float(chunk_s) * 1_000_000
        wrote_any = False
        while any(r.has_rows() for r in readers):
            hi = lo + chunk_us
            parts = []
            for r in readers:
                part = r.take(lo, hi)
                if part is not None and len(part) > 0:
                    parts.append(part)
            if parts:
                merged = pd.concat(parts, ignore_index=True).sort_values(
                    sort_cols, kind="mergesort"
                )
                merged.to_csv(out_csv, index=False, mode="a", header=not wrote_any)
                wrote_any = True
                del merged, parts
                gc.collect()
            lo = hi
        if not wrote_any:
            pd.DataFrame(columns=columns).to_csv(out_csv, index=False)
    finally:
        del readers
        gc.collect()


# ---------------------------------------------------------------------------
# Data integrity sanity gate
# ---------------------------------------------------------------------------

_REQUEST_RE = re.compile(r"request=([^-\s]*)")
_LIMIT_RE = re.compile(r"limit=([^-\s]*)")


def parse_request_limit(cell: str) -> tuple[int | None, int | None]:
    """Parse the FIRST request=/limit= pair from a cell label into millicores.

    Values ``""`` / ``none`` map to None; the ``m`` suffix is stripped.
    Returns (None, None) when no pair is present.
    """

    def _parse(match: re.Match[str] | None) -> int | None:
        if match is None:
            return None
        value = match.group(1).strip().lower()
        if value in ("", "none"):
            return None
        value = value.rstrip("m")
        if not value:
            return None
        return int(value)

    return (_parse(_REQUEST_RE.search(cell)), _parse(_LIMIT_RE.search(cell)))


def sanity_check(
    summary_df: pd.DataFrame, workload_by_cell: dict[str, str | None]
) -> list[str]:
    """Return the violated facts; empty list means the gate passes.

    Rules:
      - stress-ng saturating: limit < demand (2000m) -> expect ratio >= 0.95;
        limit >= demand -> expect ratio < 0.05.
      - cpu-burner light: limit >= 300m -> expect ratio < 0.05.
      - api-server/db-simulator: no throttle facts.
      - Labels with multiple request/limit pairs skip the throttle rule
        (ambiguous) but keep monotonicity.
      - Every row: mean/median/p50 <= p95 <= p99.
    """
    violations: list[str] = []
    for row in summary_df.itertuples(index=False):
        cell = row.cell
        ratio = float(row.throttle_ratio)
        cpu_max = float(row.cpu_max)
        mean_us = float(row.mean_us)
        median_us = float(row.median_us)
        p50_us = float(row.p50_us)
        p95_us = float(row.p95_us)
        p99_us = float(row.p99_us)

        if not (
            mean_us <= p95_us <= p99_us and median_us <= p95_us and p50_us <= p95_us
        ):
            violations.append(
                f"cell {cell}: monotonicity violated: mean/median/p50 <= p95 <= p99"
            )

        workload = workload_by_cell.get(cell)
        if workload not in ("stress-ng", "cpu-burner"):
            continue
        if cell.count("request=") > 1:
            continue
        if cpu_max == 0:
            # Unlimited quota (cgroup cpu.max = "max"): no quota exists, so no
            # quota-based throttle expectation applies.
            continue
        _request, limit = parse_request_limit(cell)
        if limit is None:
            continue

        if workload == "stress-ng":
            if limit < DIST_DEMAND_MILLICORES:
                if ratio < 0.95:
                    violations.append(
                        f"cell {cell}: stress-ng saturating, limit < demand: "
                        f"throttle_ratio={ratio:.3f} < 0.95"
                    )
            else:
                if ratio >= 0.05:
                    violations.append(
                        f"cell {cell}: stress-ng saturating, limit >= demand: "
                        f"throttle_ratio={ratio:.3f} >= 0.05"
                    )
        elif workload == "cpu-burner":
            if limit >= 300 and ratio >= 0.05:
                violations.append(
                    f"cell {cell}: cpu-burner light: throttle_ratio={ratio:.3f} >= 0.05"
                )
    return violations


# ---------------------------------------------------------------------------
# Thin trace layer (lazy perfetto import)
# ---------------------------------------------------------------------------


def load_trace(trace_path: Path):
    """Open a Perfetto trace with raw ftrace ingestion enabled.

    The perfetto import is lazy so the module stays importable without the
    package; a missing dependency or corrupt trace raises (the analyzer prints
    the message and exits non-zero — never silently empty output).
    """
    from perfetto.trace_processor import TraceProcessor

    try:
        from perfetto.trace_processor import TraceProcessorConfig
    except ImportError:
        TraceProcessorConfig = None

    if TraceProcessorConfig is None:
        return TraceProcessor(file_path=str(trace_path))
    return TraceProcessor(
        file_path=str(trace_path),
        config=TraceProcessorConfig(ingest_ftrace_in_raw=True),
    )


def trace_event_bounds(tp) -> tuple[int, int]:
    """Return (first_ts_ns, last_ts_ns) over sched_slice."""
    result = tp.query(QUERY_BOUNDS)
    df = result.as_pandas_dataframe()
    if df.empty:
        return (0, 0)
    row = df.iloc[0]
    return (int(row["first_ts_ns"]), int(row["last_ts_ns"]))


def query_slices(tp, window_start_us: int, window_end_us: int) -> pd.DataFrame:
    """Extract per-slice rows from sched_slice for the guard window.

    Returns the pinned SOURCE columns (no ``pod``); empty results still carry
    the columns (normalization).
    """
    result = tp.query(
        QUERY_SLICES.format(
            start_ns=window_start_us * 1000, end_ns=window_end_us * 1000
        )
    )
    df = result.as_pandas_dataframe()
    if df.empty:
        return pd.DataFrame(columns=SLICES_SOURCE_COLUMNS)
    return df[SLICES_SOURCE_COLUMNS]


def query_runtime(tp, window_start_us: int, window_end_us: int) -> pd.DataFrame:
    """Extract sched_stat_runtime samples for the guard window.

    Returns the pinned SOURCE columns (no ``pod``); empty results still carry
    the columns (normalization).
    """
    result = tp.query(
        QUERY_RUNTIME.format(
            start_ns=window_start_us * 1000, end_ns=window_end_us * 1000
        )
    )
    df = result.as_pandas_dataframe()
    if df.empty:
        return pd.DataFrame(columns=RUNTIME_SOURCE_COLUMNS)
    return df[RUNTIME_SOURCE_COLUMNS]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Extract per-slice CPU execution-time distribution from staged "
            "experiment data."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, metavar="DIR", help="Run data dir with cell dirs"
    )
    parser.add_argument(
        "--output-dir", required=True, metavar="DIR", help="Output root dir"
    )
    parser.add_argument(
        "--family", required=True, metavar="NAME", help="Family name (output subdir)"
    )
    parser.add_argument(
        "--workload",
        choices=("stress-ng", "cpu-burner", "api-server", "db-simulator"),
        default=None,
        help="Family-level workload type (drives the sanity gate)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=90.0,
        help="Measurement duration in seconds (default: 90)",
    )
    parser.add_argument(
        "--chunk-s",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help=(
            "Process the guard window in time-chunked windows of this width "
            "in seconds (default: 5; outputs are byte-identical to one pass)"
        ),
    )
    return parser


def _replicate_number(rep_dir: Path) -> int:
    """Read the replicate number from metadata.json, falling back to the dir name."""
    meta = rep_dir / "metadata.json"
    if meta.is_file():
        try:
            return int(json.loads(meta.read_text())["replicate"])
        except (ValueError, KeyError, OSError):
            pass
    name = rep_dir.name
    if name.startswith("replicate-"):
        try:
            return int(name[len("replicate-") :])
        except ValueError:
            pass
    return 0


def _discover_cgroup_csvs(rep_dir: Path) -> dict[str, Path]:
    """Map pod -> cgroup CSV from a replicate dir (runner's actual naming).

    Co-located (N-pod) cells write ``cgroup-<pod>.csv`` per pod; single-pod
    cells write a bare ``cgroup.csv`` carrying the pod identity in its ``pod``
    column.  The ``cgroup*.csv`` glob naturally excludes the runner's
    ``*.warnings`` sidecar files (they end in ``.warnings``, not ``.csv``) and
    the ``cgroup-hierarchy-<node>.json`` snapshots.
    """
    by_pod: dict[str, Path] = {}
    for path in sorted(rep_dir.glob("cgroup*.csv")):
        name = path.name
        if name.startswith("cgroup-") and name.endswith(".csv"):
            by_pod[name[len("cgroup-") : -len(".csv")]] = path
            continue
        try:
            pods = pd.read_csv(path, usecols=["pod"])["pod"]
        except Exception:
            continue
        for pod in pods.dropna().astype(str).unique():
            by_pod.setdefault(pod, path)
    return by_pod


def main(argv: list[str] | None = None) -> int:
    """Process every cell under --data-dir and emit the pinned outputs.

    Returns non-zero when the sanity gate reports violated facts
    (each printed to stderr), or when a trace cannot be loaded.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # The reference run (no --chunk-s)
    # processes the full guard window in one pass; passing --chunk-s switches
    # the replicate loop to time-chunked windows.  The flag's argparse default
    # is 5.0, so presence is detected on the raw argv.
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    chunked = any(arg.split("=", 1)[0] == "--chunk-s" for arg in raw_argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        parser.error(f"data dir missing: {data_dir}")

    out_root = Path(args.output_dir) / "distribution" / args.family
    workload_by_cell: dict[str, str | None] = {}

    all_summary: list[pd.DataFrame] = []
    violations: list[str] = []

    # Scratch dir for the per-replicate runtime rows (outside the
    # output tree — the pinned per-cell file set is exact).  Cleaned up even
    # on early return / trace-load failure.
    runtime_scratch = tempfile.TemporaryDirectory(prefix="dist-analyze-runtime-")
    runtime_tmp = Path(runtime_scratch.name)
    try:
        for cell_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
            cell = cell_dir.name
            workload_by_cell[cell] = args.workload
            cell_out = out_root / cell
            cell_out.mkdir(parents=True, exist_ok=True)

            # Per-replicate runtime rows are staged in a scratch dir
            # OUTSIDE the output tree (the pinned per-cell file set is exact) and
            # merged into dist-runtime.csv after all replicates are freed.
            cell_runtime_tmp = runtime_tmp / cell
            cell_runtime_tmp.mkdir(parents=True, exist_ok=True)

            percentiles: dict[str, dict[str, dict[str, float]]] = {}
            summary_rows: list[tuple] = []

            for rep_dir in sorted(p for p in cell_dir.iterdir() if p.is_dir()):
                replicate = _replicate_number(rep_dir)
                pid_map = load_pid_map(rep_dir)

                trace_matches = sorted(rep_dir.glob("*.perfetto-trace"))
                if not trace_matches:
                    print(
                        f"error: no perfetto trace (*.perfetto-trace) found in {rep_dir}",
                        file=sys.stderr,
                    )
                    return 1
                trace_path = trace_matches[0]
                try:
                    tp = load_trace(trace_path)
                except Exception as exc:
                    print(
                        f"error: failed to load trace {trace_path}: {exc}",
                        file=sys.stderr,
                    )
                    return 1

                first_ns, last_ns = trace_event_bounds(tp)
                start_us, end_us = retained_window(first_ns, last_ns)

                # Process the guard window in time-chunked
                # windows when --chunk-s is given; without the flag the full
                # window is queried in one pass (the byte-identity reference).
                if chunked:
                    windows = chunk_windows(start_us, end_us, args.chunk_s)
                else:
                    windows = [(start_us, end_us)]

                # Accumulate each chunk's rows; the concat equals the one-shot
                # window query exactly (no chunk duplicates or drops rows), so the
                # per-replicate result is identical regardless of chunking.
                chunk_slices: list[pd.DataFrame] = []
                chunk_runtime: list[pd.DataFrame] = []
                for c_start_us, c_end_us in windows:
                    chunk_slices.append(query_slices(tp, c_start_us, c_end_us))
                    chunk_runtime.append(query_runtime(tp, c_start_us, c_end_us))
                if chunk_slices:
                    slices_src = pd.concat(chunk_slices, ignore_index=True)
                else:
                    slices_src = pd.DataFrame(columns=SLICES_SOURCE_COLUMNS)
                if chunk_runtime:
                    runtime_src = pd.concat(chunk_runtime, ignore_index=True)
                else:
                    runtime_src = pd.DataFrame(columns=RUNTIME_SOURCE_COLUMNS)
                del chunk_slices, chunk_runtime

                slices_df = assign_pods(slices_src, pid_map)
                runtime_df = assign_runtime_pods(runtime_src, pid_map)
                del slices_src, runtime_src
                # Per-replicate slice file: same pinned
                # SLICES_COLUMNS as the merged dist-slices.csv, sorted identically
                # (ts_start_us, tid) so each file is a deterministic partition of
                # the merged union below. dist-gif.py renders one exec-timeline
                # GIF per file (revised contract).
                slices_df.sort_values(["ts_start_us", "tid"], kind="mergesort").to_csv(
                    cell_out / f"dist-slices-replicate-{replicate}.csv", index=False
                )
                # Do NOT accumulate the replicate DataFrames — the merged
                # dist-slices.csv / dist-runtime.csv are regenerated from the
                # compact per-replicate CSVs after the loop, so the raw
                # trace-derived frames are freed before the merge runs (this is
                # what bounds peak RSS: 3 x 4M-row frames never coexist).
                runtime_df.to_csv(
                    cell_runtime_tmp / f"replicate-{replicate}.csv", index=False
                )

                if slices_df.empty:
                    coverage = 0.0
                else:
                    coverage = compute_coverage(
                        float(slices_df["ts_start_us"].min()),
                        float(slices_df["ts_start_us"].max()),
                        args.duration,
                    )
                quality = quality_for(coverage)

                pods = sorted(slices_df["pod"].unique()) if not slices_df.empty else []
                cgroup_by_pod = _discover_cgroup_csvs(rep_dir)
                for pod in pods:
                    durations = slices_df.loc[
                        slices_df["pod"] == pod, "duration_us"
                    ].astype(float)
                    values = durations.tolist()
                    stats = compute_stats(values)
                    percentiles.setdefault(str(replicate), {})[pod] = (
                        compute_percentiles(values)
                    )

                    cgroup_path = cgroup_by_pod.get(pod)
                    if cgroup_path is None:
                        if pod != SYSTEM_POD:
                            print(
                                f"error: no cgroup CSV (cgroup*.csv) found for pod "
                                f"{pod} in {rep_dir}",
                                file=sys.stderr,
                            )
                            return 1
                        ratio = 0.0
                        weight, cpu_max = 0, 0
                    else:
                        ratio = compute_throttle_ratio(cgroup_path)
                        weight, cpu_max = compute_cpu_limits(cgroup_path)
                    summary_rows.append(
                        (
                            cell,
                            replicate,
                            pod,
                            len(durations),
                            float(durations.sum()) / 1000.0,
                            stats["mean_us"],
                            stats["median_us"],
                            stats["p50_us"],
                            stats["p95_us"],
                            stats["p99_us"],
                            stats["max_us"],
                            ratio,
                            weight,
                            cpu_max,
                            quality,
                        )
                    )

                # Free the replicate's DataFrames and the
                # trace layer before the next replicate (the merged files are
                # regenerated from the per-replicate CSVs, so no accumulation).
                del slices_df, runtime_df, tp
                gc.collect()

            # Regenerate the merged dist-slices.csv from the
            # per-replicate files (raw trace-derived frames were freed per
            # replicate) — same concat + mergesort semantics as the one-pass
            # path, so dist-slices.csv is byte-identical.  The
            # chunked ts-window merge (write_merged_windowed) keeps peak RSS
            # O(chunk) instead of materializing the full merged DataFrame.
            write_merged_windowed(
                sorted(cell_out.glob("dist-slices-replicate-*.csv")),
                cell_out / "dist-slices.csv",
                "ts_start_us",
                ["ts_start_us", "tid"],
                SLICES_COLUMNS,
            )
            gc.collect()

            # Same for runtime: stage the per-replicate rows in the scratch dir,
            # free the raw frames, then rebuild dist-runtime.csv from the compact
            # files (same sort by ts, mergesort — byte-identical to one pass).
            # The chunked merge needs each per-replicate file sorted by
            # ts; the raw query order is ts-ordered for a real trace processor
            # but NOT for the canned fixtures, so each file is stably sorted in
            # place first (one file at a time — bounded memory).
            runtime_files = sorted(cell_runtime_tmp.glob("replicate-*.csv"))
            if runtime_files:
                for runtime_path in runtime_files:
                    frame = pd.read_csv(runtime_path, dtype=_MERGE_STRING_DTYPE)
                    frame.sort_values("ts", kind="mergesort").to_csv(
                        runtime_path, index=False
                    )
                    del frame
                    gc.collect()
                write_merged_windowed(
                    runtime_files,
                    cell_out / "dist-runtime.csv",
                    "ts",
                    ["ts"],
                    RUNTIME_COLUMNS,
                )
            else:
                pd.DataFrame(columns=RUNTIME_COLUMNS).to_csv(
                    cell_out / "dist-runtime.csv", index=False
                )
            gc.collect()

            summary_df = pd.DataFrame(
                summary_rows, columns=SUMMARY_COLUMNS
            ).sort_values(["pod", "replicate"], kind="mergesort")
            summary_df.to_csv(cell_out / "dist-summary.csv", index=False)
            (cell_out / "dist-percentiles.json").write_text(
                json.dumps(percentiles, indent=2, sort_keys=True) + "\n"
            )

            all_summary.append(summary_df)

    finally:
        runtime_scratch.cleanup()

    # Sanity gate: violated facts exit non-zero.
    if all_summary:
        combined = pd.concat(all_summary, ignore_index=True)
        violations = sanity_check(combined, workload_by_cell)
        for violation in violations:
            print(violation, file=sys.stderr)

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
