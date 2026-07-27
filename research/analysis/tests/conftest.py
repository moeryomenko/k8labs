"""Shared test fixtures for perfetto analysis tests.

Provides mock TraceProcessor, fixture CSV data, and helper utilities
so tests can run without a real Perfetto trace file or installation.
"""

from __future__ import annotations

import io
import os
import pathlib
import struct
import zipfile
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
            if "sched_slice" in sql_lower and "group by" in sql_lower and "thread" in sql_lower:
                return MockQueryResult(threads_df)
            elif "sched_slice" in sql_lower and "group by" in sql_lower and "cpu" in sql_lower:
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
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)

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
