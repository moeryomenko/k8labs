"""Shared test fixtures for perfetto analysis tests.

Provides mock TraceProcessor, fixture CSV data, and helper utilities
so tests can run without a real Perfetto trace file or installation.
"""

from __future__ import annotations

import json
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
# TASK-014 fixtures — weight-share (Family A) and request×limit heatmap (Family B)
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
    """Family A fixture with a cell that is missing one cgroup file (REQ-2)."""
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
    """Data dir whose summary.csv has only the header row (REQ-4 empty input)."""
    root = tmp_path / "empty-summary"
    write_summary_csv(root / "summary.csv", [])
    return root


# ---------------------------------------------------------------------------
# TASK-016 fixtures — QoS hierarchy (Family C), latency interference (Family D),
# tunable sweep under contention (Family F)
#
# summary.csv keeps the runner's 8-column schema (FAMILY_SUMMARY_COLUMNS).
#   Family C adds per-cell cgroup-hierarchy-<node>.json (TASK-009 schema):
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
    """Family C fixture with a cell that has NO hierarchy JSON (REQ-2).

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
    """Family D fixture with a cell that has NO latency.csv (REQ-4).

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
    """Family C fixture with one cell missing its hierarchy JSON (REQ-2)."""
    return build_incomplete_hierarchy_data_dir(tmp_path / "family-c-incomplete")


@pytest.fixture
def family_d_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete Family D fixture: two latency cells with throttling stats."""
    return build_family_d_data_dir(tmp_path / "family-d")


@pytest.fixture
def missing_latency_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family D fixture with one cell missing latency.csv (REQ-4)."""
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
# TASK-018 fixtures — analysis outputs consumed by generate-report.py
#
# The report generator (TASK-019) reads these CSVs from one input dir and
# renders interaction-report.md. The values below mirror the TASK-014/016
# fixtures (same cell labels, shares, percentiles, significance verdicts) so
# the report assertions stay consistent with the analyzer tests. Column
# schemas are copied verbatim from the analyzer output contracts:
#   weight-share-summary.csv  : cell,pod,achieved_share,weight_share,ratio_error
#   heatmap-throttling_ratio.csv : request + one column per limit (int names)
#   qos-summary.csv           : cell,qos_slice,pod,cpu_weight,achieved_share,throttled_usec
#   latency-summary.csv       : cell,p50,p95,p99,throttled_usec,usage_usec,throttling_ratio
#   latency-correlation.csv   : metric,correlation
#   tunables-comparison.csv   : tunable,mean_p99,std_p99,mean_slice_us,std_slice_us,n
#   tunables-significance.csv : tunable,mean_p99,default_mean_p99,diff_p99,noise_threshold,significant
# ---------------------------------------------------------------------------

REPORT_INPUT_FILES = [
    "weight-share-summary.csv",
    "heatmap-throttling_ratio.csv",
    "qos-summary.csv",
    "latency-summary.csv",
    "latency-correlation.csv",
    "tunables-comparison.csv",
    "tunables-significance.csv",
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
) -> pathlib.Path:
    """Write all seven analysis-output CSVs into *root* and return it.

    Values mirror the TASK-014/016 analyzer fixtures so report assertions
    reuse the same hand-computed numbers. With ``shuffled=True`` the data
    rows are reversed inside every CSV while the schema stays identical —
    the report must sort, so output must be byte-identical either way
    (REQ-4 determinism).
    """
    specs = [
        ("weight-share-summary.csv", WEIGHT_SHARE_COLUMNS, WEIGHT_SHARE_ROWS),
        ("heatmap-throttling_ratio.csv", HEATMAP_COLUMNS, HEATMAP_ROWS),
        ("qos-summary.csv", QOS_COLUMNS, QOS_ROWS),
        ("latency-summary.csv", LATENCY_COLUMNS, LATENCY_ROWS),
        ("latency-correlation.csv", CORRELATION_COLUMNS, CORRELATION_ROWS),
        ("tunables-comparison.csv", TUN_COMPARISON_COLUMNS, TUN_COMPARISON_ROWS),
        ("tunables-significance.csv", TUN_SIGNIFICANCE_COLUMNS, TUN_SIGNIFICANCE_ROWS),
    ]
    for filename, columns, rows in specs:
        data = list(reversed(rows)) if shuffled else rows
        write_analysis_csv(root / filename, columns, data)
    return root


@pytest.fixture
def analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Complete analysis-output fixture: all seven CSVs with known values."""
    return build_analysis_output_dir(tmp_path / "analysis-output")


@pytest.fixture
def shuffled_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Same CSVs as analysis_output_dir but with data rows reversed."""
    return build_analysis_output_dir(tmp_path / "analysis-shuffled", shuffled=True)


@pytest.fixture
def empty_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis-output dir with no CSVs at all (REQ-2 empty input)."""
    root = tmp_path / "analysis-empty"
    root.mkdir()
    return root


@pytest.fixture
def partial_analysis_output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis-output dir with only weight-share-summary.csv (REQ-2)."""
    root = tmp_path / "analysis-partial"
    write_analysis_csv(
        root / "weight-share-summary.csv", WEIGHT_SHARE_COLUMNS, WEIGHT_SHARE_ROWS
    )
    return root
