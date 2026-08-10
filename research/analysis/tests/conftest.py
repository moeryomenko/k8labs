"""Shared test fixtures for perfetto analysis tests.

Provides mock TraceProcessor, fixture CSV data, and helper utilities
so tests can run without a real Perfetto trace file or installation.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixture CSV data — shape of each expected output CSV
# ---------------------------------------------------------------------------

THREADS_CSV = """cpu,thread_name,pid,tid,exec_time_ms,exec_time_pct
0,swapper/0,0,0,85000.0,85.0
0,stress-ng-cpu,1001,2001,10000.0,10.0
0,kworker/u2:0,0,42,5000.0,5.0
1,swapper/1,0,0,92000.0,92.0
1,stress-ng-cpu,1001,3001,5000.0,5.0
1,kworker/u2:1,0,67,3000.0,3.0
"""

CPU_UTIL_CSV = """core,utilization_pct,nr_switches
0,15.0,2500
1,8.0,1800
"""

PROCESS_SUMMARY_CSV = """pid,name,cpu_time_ms,cpu_time_pct,thread_count,nr_ctx_switches
0,swapper/idle,177000.0,88.5,2,0
1001,stress-ng-cpu,15000.0,7.5,2,4300
"""

SCHED_LATENCY_CSV = """pid,tid,thread_name,wakeup_latency_ms,count
1001,2001,stress-ng-cpu,0.15,1200
1001,3001,stress-ng-cpu,0.22,800
"""

# SQL queries the analysis script is expected to contain.
# These are keyword / structure fragments we validate exist in the query strings.
EXPECTED_QUERY_FRAGMENTS = {
    "threads": [
        "SELECT",
        "sched_slice",
        "SUM",
        "GROUP BY",
        "exec_time",
    ],
    "cpu_util": [
        "SELECT",
        "sched_slice",
        "cpu",
        "utilization",
        "GROUP BY",
    ],
    "process_summary": [
        "SELECT",
        "process",
        "thread",
        "SUM",
        "GROUP BY",
    ],
    "sched_latency": [
        "SELECT",
        "sched_waking",
        "wakeup_latency",
        "latency",
    ],
}


def make_mock_trace_processor(
    *,
    threads_df: Any = None,
    cpu_util_df: Any = None,
    process_summary_df: Any = None,
    sched_latency_df: Any = None,
    query_log: list | None = None,
) -> Any:
    """Build a mock TraceProcessor that returns canned DataFrames for queries.

    Each named query is detected by substrings expected in the SQL.
    Unmatched queries return an empty result set.
    """
    import pandas as pd

    # Default empty DataFrames with correct schema
    if threads_df is None:
        threads_df = pd.DataFrame(
            {
                "cpu": pd.Series(dtype="int64"),
                "thread_name": pd.Series(dtype="str"),
                "pid": pd.Series(dtype="int64"),
                "tid": pd.Series(dtype="int64"),
                "exec_time_ms": pd.Series(dtype="float64"),
                "exec_time_pct": pd.Series(dtype="float64"),
            }
        )
    if cpu_util_df is None:
        cpu_util_df = pd.DataFrame(
            {
                "core": pd.Series(dtype="int64"),
                "utilization_pct": pd.Series(dtype="float64"),
                "nr_switches": pd.Series(dtype="int64"),
            }
        )
    if process_summary_df is None:
        process_summary_df = pd.DataFrame(
            {
                "pid": pd.Series(dtype="int64"),
                "name": pd.Series(dtype="str"),
                "cpu_time_ms": pd.Series(dtype="float64"),
                "cpu_time_pct": pd.Series(dtype="float64"),
                "thread_count": pd.Series(dtype="int64"),
                "nr_ctx_switches": pd.Series(dtype="int64"),
            }
        )
    if sched_latency_df is None:
        sched_latency_df = pd.DataFrame(
            {
                "pid": pd.Series(dtype="int64"),
                "tid": pd.Series(dtype="int64"),
                "thread_name": pd.Series(dtype="str"),
                "wakeup_latency_ms": pd.Series(dtype="float64"),
                "count": pd.Series(dtype="int64"),
            }
        )

    queries_run: list[str] = []
    if query_log is not None:
        queries_run = query_log  # caller-managed list, mutated in-place

    class MockQueryResult:
        """Simulates the Perfetto QueryResult iterator interface."""

        def __init__(self, df: pd.DataFrame):
            self._df = df
            self._it = iter(df.itertuples(index=False))
            self._columns = list(df.columns)

        def __iter__(self) -> Iterator:
            return self

        def __next__(self):
            return next(self._it)

        def as_pandas_dataframe(self) -> pd.DataFrame:
            return self._df

    class MockTraceProcessor:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql: str) -> MockQueryResult:
            queries_run.append(sql)
            sql_lower = sql.lower()
            if (
                "sched_slice" in sql_lower
                and "group by" in sql_lower
                and "thread" in sql_lower
            ):
                return MockQueryResult(threads_df)
            elif (
                "sched_slice" in sql_lower
                and "group by" in sql_lower
                and "cpu" in sql_lower
            ):
                return MockQueryResult(cpu_util_df)
            elif "process" in sql_lower and "group by" in sql_lower:
                return MockQueryResult(process_summary_df)
            elif "wakeup" in sql_lower or "sched_waking" in sql_lower:
                return MockQueryResult(sched_latency_df)
            else:
                return MockQueryResult(pd.DataFrame())

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

        def close(self):
            pass

    return MockTraceProcessor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_trace_processor(monkeypatch):
    """Install the mock TraceProcessor so scripts can be tested without Perfetto."""
    import sys
    from unittest import mock

    # Create a fake perfetto.trace_processor module
    fake_module = mock.MagicMock()
    fake_module.TraceProcessor = make_mock_trace_processor()
    monkeypatch.setitem(sys.modules, "perfetto.trace_processor", fake_module)
    monkeypatch.setattr(
        "perfetto.trace_processor.TraceProcessor",
        make_mock_trace_processor(),
    )
    return fake_module


@pytest.fixture
def mock_trace_processor_factory():
    """Fixture that returns the factory so tests can customize mock DataFrames."""
    return make_mock_trace_processor


@pytest.fixture
def tmp_home(tmp_path: pathlib.Path) -> pathlib.Path:
    """A temporary directory for test output."""
    return tmp_path


@pytest.fixture
def mock_trace_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a fake .perfetto-trace file (not a real trace, just a marker file).

    Since Perfetto traces are binary protobuf, we create a minimal non-empty
    file with the correct extension so the script can validate file existence.
    """
    path = tmp_path / "test-trace.perfetto-trace"
    # Write a minimal valid header that Perfetto's TraceProcessor would accept
    # Real traces start with an 8-byte magic: HPb\x00\x01\x00\x00\x00
    path.write_bytes(b"HPb\x00\x01\x00\x00\x00")
    return path


@pytest.fixture
def mock_trace_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a directory with multiple fake .perfetto-trace files."""
    d = tmp_path / "traces"
    d.mkdir()
    for name in ["trace-a.perfetto-trace", "trace-b.perfetto-trace"]:
        (d / name).write_bytes(b"HPb\x00\x01\x00\x00\x00")
    return d


@pytest.fixture
def mock_csv_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write fixture CSVs to a temporary directory and return the path."""
    d = tmp_path / "csv_input"
    d.mkdir()
    (d / "perfetto-threads.csv").write_text(THREADS_CSV)
    (d / "perfetto-cpu-util.csv").write_text(CPU_UTIL_CSV)
    (d / "perfetto-process-summary.csv").write_text(PROCESS_SUMMARY_CSV)
    (d / "perfetto-sched-latency.csv").write_text(SCHED_LATENCY_CSV)
    return d


def png_header_bytes() -> bytes:
    """Return the minimal valid PNG file header (8-byte magic + IHDR chunk).

    This is a 67-byte valid PNG: 8 magic bytes, IHDR chunk, IEND chunk.
    """
    import struct
    import zlib

    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    # Build IHDR chunk: width=1, height=1, bit_depth=8, color_type=2 (RGB)
    width = 1
    height = 1
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr_chunk = (
        struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    )

    # Build IEND chunk
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    return sig + ihdr_chunk + iend_chunk


@pytest.fixture
def mock_png_plots(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create fake PNG plot files to simulate existing plots."""
    d = tmp_path / "plots"
    d.mkdir()
    png_data = png_header_bytes()
    for name in [
        "cpu-timeline.png",
        "slice-distribution.png",
        "cpu-utilization.png",
        "sched-latency.png",
    ]:
        (d / name).write_bytes(png_data)
    return d


# ---------------------------------------------------------------------------
# Fixtures — weight-share (Family A) and request×limit heatmap (Family B)
#
# The schemas below are copied verbatim from run-experiment.sh:
#   summary.csv  : cell_label,replicate,nr_periods,nr_throttled,throttled_usec,
#                  usage_usec,cpu_weight,cpu_max            (8 columns)
#   cgroup CSV   : timestamp,pod,container,nr_periods,nr_throttled,
#                  throttled_usec,usage_usec,cpu_weight,
#                  cpu_max_quota,cpu_max_period             (10 columns)
# summary.csv lives at the --data-dir root; per-cell replicate dirs are nested
# under a run timestamp: <data-dir>/<timestamp>/<cell>/replicate-<N>/cgroup-<pod>.csv
# (multi-pod) or cgroup.csv (single-pod).
# ---------------------------------------------------------------------------

FAMILY_SUMMARY_COLUMNS = [
    "cell_label",
    "replicate",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
    "usage_usec",
    "cpu_weight",
    "cpu_max",
]

CGROUP_COLUMNS = [
    "timestamp",
    "pod",
    "container",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
    "usage_usec",
    "cpu_weight",
    "cpu_max_quota",
    "cpu_max_period",
]


def write_summary_csv(path: pathlib.Path, rows: list[tuple]) -> pathlib.Path:
    """Write a summary.csv with the runner's 8-column schema.

    ``rows`` are (cell_label, replicate, nr_periods, nr_throttled,
    throttled_usec, usage_usec, cpu_weight, cpu_max) tuples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(FAMILY_SUMMARY_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_cgroup_csv(
    path: pathlib.Path,
    pod: str,
    usage_usec: int,
    cpu_weight: int,
    cpu_max_quota: int,
    samples: int = 3,
) -> pathlib.Path:
    """Write a per-pod cgroup time series with the runner's 10-column schema.

    The final sample's usage_usec equals the summary row for that pod, matching
    how the runner builds summary.csv from the last cgroup line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(CGROUP_COLUMNS)]
    for i in range(1, samples + 1):
        lines.append(
            ",".join(
                [
                    f"2026-08-03T10:00:{i * 5:02d}Z",
                    pod,
                    "cpu-burner",
                    str(10 * i),  # nr_periods
                    "0",  # nr_throttled
                    "0",  # throttled_usec
                    str(int(usage_usec * i / samples)),
                    str(cpu_weight),
                    str(cpu_max_quota),
                    "100000",  # cpu_max_period
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def build_family_a_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Write a complete Family A (weight-share) fixture and return root.

    Two cells: a 2-pod cell (a:59 / b:100, usage 60000/100000, 3 replicates)
    and a 3-pod cell (a:59 / b:100 / c:40, usage 60000/100000/40000, 2
    replicates). Every pod/replicate has a cgroup-<pod>.csv file, so the
    weight-share script must include both cells in its output.
    """
    run_id = "20260803T000000Z"
    cell1 = "a=500m;b=500m"
    for rep in (1, 2, 3):
        base = root / run_id / cell1 / f"replicate-{rep}"
        write_cgroup_csv(base / "cgroup-a.csv", "a", 60000, 59, 50000)
        write_cgroup_csv(base / "cgroup-b.csv", "b", 100000, 100, 50000)
    cell2 = "a=300m;b=600m;c=600m"
    for rep in (1, 2):
        base = root / run_id / cell2 / f"replicate-{rep}"
        write_cgroup_csv(base / "cgroup-a.csv", "a", 60000, 59, 30000)
        write_cgroup_csv(base / "cgroup-b.csv", "b", 100000, 100, 60000)
        write_cgroup_csv(base / "cgroup-c.csv", "c", 40000, 40, 60000)

    rows: list[tuple] = []
    for rep in (1, 2, 3):
        rows.append((f"a-{cell1}", rep, 30, 0, 0, 60000, 59, 50000))
        rows.append((f"b-{cell1}", rep, 30, 0, 0, 100000, 100, 50000))
    for rep in (1, 2):
        rows.append((f"a-{cell2}", rep, 30, 0, 0, 60000, 59, 30000))
        rows.append((f"b-{cell2}", rep, 30, 0, 0, 100000, 100, 60000))
        rows.append((f"c-{cell2}", rep, 30, 0, 0, 40000, 40, 60000))
    write_summary_csv(root / "summary.csv", rows)
    return root


@pytest.fixture
def family_a_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family A fixture: two cells (2-pod and 3-pod), all cgroup files present."""
    return build_family_a_data_dir(tmp_path / "family-a")


def build_incomplete_cgroup_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family A fixture where one cell is missing a per-pod cgroup file.

    Cell ``x=200m;y=200m`` is missing ``cgroup-y.csv`` in replicate-2, so the
    weight-share script must skip that cell with a warning and still emit the
    complete cell ``a=500m;b=500m``.
    """
    run_id = "20260803T010000Z"
    complete = "a=500m;b=500m"
    for rep in (1, 2):
        base = root / run_id / complete / f"replicate-{rep}"
        write_cgroup_csv(base / "cgroup-a.csv", "a", 60000, 59, 50000)
        write_cgroup_csv(base / "cgroup-b.csv", "b", 100000, 100, 50000)
    incomplete = "x=200m;y=200m"
    for rep in (1, 2):
        base = root / run_id / incomplete / f"replicate-{rep}"
        write_cgroup_csv(base / "cgroup-x.csv", "x", 80000, 30, 20000)
        if rep == 1:
            write_cgroup_csv(base / "cgroup-y.csv", "y", 80000, 30, 20000)

    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((f"a-{complete}", rep, 30, 0, 0, 60000, 59, 50000))
        rows.append((f"b-{complete}", rep, 30, 0, 0, 100000, 100, 50000))
        rows.append((f"x-{incomplete}", rep, 30, 0, 0, 80000, 30, 20000))
        rows.append((f"y-{incomplete}", rep, 30, 0, 0, 80000, 30, 20000))
    write_summary_csv(root / "summary.csv", rows)
    return root


@pytest.fixture
def incomplete_cgroup_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family A fixture with a cell that is missing one cgroup file."""
    return build_incomplete_cgroup_data_dir(tmp_path / "family-a-incomplete")


# (cell_label, nr_periods, nr_throttled, throttled_usec, usage_usec, cpu_weight, cpu_max)
FAMILY_B_CELLS = [
    ("request=100m-limit=200m", 1390, 1251, 93422080, 27727824, 17, 20000),
    ("request=100m-limit=500m", 1250, 625, 16463983, 62563172, 17, 50000),
    ("request=100m-limit=1000m", 1200, 240, 1000000, 101000000, 17, 100000),
    ("request=500m-limit=1000m", 1300, 1040, 5000000, 89123456, 50, 100000),
    ("request=500m-limit=2000m", 1300, 130, 100000, 180000000, 50, 200000),
]


def build_family_b_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Write a complete Family B (request×limit matrix) fixture and return root.

    Five cells spanning requests {100, 500} and limits {200, 500, 1000, 2000},
    three replicates each with identical stats. Per-replicate throttling ratios
    are exact: 1251/1390=0.9, 625/1250=0.5, 240/1200=0.2, 1040/1300=0.8,
    130/1300=0.1. Cell value = mean across replicates, so the pivot values are
    the same exact ratios.
    """
    rows: list[tuple] = []
    for (
        cell,
        nr_periods,
        nr_throttled,
        throttled_usec,
        usage_usec,
        weight,
        cmax,
    ) in FAMILY_B_CELLS:
        for rep in (1, 2, 3):
            rows.append(
                (
                    cell,
                    rep,
                    nr_periods,
                    nr_throttled,
                    throttled_usec,
                    usage_usec,
                    weight,
                    cmax,
                )
            )
    write_summary_csv(root / "summary.csv", rows)
    return root


@pytest.fixture
def family_b_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family B fixture: 5 request×limit cells, 3 replicates each."""
    return build_family_b_data_dir(tmp_path / "family-b")


@pytest.fixture
def empty_summary_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Data dir whose summary.csv has only the header row (empty input)."""
    root = tmp_path / "empty-summary"
    write_summary_csv(root / "summary.csv", [])
    return root


# ---------------------------------------------------------------------------
# Fixtures — QoS hierarchy (Family C), latency interference (Family D),
# tunable sweep under contention (Family F)
#
# summary.csv keeps the runner's 8-column schema (FAMILY_SUMMARY_COLUMNS).
#   Family C adds per-cell cgroup-hierarchy-<node>.json (schema):
#     {node, timestamp, kubepods_slice_weight,
#      slices[{name, cpu_weight, pods[{name, cpu_weight, cpu_max}]}]}
#   Family D adds per-replicate latency.csv (load-generator contract:
#     timestamp,endpoint,latency_ms,status).
#   Family F adds per-replicate latency.csv + eevdf-slices.csv (eevdf-analyze
#     schema: timestamp_start,timestamp_end,duration_us,pid,task,cpu).
# ---------------------------------------------------------------------------


def write_latency_csv(
    path: pathlib.Path,
    values: list[float],
    endpoint: str = "users",
    status: int = 200,
) -> pathlib.Path:
    """Write a load-generator latency.csv (timestamp,endpoint,latency_ms,status).

    ``values=[]`` produces a header-only CSV, which latency_stats treats as
    empty input (percentiles 0.0, zero data rows).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,endpoint,latency_ms,status"]
    for i, v in enumerate(values, start=1):
        lines.append(f"2026-08-03T10:00:{i % 60:02d}Z,{endpoint},{v},{status}")
    path.write_text("\n".join(lines) + "\n")
    return path


def write_eevdf_slices_csv(path: pathlib.Path, durations: list[float]) -> pathlib.Path:
    """Write an eevdf-slices.csv (eevdf-analyze 6-column schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp_start,timestamp_end,duration_us,pid,task,cpu"]
    start = 0
    for i, d in enumerate(durations, start=1):
        lines.append(f"{start},{start + d},{d},{1000 + i},cpu-burner,0")
        start += d
    path.write_text("\n".join(lines) + "\n")
    return path


def family_c_hierarchy(node: str = "cp1") -> dict:
    """Golden cgroup-hierarchy JSON for one 3-QoS-class cell.

    Slice weights mirror research/experiments/tests/test-cgroup-hierarchy.bats
    (kubepods_slice_weight 100, burstable 46, besteffort 2). Pod weights
    59/100/1 match the Family C summary.csv cpu_weight column.
    """
    return {
        "node": node,
        "timestamp": "2026-08-03T10:00:00Z",
        "kubepods_slice_weight": "100",
        "slices": [
            {
                "name": "kubepods-guaranteed.slice",
                "cpu_weight": "100",
                "pods": [
                    {
                        "name": "kubepods-guaranteed-podg1.slice",
                        "cpu_weight": "59",
                        "cpu_max": "50000 100000",
                    }
                ],
            },
            {
                "name": "kubepods-burstable.slice",
                "cpu_weight": "46",
                "pods": [
                    {
                        "name": "kubepods-burstable-podb1.slice",
                        "cpu_weight": "100",
                        "cpu_max": "50000 100000",
                    }
                ],
            },
            {
                "name": "kubepods-besteffort.slice",
                "cpu_weight": "2",
                "pods": [
                    {
                        "name": "kubepods-besteffort-podbe1.slice",
                        "cpu_weight": "1",
                        "cpu_max": "max 100000",
                    }
                ],
            },
        ],
    }


def write_hierarchy_json(path: pathlib.Path, node: str = "cp1") -> pathlib.Path:
    """Write a cgroup-hierarchy-<node>.json file for one cell."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(family_c_hierarchy(node), indent=2) + "\n")
    return path


def build_family_c_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Complete Family C fixture: one 3-QoS cell with matching hierarchy JSON.

    Cell ``qos-compete`` runs a guaranteed pod (usage 60000, weight 59), a
    burstable pod (usage 100000, throttled 25000, weight 100) and a besteffort
    pod (usage 5000, weight 1), 2 replicates each. The hierarchy JSON lives at
    ``<root>/qos-compete/cgroup-hierarchy-cp1.json`` and carries the same pod
    weights (59/100/1), so verify_hierarchy_weights reports no mismatch.
    """
    cell = "qos-compete"
    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((f"guaranteed-{cell}", rep, 1000, 0, 0, 60000, 59, 50000))
        rows.append((f"burstable-{cell}", rep, 1000, 500, 25000, 100000, 100, 50000))
        rows.append((f"besteffort-{cell}", rep, 1000, 0, 0, 5000, 1, 100000))
    write_summary_csv(root / "summary.csv", rows)
    write_hierarchy_json(root / cell / "cgroup-hierarchy-cp1.json", node="cp1")
    return root


def build_incomplete_hierarchy_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family C fixture with a cell that has NO hierarchy JSON.

    ``qos-compete`` is complete; ``qos-broken`` has summary rows but no
    cgroup-hierarchy-*.json anywhere, so the analyzer must skip it with a
    warning and still emit the complete cell.
    """
    cell = "qos-compete"
    broken = "qos-broken"
    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((f"guaranteed-{cell}", rep, 1000, 0, 0, 60000, 59, 50000))
        rows.append((f"burstable-{cell}", rep, 1000, 0, 0, 100000, 100, 50000))
        rows.append((f"besteffort-{cell}", rep, 1000, 0, 0, 5000, 1, 100000))
        rows.append((f"guaranteed-{broken}", rep, 1000, 0, 0, 60000, 59, 50000))
        rows.append((f"burstable-{broken}", rep, 1000, 0, 0, 100000, 100, 50000))
        rows.append((f"besteffort-{broken}", rep, 1000, 0, 0, 5000, 1, 100000))
    write_summary_csv(root / "summary.csv", rows)
    write_hierarchy_json(root / cell / "cgroup-hierarchy-cp1.json", node="cp1")
    return root


def build_family_d_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Complete Family D fixture: two single-workload cells, 2 replicates.

    Cell ``req=100m-lim=200m`` is heavily throttled (900/1000 periods -> ratio
    0.9) with latencies 1..20 ms -> p50 10.5, p95 19.05, p99 19.81. Cell
    ``req=500m-lim=1000m`` is lightly throttled (ratio 0.1) with latencies
    1..10 ms -> p50 5.5, p95 9.55, p99 9.91. Every replicate has a latency.csv
    under ``<root>/<cell>/replicate-<n>/``.
    """
    cell1 = "req=100m-lim=200m"
    cell2 = "req=500m-lim=1000m"
    for rep in (1, 2):
        write_latency_csv(
            root / cell1 / f"replicate-{rep}" / "latency.csv", list(range(1, 21))
        )
        write_latency_csv(
            root / cell2 / f"replicate-{rep}" / "latency.csv", list(range(1, 11))
        )
    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((cell1, rep, 1000, 900, 9000000, 12000000, 17, 20000))
        rows.append((cell2, rep, 1000, 100, 1000000, 8000000, 50, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


def build_missing_latency_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family D fixture with a cell that has NO latency.csv.

    ``req=100m-lim=200m`` has latency files; ``req=500m-lim=1000m`` has summary
    rows only, so the analyzer must skip it with a warning and still emit the
    complete cell.
    """
    cell1 = "req=100m-lim=200m"
    cell2 = "req=500m-lim=1000m"
    for rep in (1, 2):
        write_latency_csv(
            root / cell1 / f"replicate-{rep}" / "latency.csv", list(range(1, 21))
        )
    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((cell1, rep, 1000, 900, 9000000, 12000000, 17, 20000))
        rows.append((cell2, rep, 1000, 100, 1000000, 8000000, 50, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


# Per-replicate p99 target (latency.csv samples are all equal -> tied percentiles)
FAMILY_F_P99 = {
    "default": (12.0, 12.0, 12.0),
    "base-slice-low": (6.0, 6.0, 6.0),
    "base-slice-mid": (12.0, 12.5, 13.0),
    "base-slice-high": (18.0, 18.0, 18.0),
}
# Per-replicate mean slice duration_us (eevdf-slices.csv rows are all equal)
FAMILY_F_SLICE_MEANS = {
    "default": (1000.0, 1100.0, 900.0),
    "base-slice-low": (500.0, 550.0, 450.0),
    "base-slice-mid": (1000.0, 1100.0, 900.0),
    "base-slice-high": (1500.0, 1650.0, 1350.0),
}


def build_family_f_data_dir(
    root: pathlib.Path,
    include_default: bool = True,
    degraded: bool = False,
) -> pathlib.Path:
    """Family F fixture: one cell per tunable set, 3 replicates each.

    Tunables: ``default`` (p99 12.0, slice 1000.0us), ``base-slice-low``
    (6.0 / 500.0us), ``base-slice-mid`` (12.5 / 1000.0us), ``base-slice-high``
    (18.0 / 1500.0us). With ``degraded=True``, replicate-2 of base-slice-low
    loses its eevdf-slices.csv so that tunable drops to n=2.
    """
    tunables = ["base-slice-low", "base-slice-mid", "base-slice-high"]
    if include_default:
        tunables.insert(0, "default")
    rows: list[tuple] = []
    for tun in tunables:
        for rep, (p99, slice_mean) in enumerate(
            zip(FAMILY_F_P99[tun], FAMILY_F_SLICE_MEANS[tun]), start=1
        ):
            base = root / tun / f"replicate-{rep}"
            write_latency_csv(base / "latency.csv", [p99] * 10)
            if not (degraded and tun == "base-slice-low" and rep == 2):
                write_eevdf_slices_csv(base / "eevdf-slices.csv", [slice_mean] * 3)
            rows.append((tun, rep, 1000, 0, 0, 100000, 59, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


@pytest.fixture
def family_c_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family C fixture: one 3-QoS cell with matching hierarchy JSON."""
    return build_family_c_data_dir(tmp_path / "family-c")


@pytest.fixture
def incomplete_hierarchy_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family C fixture with one cell missing its hierarchy JSON."""
    return build_incomplete_hierarchy_data_dir(tmp_path / "family-c-incomplete")


@pytest.fixture
def family_d_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family D fixture: two latency cells with throttling stats."""
    return build_family_d_data_dir(tmp_path / "family-d")


@pytest.fixture
def missing_latency_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family D fixture with one cell missing latency.csv."""
    return build_missing_latency_data_dir(tmp_path / "family-d-missing")


@pytest.fixture
def family_f_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family F fixture: four tunable sets, 3 replicates each."""
    return build_family_f_data_dir(tmp_path / "family-f")


@pytest.fixture
def family_f_degraded_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family F fixture where base-slice-low loses a replicate's eevdf-slices.csv."""
    return build_family_f_data_dir(tmp_path / "family-f-degraded", degraded=True)


@pytest.fixture
def family_f_no_default_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family F fixture without the 'default' tunable (significance edge)."""
    return build_family_f_data_dir(
        tmp_path / "family-f-no-default", include_default=False
    )


# ---------------------------------------------------------------------------
# Fixtures — analysis outputs consumed by generate-report.py
#
# The report generator reads these CSVs from one input dir and
# renders interaction-report.md. The values below mirror the analyzer fixtures
# (same cell labels, shares, percentiles, significance verdicts) so
# the report assertions stay consistent with the analyzer tests. Column
# schemas are copied verbatim from the analyzer output contracts:
#   weight-share-summary.csv  : cell,pod,achieved_share,weight_share,ratio_error
#   heatmap-throttling_ratio.csv : request + one column per limit (int names)
#   qos-summary.csv           : cell,qos_slice,pod,cpu_weight,achieved_share,throttled_usec
#   latency-summary.csv       : cell,p50,p95,p99,throttled_usec,usage_usec,throttling_ratio
#   latency-correlation.csv   : metric,correlation
#   tunables-comparison.csv   : tunable,mean_p99,std_p99,mean_slice_us,std_slice_us,n
#   tunables-significance.csv : tunable,mean_p99,default_mean_p99,diff_p99,noise_threshold,significant
#   burst-summary.csv (pinned contract): cell,replicate,nr_periods,
#     nr_throttled,throttled_usec,usage_usec,cpu_max_burst,cpu_max_quota
# qos-summary.csv rows can carry either the kubepods-guaranteed.slice wrapper
# (QOS_ROWS) or the direct kubepods-pod<uid>.slice TRUE-Guaranteed row
# (QOS_ROWS_DIRECT_GUARANTEED) — the latter is what qos-analyze.py
# emits when the snapshot has no wrapper slice.
# ---------------------------------------------------------------------------

REPORT_INPUT_FILES = [
    "weight-share-summary.csv",
    "heatmap-throttling_ratio.csv",
    "qos-summary.csv",
    "latency-summary.csv",
    "latency-correlation.csv",
    "tunables-comparison.csv",
    "tunables-significance.csv",
    "burst-summary.csv",
]

WEIGHT_SHARE_COLUMNS = ["cell", "pod", "achieved_share", "weight_share", "ratio_error"]
WEIGHT_SHARE_ROWS = [
    ("a=500m;b=500m", "a", 0.375, 59 / 159, 0.375 - 59 / 159),
    ("a=500m;b=500m", "b", 0.625, 100 / 159, 0.625 - 100 / 159),
    ("a=300m;b=600m;c=600m", "a", 0.3, 59 / 199, 0.3 - 59 / 199),
    ("a=300m;b=600m;c=600m", "b", 0.5, 100 / 199, 0.5 - 100 / 199),
    ("a=300m;b=600m;c=600m", "c", 0.2, 40 / 199, 0.2 - 40 / 199),
]

HEATMAP_COLUMNS = ["request", "200", "500", "1000", "2000"]
HEATMAP_ROWS = [
    (100, 0.9, 0.5, 0.2, None),  # None -> empty field -> NaN when read back
    (500, None, None, 0.8, 0.1),
]

QOS_COLUMNS = [
    "cell",
    "qos_slice",
    "pod",
    "cpu_weight",
    "achieved_share",
    "throttled_usec",
]
QOS_ROWS = [
    (
        "qos-compete",
        "kubepods-guaranteed.slice",
        "kubepods-guaranteed-podg1.slice",
        59,
        12 / 33,
        0,
    ),
    (
        "qos-compete",
        "kubepods-burstable.slice",
        "kubepods-burstable-podb1.slice",
        100,
        20 / 33,
        50000,
    ),
    (
        "qos-compete",
        "kubepods-besteffort.slice",
        "kubepods-besteffort-podbe1.slice",
        1,
        1 / 33,
        0,
    ),
]

# qos-summary rows for a TRUE-Guaranteed pod (systemd cgroup driver).
# qos-analyze.py emits the direct kubepods-pod<uid>.slice as the guaranteed row
# (self-representing: qos_slice == pod) when the snapshot has NO
# kubepods-guaranteed.slice wrapper. The report generator must sort this row
# as guaranteed, before burstable/besteffort.
QOS_ROWS_DIRECT_GUARANTEED = [
    (
        "qos-compete",
        "kubepods-podg1.slice",
        "kubepods-podg1.slice",
        59,
        12 / 33,
        0,
    ),
    (
        "qos-compete",
        "kubepods-burstable.slice",
        "kubepods-burstable-podb1.slice",
        100,
        20 / 33,
        50000,
    ),
    (
        "qos-compete",
        "kubepods-besteffort.slice",
        "kubepods-besteffort-podbe1.slice",
        1,
        1 / 33,
        0,
    ),
]

LATENCY_COLUMNS = [
    "cell",
    "p50",
    "p95",
    "p99",
    "throttled_usec",
    "usage_usec",
    "throttling_ratio",
]
LATENCY_ROWS = [
    ("req=100m-lim=200m", 10.5, 19.05, 19.81, 18000000, 24000000, 0.9),
    ("req=500m-lim=1000m", 5.5, 9.55, 9.91, 2000000, 16000000, 0.1),
]

CORRELATION_COLUMNS = ["metric", "correlation"]
CORRELATION_ROWS = [
    ("p50_vs_throttled_usec", 1.0),
    ("p95_vs_throttled_usec", 1.0),
    ("p99_vs_throttled_usec", 1.0),
]

TUN_COMPARISON_COLUMNS = [
    "tunable",
    "mean_p99",
    "std_p99",
    "mean_slice_us",
    "std_slice_us",
    "n",
]
TUN_COMPARISON_ROWS = [
    ("default", 12.0, 0.0, 1000.0, 100.0, 3),
    ("base-slice-low", 6.0, 0.0, 500.0, 50.0, 3),
    ("base-slice-mid", 12.5, 0.5, 1000.0, 100.0, 3),
    ("base-slice-high", 18.0, 0.0, 1500.0, 150.0, 3),
]

TUN_SIGNIFICANCE_COLUMNS = [
    "tunable",
    "mean_p99",
    "default_mean_p99",
    "diff_p99",
    "noise_threshold",
    "significant",
]
TUN_SIGNIFICANCE_ROWS = [
    ("base-slice-low", 6.0, 12.0, -6.0, 0.0, True),
    ("base-slice-mid", 12.5, 12.0, 0.5, 0.5, False),
    ("base-slice-high", 18.0, 12.0, 6.0, 0.0, True),
]

# Pinned burst contract:
#   burst-summary.csv — cell,replicate,nr_periods,nr_throttled,throttled_usec,
#   usage_usec,cpu_max_burst,cpu_max_quota
# cpu_max_burst is the value ACTUALLY written to cpu.max.burst during the cell
# (kernel-validated: 0 for the no-burst baseline, 25000 for the burst cell).
# cpu_max_quota is the CFS quota in microseconds (25000 = 250m). The `cell`
# labels are the experiment matrix labels copied from the real
# research/experiments/data/cpu-burst/summary.csv — the burst cell is labelled
# burst=100000 (the value the matrix requested, rejected EINVAL by the kernel)
# while the applied value is 25000; the generator MUST read the applied value
# from cpu_max_burst and never parse the cell label.
BURST_COLUMNS = [
    "cell",
    "replicate",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
    "usage_usec",
    "cpu_max_burst",
    "cpu_max_quota",
]
BURST_ROWS = [
    ("request=-limit=250m-burst=", 1, 124, 105, 5200000, 2750000, 0, 25000),
    ("request=-limit=250m-burst=", 2, 128, 105, 5300000, 2730000, 0, 25000),
    ("request=-limit=250m-burst=", 3, 127, 105, 5340000, 2754000, 0, 25000),
    ("request=-limit=250m-burst=100000", 1, 127, 0, 0, 2762704, 25000, 25000),
    ("request=-limit=250m-burst=100000", 2, 129, 0, 0, 2750086, 25000, 25000),
    ("request=-limit=250m-burst=100000", 3, 123, 0, 0, 2764686, 25000, 25000),
]


def write_analysis_csv(
    path: pathlib.Path,
    columns: list[str],
    rows: list[tuple],
) -> pathlib.Path:
    """Write one analysis-output CSV with the given schema.

    ``None`` in a row renders as an empty field (pandas reads it back as
    NaN, matching how the analyzers serialize empty pivot cells); everything
    else via ``str()``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def build_analysis_output_dir(
    root: pathlib.Path,
    *,
    shuffled: bool = False,
    qos_rows: list[tuple] | None = None,
) -> pathlib.Path:
    """Write all eight analysis-output CSVs into *root* and return it.

    Values mirror the analyzer fixtures so report assertions
    reuse the same hand-computed numbers. With ``shuffled=True`` the data
    rows are reversed inside every CSV while the schema stays identical —
    the report must sort, so output must be byte-identical either way
    (determinism). ``qos_rows`` overrides the qos-summary rows
    (QOS_ROWS_DIRECT_GUARANTEED is passed for the TRUE-Guaranteed
    layout; default is the wrapper layout QOS_ROWS).
    """
    specs = [
        ("weight-share-summary.csv", WEIGHT_SHARE_COLUMNS, WEIGHT_SHARE_ROWS),
        ("heatmap-throttling_ratio.csv", HEATMAP_COLUMNS, HEATMAP_ROWS),
        (
            "qos-summary.csv",
            QOS_COLUMNS,
            QOS_ROWS if qos_rows is None else qos_rows,
        ),
        ("latency-summary.csv", LATENCY_COLUMNS, LATENCY_ROWS),
        ("latency-correlation.csv", CORRELATION_COLUMNS, CORRELATION_ROWS),
        ("tunables-comparison.csv", TUN_COMPARISON_COLUMNS, TUN_COMPARISON_ROWS),
        ("tunables-significance.csv", TUN_SIGNIFICANCE_COLUMNS, TUN_SIGNIFICANCE_ROWS),
        ("burst-summary.csv", BURST_COLUMNS, BURST_ROWS),
    ]
    for filename, columns, rows in specs:
        data = list(reversed(rows)) if shuffled else rows
        write_analysis_csv(root / filename, columns, data)
    return root


@pytest.fixture
def analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete analysis-output fixture: all eight CSVs with known values."""
    return build_analysis_output_dir(tmp_path / "analysis-output")


@pytest.fixture
def shuffled_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Same CSVs as analysis_output_dir but with data rows reversed."""
    return build_analysis_output_dir(tmp_path / "analysis-shuffled", shuffled=True)


@pytest.fixture
def qos_direct_guaranteed_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """qos-summary carries the direct kubepods-podg1.slice row.

    Mirrors what qos-analyze.py emits for a TRUE-Guaranteed pod (systemd
    cgroup driver: no kubepods-guaranteed.slice wrapper — the pod slice IS
    the guaranteed row, self-representing). The report must sort that row as
    guaranteed, before burstable/besteffort.
    """
    return build_analysis_output_dir(
        tmp_path / "analysis-qos-direct-guaranteed",
        qos_rows=QOS_ROWS_DIRECT_GUARANTEED,
    )


@pytest.fixture
def empty_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis-output dir with no CSVs at all (empty input)."""
    root = tmp_path / "analysis-empty"
    root.mkdir()
    return root


@pytest.fixture
def partial_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis-output dir with only weight-share-summary.csv."""
    root = tmp_path / "analysis-partial"
    write_analysis_csv(
        root / "weight-share-summary.csv", WEIGHT_SHARE_COLUMNS, WEIGHT_SHARE_ROWS
    )
    return root


# ---------------------------------------------------------------------------
# FIX-4 fixtures — REAL runner output layout (verified by live runs)
#
# The runner writes:
#   <data-dir>/summary.csv                                   (8-column schema)
#   <data-dir>/<timestamp>/<cell>/replicate-<N>/cgroup-<pod>.csv
#   <data-dir>/<timestamp>/<cell>/replicate-<N>/latency.csv
#   <data-dir>/<timestamp>/<cell>/replicate-<N>/cgroup-hierarchy-<node>.json
# and the summary cell_label is "<pod>-<cell>" where <pod> may contain dashes
# (pod-a, pod-b, ls-api, batch-stress) and <cell> is the full matrix cell
# string that NAMES the cell directory (no pod prefix). A naive first-dash
# split of the label is therefore WRONG (pod-a-a_request=... -> pod "pod"),
# which is the verified bug these fixtures reproduce. Cell strings
# below are copied verbatim from research/experiments/data/*/summary.csv.
# ---------------------------------------------------------------------------

REAL_TS = "20260803T093031Z"

# Real cell strings (matrix cells; these ARE the cell directory names).
WS_CELL_500 = "a_request=500m-a_limit=-b_request=500m-b_limit=-c_request=-c_limit="
WS_CELL_100 = "a_request=100m-a_limit=-b_request=500m-b_limit=-c_request=-c_limit="
QOS_CELL = (
    "guaranteed_request=500m-guaranteed_limit=500m-"
    "burstable_request=500m-burstable_limit=2000m-besteffort_request=-besteffort_limit="
)
LAT_CELL_250 = (
    "ls-api_request=250m-ls-api_limit=1000m-"
    "batch-stress_request=1000m-batch-stress_limit=2000m"
)
LAT_CELL_500 = (
    "ls-api_request=500m-ls-api_limit=500m-"
    "batch-stress_request=1000m-batch-stress_limit=2000m"
)
TUN_CELL_PREFIX = (
    "ls-api_request=500m-ls-api_limit=500m-"
    "batch-stress_request=1000m-batch-stress_limit=2000m-tunables="
)


def build_real_weight_share_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family A fixture in the REAL layout, 3 dash-named pods per cell.

    Cells nest at ``<root>/<timestamp>/<cell>/replicate-<N>/``; summary rows
    carry ``pod-a/pod-b/pod-c - <cell>`` labels (pod names contain dashes, so
    first-dash splitting is wrong). Two cells, 3 pods each, 2 replicates.
    Weights 59/100/1 (pod-c is BestEffort, weight 1); per-replicate usage
    60000/100000/5000 (cell1) and 30000/150000/5000 (cell2).

    Expected shares (hand-computed, sum-then-divide over 2 replicates):
      cell1: achieved 12/33, 20/33, 1/33 ; weight 59/160, 100/160, 1/160
      cell2: achieved 6/37, 30/37, 1/37  ; weight 17/118, 100/118, 1/118
    """
    specs = {
        WS_CELL_500: (60000, 100000, 5000, 59),
        WS_CELL_100: (30000, 150000, 5000, 17),
    }
    rows: list[tuple] = []
    for cell, (ua, ub, uc, wa) in specs.items():
        for rep in (1, 2):
            base = root / REAL_TS / cell / f"replicate-{rep}"
            write_cgroup_csv(base / "cgroup-pod-a.csv", "pod-a", ua, wa, 50000)
            write_cgroup_csv(base / "cgroup-pod-b.csv", "pod-b", ub, 100, 50000)
            write_cgroup_csv(base / "cgroup-pod-c.csv", "pod-c", uc, 1, 100000)
            rows.append((f"pod-a-{cell}", rep, 30, 0, 0, ua, wa, 50000))
            rows.append((f"pod-b-{cell}", rep, 30, 0, 0, ub, 100, 50000))
            rows.append((f"pod-c-{cell}", rep, 30, 0, 0, uc, 1, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


def build_real_qos_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family C fixture in the REAL layout.

    The cgroup-hierarchy snapshot nests at
    ``<root>/<timestamp>/<cell>/replicate-<N>/cgroup-hierarchy-w1.json`` — the
    direct-child glob the analyzer used misses it. Summary
    rows carry ``guaranteed/burstable/besteffort - <cell>`` labels. Numbers
    mirror the flat family_c fixture so the same hand-computed shares apply
    (12/33, 20/33, 1/33; weights 59/100/1; throttled 0/50000/0).
    """
    rows: list[tuple] = []
    for rep in (1, 2):
        base = root / REAL_TS / QOS_CELL / f"replicate-{rep}"
        write_hierarchy_json(base / "cgroup-hierarchy-w1.json", node="w1")
        rows.append((f"guaranteed-{QOS_CELL}", rep, 1000, 0, 0, 60000, 59, 50000))
        rows.append(
            (f"burstable-{QOS_CELL}", rep, 1000, 500, 25000, 100000, 100, 200000)
        )
        rows.append((f"besteffort-{QOS_CELL}", rep, 1000, 0, 0, 5000, 1, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


def build_real_latency_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family D fixture in the REAL layout, two pods per cell.

    ``ls-api`` and ``batch-stress`` (a dash-named pod!) share each cell; one
    latency.csv per replicate nests at ``<timestamp>/<cell>/replicate-<N>/``.
    Summary labels ``ls-api-<cell>`` / ``batch-stress-<cell>`` force suffix
    cell resolution (naive first-dash split turns ``batch-stress-...`` into
    pod "batch"). Both pods' summary rows aggregate into ONE cell row.

    Expected (hand-computed):
      cell1 (250m): p50/p95/p99 10.5/19.05/19.81; throttled 18000000;
                    usage 24200000; ratio 1800/4000 = 0.45
      cell2 (500m): p50/p95/p99 5.5/9.55/9.91;  throttled 2000000;
                    usage 16200000; ratio 200/4000 = 0.05
    """
    specs = {
        LAT_CELL_250: (
            [float(v) for v in range(1, 21)],
            100000,
            9000000,
            12000000,
            900,
        ),
        LAT_CELL_500: ([float(v) for v in range(1, 11)], 100000, 1000000, 8000000, 100),
    }
    rows: list[tuple] = []
    for cell, (
        vals,
        ls_usage,
        bs_throttled,
        bs_usage,
        bs_throttled_nr,
    ) in specs.items():
        for rep in (1, 2):
            write_latency_csv(
                root / REAL_TS / cell / f"replicate-{rep}" / "latency.csv", vals
            )
            rows.append((f"ls-api-{cell}", rep, 1000, 0, 0, ls_usage, 59, 100000))
            rows.append(
                (
                    f"batch-stress-{cell}",
                    rep,
                    1000,
                    bs_throttled_nr,
                    bs_throttled,
                    bs_usage,
                    100,
                    200000,
                )
            )
    write_summary_csv(root / "summary.csv", rows)
    return root


def build_real_tunables_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family F fixture in the REAL layout with NO eevdf-slices.csv anywhere.

    Cell dirs end ``-tunables=<name>``; latency.csv nests at
    ``<timestamp>/<cell>/replicate-<N>/``; summary labels carry the
    ls-api/batch-stress pod prefix. FIX-4 relaxes the analyzer so the p99-only
    significance verdict is still emitted and slice columns are NaN.

    Per-replicate p99s: default 12.0, base-slice-low 6.0, base-slice-high 18.0
    (all tied -> std 0.0, n=3). Verdicts: low diff -6 significant, high diff
    +6 significant.
    """
    tunables = [
        ("default", (12.0, 12.0, 12.0)),
        ("base-slice-low", (6.0, 6.0, 6.0)),
        ("base-slice-high", (18.0, 18.0, 18.0)),
    ]
    rows: list[tuple] = []
    for name, p99s in tunables:
        cell = TUN_CELL_PREFIX + name
        for rep, p99 in enumerate(p99s, start=1):
            write_latency_csv(
                root / REAL_TS / cell / f"replicate-{rep}" / "latency.csv",
                [p99] * 10,
            )
            rows.append((f"ls-api-{cell}", rep, 1000, 0, 0, 100000, 59, 50000))
            rows.append(
                (f"batch-stress-{cell}", rep, 1000, 0, 0, 173000000, 100, 200000)
            )
    write_summary_csv(root / "summary.csv", rows)
    return root


def build_flat_noslices_tunables_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Flat-layout Family F fixture (dirs directly under root) with latency.csv
    only — NO eevdf-slices.csv. Pins the FIX-4 slice-optional rule in the
    layout the pre-FIX-4 tests use, so the relaxed rule is proven independent
    of the cell-dir discovery change."""
    tunables = [
        ("default", (12.0, 12.0, 12.0)),
        ("base-slice-low", (6.0, 6.0, 6.0)),
        ("base-slice-high", (18.0, 18.0, 18.0)),
    ]
    rows: list[tuple] = []
    for name, p99s in tunables:
        for rep, p99 in enumerate(p99s, start=1):
            write_latency_csv(
                root / name / f"replicate-{rep}" / "latency.csv", [p99] * 10
            )
            rows.append((name, rep, 1000, 0, 0, 100000, 59, 100000))
    write_summary_csv(root / "summary.csv", rows)
    return root


@pytest.fixture
def real_weight_share_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """FIX-4: weight-share fixture in the REAL runner layout."""
    return build_real_weight_share_data_dir(tmp_path / "weight-share-real")


@pytest.fixture
def real_qos_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """FIX-4: QoS fixture with replicate-nested hierarchy JSON."""
    return build_real_qos_data_dir(tmp_path / "qos-real")


@pytest.fixture
def real_latency_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """FIX-4: latency fixture with pod-prefixed labels + nesting."""
    return build_real_latency_data_dir(tmp_path / "latency-real")


@pytest.fixture
def real_tunables_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """FIX-4: tunables fixture with no eevdf-slices.csv anywhere."""
    return build_real_tunables_data_dir(tmp_path / "tunables-real")


@pytest.fixture
def flat_noslices_tunables_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """FIX-4: flat tunables fixture with latency.csv but no slices."""
    return build_flat_noslices_tunables_data_dir(tmp_path / "tunables-flat-noslices")


# ---------------------------------------------------------------------------
# Fixtures — dist-analyze.py (EEVDF CPU execution-time distribution)
#
# dist-analyze.py consumes staged experiment data per cell. Cell
# layout follows the runner convention, with --data-dir
# pointing at the run dir and cell dirs nested inside:
#   <data-dir>/<cell>/replicate-<N>/perfetto-trace.perfetto-trace
#   <data-dir>/<cell>/replicate-<N>/eevdf-<pod>-pids.csv
#   <data-dir>/<cell>/replicate-<N>/cgroup-<pod>.csv
#   <data-dir>/<cell>/replicate-<N>/metadata.json
#
# The Perfetto trace itself is never parsed in tests: a fake
# perfetto.trace_processor package (PYTHONPATH shim, same pattern as
# fake_perfetto_env) serves canned sched_slice / sched_stat_runtime rows for
# subprocess runs, and make_dist_mock_trace_processor serves them in-process.
# The pinned dist-analyze output contracts:
#   dist-slices.csv    ts_start_us,ts_end_us,duration_us,cpu,tid,thread_name,pod
#   dist-runtime.csv   ts,cpu,pid,tid,thread_name,pod,runtime_ns
#   dist-summary.csv   cell,replicate,pod,slice_count,total_exec_ms,mean_us,
#                      median_us,p50_us,p95_us,p99_us,max_us,throttle_ratio,
#                      cpu_weight,cpu_max,quality
#   dist-percentiles.json  {replicate: {pod: {p1..p99 at 1-decile steps}}}
# ---------------------------------------------------------------------------

DIST_SLICES_COLUMNS = [
    "ts_start_us",
    "ts_end_us",
    "duration_us",
    "cpu",
    "tid",
    "thread_name",
    "pod",
]
DIST_RUNTIME_COLUMNS = [
    "ts",
    "cpu",
    "pid",
    "tid",
    "thread_name",
    "pod",
    "runtime_ns",
]
DIST_SUMMARY_COLUMNS = [
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
DIST_PERCENTILE_STEPS = (1, 11, 21, 31, 41, 51, 61, 71, 81, 91, 99)
DIST_COVERAGE_THRESHOLD = 0.80
DIST_SYSTEM_POD = "system"
DIST_GUARD_S = 2.0
DIST_DEMAND_MILLICORES = 2000  # saturating demand on the 2-vCPU w1 worker

# eevdf-<pod>-pids.csv schema (cgroup-pid-watch.sh print_csv_header).
EEVDF_PIDS_COLUMNS = (
    "timestamp,pod,pid,sum_exec_runtime,wait_sum,sleep_sum,iowait_sum,"
    "nr_switches,nr_voluntary_switches,nr_involuntary_switches,run_delay,pcount"
)


def write_eevdf_pids_csv(
    path: pathlib.Path, pod: str, pids: list[int], samples: int = 2
) -> pathlib.Path:
    """Write an eevdf-<pod>-pids.csv with the cgroup-pid-watch schema.

    Every sample lists every pid with the pod label; the analyzer must build
    the pid->pod map from the pod/pid columns ONLY.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [EEVDF_PIDS_COLUMNS]
    for s in range(1, samples + 1):
        for pid in pids:
            lines.append(
                ",".join(
                    [
                        f"2026-08-05T10:00:{s:02d}Z",
                        pod,
                        str(pid),
                        "123456",
                        "0",
                        "0",
                        "0",
                        str(s * 10),
                        "0",
                        "0",
                        "0",
                        str(s),
                    ]
                )
            )
    path.write_text("\n".join(lines) + "\n")
    return path


def write_dist_cgroup_csv(
    path: pathlib.Path,
    pod: str,
    *,
    nr_periods: int,
    nr_throttled: int,
    cpu_weight: int,
    cpu_max_quota: int,
    container: str = "stress-ng",
    samples: int = 3,
) -> pathlib.Path:
    """Write a per-pod cgroup-<pod>.csv (runner 10-column schema).

    All samples carry the same nr_periods/nr_throttled so the analyzer's
    last-sample semantics equal the pinned ratio (summary.csv is built from
    the last cgroup line in the runner).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(CGROUP_COLUMNS)]
    for i in range(1, samples + 1):
        lines.append(
            ",".join(
                [
                    f"2026-08-05T10:00:{i * 5:02d}Z",
                    pod,
                    container,
                    str(nr_periods),
                    str(nr_throttled),
                    "5000",
                    "100000",
                    str(cpu_weight),
                    str(cpu_max_quota),
                    "100000",
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def write_dist_metadata_json(
    path: pathlib.Path,
    *,
    cell: str,
    replicate: int,
    pod_name: str,
    node_name: str = "w1",
    trace_size: int = 12345678,
) -> pathlib.Path:
    """Write a cell metadata.json (runner save_cell_metadata schema).

    The runner logs the trace file size per cell, so the
    pinned metadata carries perfetto_trace_file / perfetto_config /
    trace_file_size alongside the runner's base fields.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-05T10:00:00Z",
                "experiment_cell": cell,
                "replicate": replicate,
                "pod_name": pod_name,
                "node_name": node_name,
                "perfetto_trace_file": "perfetto-trace.perfetto-trace",
                "perfetto_config": "eevdf-deep",
                "trace_file_size": trace_size,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def write_dist_trace(cell_dir: pathlib.Path) -> pathlib.Path:
    """Write the fake .perfetto-trace marker the runner produces per cell."""
    cell_dir.mkdir(parents=True, exist_ok=True)
    path = cell_dir / "perfetto-trace.perfetto-trace"
    path.write_bytes(b"HPb\x00\x01\x00\x00\x00")
    return path


def dist_slice_rows(
    ts_starts: list[int],
    durations: list[int],
    tid: int,
    thread_name: str = "stress-ng-cpu",
    cpu: int = 0,
) -> list[dict]:
    """Build sched_slice rows as query_slices returns them (ts already in us)."""
    rows: list[dict] = []
    for ts, dur in zip(ts_starts, durations):
        rows.append(
            {
                "ts_start_us": ts,
                "ts_end_us": ts + dur,
                "duration_us": dur,
                "cpu": cpu,
                "tid": tid,
                "thread_name": thread_name,
            }
        )
    return rows


def dist_runtime_rows(
    rows: list[tuple[int, int, int, str, int]], cpu: int = 0
) -> list[dict]:
    """Build sched_stat_runtime rows: (ts, tid, pid, thread_name, runtime_ns)."""
    return [
        {
            "ts": ts,
            "cpu": cpu,
            "pid": pid,
            "tid": tid,
            "thread_name": name,
            "runtime_ns": runtime_ns,
        }
        for ts, tid, pid, name, runtime_ns in rows
    ]


# ---------------------------------------------------------------------------
# Canned trace data (served by the fake TraceProcessor)
# ---------------------------------------------------------------------------

# Three pods whose threads ALL share the name "stress-ng-cpu" (the collision
# case) plus two system threads with the same name. tids 101/201/301
# are pod a/b/c, 901/902 are unmapped. Durations per pod are [10..100] us (10
# slices) so summary stats are the hand-computed constants in the tests.
DIST_3POD_SLICES = (
    dist_slice_rows(
        [2_500_000 + i * 1_000_000 for i in range(10)],  # 2.5s .. 11.5s
        [(i + 1) * 10 for i in range(10)],
        tid=101,
    )
    + dist_slice_rows(
        [30_000_000 + i * 1_000_000 for i in range(10)],
        [(i + 1) * 10 for i in range(10)],
        tid=201,
    )
    + dist_slice_rows(
        [78_000_000 + i * 1_000_000 for i in range(10)],  # last ts 87.0s
        [(i + 1) * 10 for i in range(10)],
        tid=301,
    )
    + dist_slice_rows([50_000_000, 50_100_000], [5, 15], tid=901)
    + dist_slice_rows([55_000_000], [100], tid=902)
)

DIST_3POD_RUNTIME = dist_runtime_rows(
    [
        (3_000_000, 101, 101, "stress-ng-cpu", 1_400_000),
        (3_010_000, 101, 101, "stress-ng-cpu", 900_000),
        (31_000_000, 201, 201, "stress-ng-cpu", 1_100_000),
        (79_000_000, 301, 301, "stress-ng-cpu", 1_200_000),
        (50_050_000, 901, 901, "stress-ng-cpu", 700_000),
    ]
)

# Minimal slices for sanity-gate cells: 3 retained events spanning 2.5s..87s
# of the 90s measurement -> coverage 0.9389 (good).
DIST_SANITY_SLICES = dist_slice_rows(
    [2_500_000, 40_000_000, 87_000_000], [100, 200, 150], tid=1001
)
DIST_SANITY_RUNTIME = dist_runtime_rows(
    [(3_000_000, 1001, 1001, "stress-ng-cpu", 1_000_000)]
)

# Degraded cell: retained events cover only 2.5s..52.5s -> 50/90 = 0.5556.
DIST_DEGRADED_SLICES = dist_slice_rows(
    [2_500_000, 30_000_000, 52_500_000], [100, 200, 150], tid=1001
)

DIST_FAKE_BOUNDS = {"first_ts_ns": 2_000_000_000, "last_ts_ns": 88_000_000_000}


def _dist_fake_trace_processor_source() -> str:
    """Source of the fake perfetto.trace_processor package used by subprocess runs.

    The TraceProcessor serves canned rows for the query patterns dist-analyze
    runs: sched_slice per-slice extraction, sched_stat_runtime samples, and the
    trace-bounds MIN/MAX query. Canned data is loaded from a JSON file named by
    the DIST_FAKE_DATA environment variable.
    """
    return """
import json
import os
import pandas as pd


def _load():
    with open(os.environ["DIST_FAKE_DATA"]) as f:
        return json.load(f)


class QueryResult:
    def __init__(self, df):
        self._df = df

    def __iter__(self):
        return iter(self._df.itertuples(index=False))

    def as_pandas_dataframe(self):
        return self._df


class TraceProcessor:
    def __init__(self, *args, **kwargs):
        pass

    def query(self, sql):
        data = _load()
        sql_lower = sql.lower()
        if "sched_stat_runtime" in sql_lower:
            return QueryResult(pd.DataFrame(data.get("runtime", [])))
        if "sched_slice" in sql_lower and "min(ts)" in sql_lower:
            return QueryResult(
                pd.DataFrame(
                    [data.get("bounds", {"first_ts_ns": 0, "last_ts_ns": 0})]
                )
            )
        if "sched_slice" in sql_lower:
            return QueryResult(pd.DataFrame(data.get("slices", [])))
        return QueryResult(pd.DataFrame())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        pass
"""


def make_dist_fake_perfetto_env(
    tmp_path: pathlib.Path,
    slices_rows: list[dict],
    runtime_rows: list[dict],
    bounds: dict | None = None,
) -> tuple[dict, pathlib.Path]:
    """Write a fake perfetto package + canned data; return (env, data_path).

    Same pattern as fake_perfetto_env in test_analyze.py: the subprocess needs
    this fake package on PYTHONPATH to run past trace processing. The canned
    slices/runtime are the "trace" the analyzer reads.
    """
    pkg_root = tmp_path / "fake-perfetto-dist"
    pkg = pkg_root / "perfetto"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "trace_processor.py").write_text(_dist_fake_trace_processor_source())

    data_path = tmp_path / "dist-fake-data.json"
    data_path.write_text(
        json.dumps(
            {
                "slices": slices_rows,
                "runtime": runtime_rows,
                "bounds": bounds or DIST_FAKE_BOUNDS,
            }
        )
    )

    env = {
        "DIST_FAKE_DATA": str(data_path),
        "MPLBACKEND": "Agg",
    }
    env.update(
        {
            "PYTHONPATH": str(pkg_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
    )
    return env, data_path


def build_dist_three_pod_cell_dir(root: pathlib.Path) -> pathlib.Path:
    """Fixture: three pods whose threads share identical names.

    Cell ``co-located-a-b-c`` with pods a/b/c (tids 101/201/301 in the
    eevdf pid CSVs) plus unmapped tids 901/902 that must be labelled system.
    """
    cell = "co-located-a-b-c"
    rep_dir = root / cell / "replicate-1"
    write_dist_trace(rep_dir)
    write_eevdf_pids_csv(rep_dir / "eevdf-a-pids.csv", "a", [101])
    write_eevdf_pids_csv(rep_dir / "eevdf-b-pids.csv", "b", [201])
    write_eevdf_pids_csv(rep_dir / "eevdf-c-pids.csv", "c", [301])
    write_dist_cgroup_csv(
        rep_dir / "cgroup-a.csv",
        "a",
        nr_periods=1000,
        nr_throttled=0,
        cpu_weight=59,
        cpu_max_quota=50000,
        container="stress-ng",
    )
    write_dist_cgroup_csv(
        rep_dir / "cgroup-b.csv",
        "b",
        nr_periods=1000,
        nr_throttled=0,
        cpu_weight=100,
        cpu_max_quota=50000,
        container="stress-ng",
    )
    write_dist_cgroup_csv(
        rep_dir / "cgroup-c.csv",
        "c",
        nr_periods=1000,
        nr_throttled=0,
        cpu_weight=1,
        cpu_max_quota=100000,
        container="stress-ng",
    )
    write_dist_metadata_json(
        rep_dir / "metadata.json",
        cell=cell,
        replicate=1,
        pod_name="a",
    )
    return root


def build_dist_cell(
    root: pathlib.Path,
    cell: str,
    *,
    pod: str,
    pids: list[int],
    nr_periods: int,
    nr_throttled: int,
    cpu_weight: int,
    cpu_max_quota: int,
    replicates: int = 1,
) -> pathlib.Path:
    """Write one single-pod cell (1..N replicates) with all per-cell inputs."""
    for rep in range(1, replicates + 1):
        rep_dir = root / cell / f"replicate-{rep}"
        write_dist_trace(rep_dir)
        write_eevdf_pids_csv(rep_dir / f"eevdf-{pod}-pids.csv", pod, pids)
        write_dist_cgroup_csv(
            rep_dir / f"cgroup-{pod}.csv",
            pod,
            nr_periods=nr_periods,
            nr_throttled=nr_throttled,
            cpu_weight=cpu_weight,
            cpu_max_quota=cpu_max_quota,
        )
        write_dist_metadata_json(
            rep_dir / "metadata.json",
            cell=cell,
            replicate=rep,
            pod_name=pod,
        )
    return root


def build_dist_stress_saturating_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Pass fixture: five saturating stress-ng cells (single pod).

    limit<demand cells (100m/100m, 100m/1000m, 500m/500m) throttle at
    0.99/0.97/0.96 (>= 0.95); limit>=demand cells (500m/2000m, 1000m/2000m)
    at 0.01/0.02 (< 0.05). Weights/quota mirror the crun conversion table.
    """
    cells = [
        ("request=100m-limit=100m", 1000, 990, 17, 10000),
        ("request=100m-limit=1000m", 1000, 970, 17, 100000),
        ("request=500m-limit=500m", 1000, 960, 59, 50000),
        ("request=500m-limit=2000m", 1000, 10, 59, 200000),
        ("request=1000m-limit=2000m", 1000, 20, 100, 200000),
    ]
    for cell, nr_periods, nr_throttled, weight, quota in cells:
        build_dist_cell(
            root,
            cell,
            pod="stress-ng",
            pids=[1001],
            nr_periods=nr_periods,
            nr_throttled=nr_throttled,
            cpu_weight=weight,
            cpu_max_quota=quota,
        )
    return root


def build_dist_cpu_burner_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Pass fixture: light cpu-burner cells, throttle ~0 at
    limit >= 300m equivalent (500m/500m, 500m/2000m, 1000m/2000m)."""
    cells = [
        ("request=500m-limit=500m", 1000, 1, 59, 50000),
        ("request=500m-limit=2000m", 1000, 1, 59, 200000),
        ("request=1000m-limit=2000m", 1000, 1, 100, 200000),
    ]
    for cell, nr_periods, nr_throttled, weight, quota in cells:
        build_dist_cell(
            root,
            cell,
            pod="cpu-burner",
            pids=[1001],
            nr_periods=nr_periods,
            nr_throttled=nr_throttled,
            cpu_weight=weight,
            cpu_max_quota=quota,
        )
    return root


def build_dist_sanity_violation_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Fail fixture: a saturating stress-ng cell that under-throttles.

    request=100m-limit=100m with ratio 0.90 (< 0.95) must fail the gate and
    print a violation naming the cell and the measured ratio.
    """
    build_dist_cell(
        root,
        "request=100m-limit=100m",
        pod="stress-ng",
        pids=[1001],
        nr_periods=1000,
        nr_throttled=900,
        cpu_weight=17,
        cpu_max_quota=10000,
    )
    return root


def build_dist_degraded_cell_dir(root: pathlib.Path) -> pathlib.Path:
    """Fixture: two single-pod cells for coverage-quality runs.

    The fixture itself is coverage-neutral; the test picks which retained-span
    dataset the fake TraceProcessor serves. With DIST_SANITY_SLICES (events
    span 2.5s..87s) coverage is 0.9389 (good); with DIST_DEGRADED_SLICES
    (2.5s..52.5s) coverage is 0.5556 < 0.80 (degraded). Both exit 0 — a
    degraded cell is a quality flag, not a sanity-gate failure.
    """
    build_dist_cell(
        root,
        "coverage-a",
        pod="stress-ng",
        pids=[1001],
        nr_periods=1000,
        nr_throttled=10,
        cpu_weight=59,
        cpu_max_quota=200000,
    )
    build_dist_cell(
        root,
        "coverage-b",
        pod="stress-ng",
        pids=[1001],
        nr_periods=1000,
        nr_throttled=10,
        cpu_weight=59,
        cpu_max_quota=200000,
    )
    return root


def build_dist_two_replicate_cell_dir(root: pathlib.Path) -> pathlib.Path:
    """Aggregation fixture: one cell, two replicates.

    Summary must carry one row per (pod, replicate); dist-slices.csv is the
    deterministic concatenation across replicates.
    """
    build_dist_cell(
        root,
        "request=100m-limit=100m",
        pod="stress-ng",
        pids=[1001],
        nr_periods=1000,
        nr_throttled=990,
        cpu_weight=17,
        cpu_max_quota=10000,
        replicates=2,
    )
    return root


def build_dist_empty_cell_dir(root: pathlib.Path) -> pathlib.Path:
    """Edge: cell with NO eevdf-<pod>-pids.csv files.

    Every sched_slice thread is unmapped and must be labelled system.
    """
    cell = "no-pids-cell"
    rep_dir = root / cell / "replicate-1"
    write_dist_trace(rep_dir)
    write_dist_cgroup_csv(
        rep_dir / "cgroup-x.csv",
        "x",
        nr_periods=1000,
        nr_throttled=0,
        cpu_weight=59,
        cpu_max_quota=100000,
    )
    write_dist_metadata_json(
        rep_dir / "metadata.json",
        cell=cell,
        replicate=1,
        pod_name="x",
    )
    return root


@pytest.fixture
def dist_three_pod_cell_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Three-pod identical-thread-name fixture."""
    return build_dist_three_pod_cell_dir(tmp_path / "dist-three-pod")


@pytest.fixture
def dist_stress_saturating_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Pass fixture: 5 saturating stress-ng cells."""
    return build_dist_stress_saturating_data_dir(tmp_path / "dist-stress")


@pytest.fixture
def dist_cpu_burner_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Pass fixture: light cpu-burner cells."""
    return build_dist_cpu_burner_data_dir(tmp_path / "dist-burner")


@pytest.fixture
def dist_sanity_violation_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Fail fixture: under-throttled saturating cell."""
    return build_dist_sanity_violation_data_dir(tmp_path / "dist-violation")


@pytest.fixture
def dist_degraded_cell_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Fixture: good + degraded coverage cells."""
    return build_dist_degraded_cell_dir(tmp_path / "dist-coverage")


@pytest.fixture
def dist_two_replicate_cell_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Aggregation fixture: one cell, two replicates."""
    return build_dist_two_replicate_cell_dir(tmp_path / "dist-two-rep")


@pytest.fixture
def dist_empty_cell_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Edge fixture: no eevdf pid files anywhere."""
    return build_dist_empty_cell_dir(tmp_path / "dist-empty")


# ---------------------------------------------------------------------------
# In-process mock TraceProcessor for the thin trace layer
# ---------------------------------------------------------------------------


def make_dist_mock_trace_processor(
    slices_df: Any = None,
    runtime_df: Any = None,
    bounds_df: Any = None,
    query_log: list | None = None,
    fail_on_load: bool = False,
) -> Any:
    """Build a mock TraceProcessor serving dist-analyze's three query patterns.

    Routing (checked in this order):
      sched_stat_runtime            -> runtime_df
      sched_slice + min(ts)         -> bounds_df (trace_event_bounds)
      sched_slice (no group by)     -> slices_df
    Unmatched queries return an empty result set.
    """
    import pandas as pd

    if slices_df is None:
        slices_df = pd.DataFrame(
            {
                "ts_start_us": pd.Series(dtype="int64"),
                "ts_end_us": pd.Series(dtype="int64"),
                "duration_us": pd.Series(dtype="int64"),
                "cpu": pd.Series(dtype="int64"),
                "tid": pd.Series(dtype="int64"),
                "thread_name": pd.Series(dtype="str"),
            }
        )
    if runtime_df is None:
        runtime_df = pd.DataFrame(
            {
                "ts": pd.Series(dtype="int64"),
                "cpu": pd.Series(dtype="int64"),
                "pid": pd.Series(dtype="int64"),
                "tid": pd.Series(dtype="int64"),
                "thread_name": pd.Series(dtype="str"),
                "runtime_ns": pd.Series(dtype="int64"),
            }
        )
    if bounds_df is None:
        bounds_df = pd.DataFrame([{"first_ts_ns": 0, "last_ts_ns": 0}])
    if query_log is not None:
        query_log = query_log  # caller-managed list, mutated in-place

    class _QueryResult:
        def __init__(self, df: pd.DataFrame):
            self._df = df

        def __iter__(self):
            return iter(self._df.itertuples(index=False))

        def as_pandas_dataframe(self) -> pd.DataFrame:
            return self._df

    class _MockTraceProcessor:
        def __init__(self, *args, **kwargs):
            if fail_on_load:
                raise RuntimeError("simulated corrupt trace: cannot parse")

        def query(self, sql: str) -> _QueryResult:
            if query_log is not None:
                query_log.append(sql)
            sql_lower = sql.lower()
            if "sched_stat_runtime" in sql_lower:
                return _QueryResult(runtime_df)
            if "sched_slice" in sql_lower and "min(ts)" in sql_lower:
                return _QueryResult(bounds_df)
            if "sched_slice" in sql_lower:
                return _QueryResult(slices_df)
            return _QueryResult(pd.DataFrame())

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

        def close(self):
            pass

    return _MockTraceProcessor


@pytest.fixture
def dist_mock_trace_processor(monkeypatch) -> Any:
    """Install the dist mock TraceProcessor so the thin layer runs in-process.

    Only ``sys.modules`` is patched: the script's ``from
    perfetto.trace_processor import TraceProcessor`` consults ``sys.modules``
    first, so replacing the entry is sufficient (a dotted-path
    ``monkeypatch.setattr`` would fail when the real perfetto package is
    installed, because perfetto lazily exposes its submodules).
    """
    import sys
    from unittest import mock

    fake_module = mock.MagicMock()
    fake_module.TraceProcessor = make_dist_mock_trace_processor()
    monkeypatch.setitem(sys.modules, "perfetto.trace_processor", fake_module)
    return fake_module


@pytest.fixture
def dist_mock_trace_processor_factory():
    """Returns the factory so tests can customize canned DataFrames."""
    return make_dist_mock_trace_processor


@pytest.fixture
def dist_fake_perfetto_env(tmp_path: pathlib.Path):
    """Factory for subprocess fake-perfetto envs (see make_dist_fake_perfetto_env)."""

    def _make(
        slices_rows: list[dict] | None = None,
        runtime_rows: list[dict] | None = None,
        bounds: dict | None = None,
    ) -> dict:
        env, _data = make_dist_fake_perfetto_env(
            tmp_path,
            slices_rows if slices_rows is not None else DIST_3POD_SLICES,
            runtime_rows if runtime_rows is not None else DIST_3POD_RUNTIME,
            bounds=bounds,
        )
        return env

    return _make


# ---------------------------------------------------------------------------
# Fixtures — dist-steps.py (six step-by-step images)
#
# dist-steps.py consumes dist-analyze OUTPUT (no traces, no cluster, no
# network):
#   <analysis-root>/distribution/<family>/<cell>/dist-slices.csv
#   <analysis-root>/distribution/<family>/<cell>/dist-summary.csv
#   <analysis-root>/distribution/<family>/<cell>/dist-percentiles.json
#
# The family fixture mirrors the six Family A stress-ng cells: the same
# labels, the crun CpuShares->cpu.weight conversion (1/17/59/100) and the
# cpu.max quotas (100000/10000/100000/50000/200000/200000). Every cell carries
# the D10 slice-duration set [10..100] us (hand-computed stats: mean 55,
# p50 55, p95 95.5, p99 99.1, max 100) with deterministic 1s-spaced ts_start.
# The throttle ratios differ per cell (0.0 / 0.99 / 0.97 / 0.96 / 0.01 / 0.02)
# so step-4/step-6 annotations carry distinct measured numbers.
# ---------------------------------------------------------------------------

# The exactly-six pinned output names, in step order 1..6.
DIST_STEPS_FILES = (
    "step-1-declared-vs-enforced.png",
    "step-2-weight-vs-quota.png",
    "step-3-slice-distribution.png",
    "step-4-throttle-pattern.png",
    "step-5-config-comparison.png",
    "step-6-guideline-summary.png",
)

# (cell label, cpu_weight, cpu_max_quota, nr_periods, nr_throttled)
# Order IS the pinned six-cell order (cell 0 = no-limit saturating cell used
# by step-3; cell 3 = the 500m/500m quota cell used by step-4).
DIST_STEPS_CELL_SPECS = [
    ("request=-limit=", 1, 100000, 1000, 0),
    ("request=100m-limit=100m", 17, 10000, 1000, 990),
    ("request=100m-limit=1000m", 17, 100000, 1000, 970),
    ("request=500m-limit=500m", 59, 50000, 1000, 960),
    ("request=500m-limit=2000m", 59, 200000, 1000, 10),
    ("request=1000m-limit=2000m", 100, 200000, 1000, 20),
]
DIST_STEPS_CELLS = tuple(spec[0] for spec in DIST_STEPS_CELL_SPECS)

# D10 durations -> hand-computed stats (pandas linear interpolation, pinned in
# the dist-analyze tests): mean 55, p50 55, p95 95.5, p99 99.1, max 100.
DIST_STEPS_D10 = [float(v) for v in range(10, 101, 10)]
DIST_STEPS_ALT = [100.0, 200.0, 300.0, 400.0, 500.0]


def _dist_steps_stats(durations: list[float]) -> dict[str, float]:
    """Compute the dist-summary stats for a duration list (pandas method)."""
    import pandas as pd

    series = pd.Series(list(durations), dtype="float64")
    return {
        "mean_us": float(series.mean()),
        "median_us": float(series.median()),
        "p50_us": float(series.quantile(0.50)),
        "p95_us": float(series.quantile(0.95)),
        "p99_us": float(series.quantile(0.99)),
        "max_us": float(series.max()),
    }


def write_dist_steps_cell_outputs(
    cell_dir: pathlib.Path,
    *,
    cell: str,
    durations: list[float],
    cpu_weight: int,
    cpu_max: int,
    throttle_ratio: float,
) -> pathlib.Path:
    """Write one cell's dist-analyze OUTPUT files (dist-slices/summary/percentiles).

    This is the exact layout dist-steps.py must read; the values are the
    fixture's "measured" data the annotations must reflect.
    """
    import pandas as pd

    cell_dir.mkdir(parents=True, exist_ok=True)
    stats = _dist_steps_stats(durations)

    # dist-slices.csv — one row per slice, 1s-spaced starts (deterministic).
    slice_rows = []
    for i, dur in enumerate(durations):
        start = 2_500_000 + i * 1_000_000
        slice_rows.append(
            (start, start + int(dur), float(dur), 0, 1001, "stress-ng-cpu", "stress-ng")
        )
    pd.DataFrame(slice_rows, columns=DIST_SLICES_COLUMNS).to_csv(
        cell_dir / "dist-slices.csv", index=False
    )

    # dist-summary.csv — the pinned 15-column schema, one pod row.
    summary_row = [
        cell,
        1,
        "stress-ng",
        len(durations),
        float(sum(durations)) / 1000.0,
        stats["mean_us"],
        stats["median_us"],
        stats["p50_us"],
        stats["p95_us"],
        stats["p99_us"],
        stats["max_us"],
        throttle_ratio,
        cpu_weight,
        cpu_max,
        "good",
    ]
    pd.DataFrame([summary_row], columns=DIST_SUMMARY_COLUMNS).to_csv(
        cell_dir / "dist-summary.csv", index=False
    )

    # dist-percentiles.json — {replicate: {pod: {p<k>: value}}}, sorted keys.
    series = pd.Series(list(durations), dtype="float64")
    table = {f"p{k}": float(series.quantile(k / 100.0)) for k in DIST_PERCENTILE_STEPS}
    (cell_dir / "dist-percentiles.json").write_text(
        json.dumps({"1": {"stress-ng": table}}, indent=2, sort_keys=True) + "\n"
    )
    return cell_dir


def build_dist_steps_family(
    root: pathlib.Path, durations: list[float] | None = None
) -> pathlib.Path:
    """Write the six-cell Family A stress-ng dist-analyze OUTPUT tree."""
    if durations is None:
        durations = DIST_STEPS_D10
    for cell, weight, quota, periods, throttled in DIST_STEPS_CELL_SPECS:
        write_dist_steps_cell_outputs(
            root / "distribution" / "dist-stress-ng" / cell,
            cell=cell,
            durations=durations,
            cpu_weight=weight,
            cpu_max=quota,
            throttle_ratio=throttled / periods,
        )
    return root


def build_dist_steps_missing_cell_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family fixture missing the 500m/500m quota cell (step-4's source).

    The renderer must fail loudly naming the missing cell instead of silently
    producing five images.
    """
    for idx, (cell, weight, quota, periods, throttled) in enumerate(
        DIST_STEPS_CELL_SPECS
    ):
        if idx == 3:  # request=500m-limit=500m — the step-4 quota cell
            continue
        write_dist_steps_cell_outputs(
            root / "distribution" / "dist-stress-ng" / cell,
            cell=cell,
            durations=DIST_STEPS_D10,
            cpu_weight=weight,
            cpu_max=quota,
            throttle_ratio=throttled / periods,
        )
    return root


def build_dist_steps_empty_slices_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family fixture where the no-limit cell's dist-slices.csv has no rows.

    A distribution image needs measured slices; the renderer must fail loudly
    naming the empty cell.
    """
    import pandas as pd

    build_dist_steps_family(root)
    no_limit = root / "distribution" / "dist-stress-ng" / DIST_STEPS_CELLS[0]
    pd.DataFrame(columns=DIST_SLICES_COLUMNS).to_csv(
        no_limit / "dist-slices.csv", index=False
    )
    return root


@pytest.fixture
def dist_steps_family_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Six-cell dist-analyze OUTPUT fixture for the step-by-step renderer."""
    return build_dist_steps_family(tmp_path / "dist-steps-family")


@pytest.fixture
def dist_steps_missing_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family fixture with the 500m/500m quota cell missing."""
    return build_dist_steps_missing_cell_data_dir(tmp_path / "dist-steps-missing")


@pytest.fixture
def dist_steps_empty_slices_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family fixture where the no-limit cell has zero slice rows."""
    return build_dist_steps_empty_slices_data_dir(tmp_path / "dist-steps-empty")
