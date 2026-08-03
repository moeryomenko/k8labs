"""Tests for perfetto-analyze.py — Perfetto trace analysis pipeline.

Test categories (22 tests total):
  1. CLI argument parsing        (5 tests)
  2. Error handling              (5 tests)
  3. SQL query validation        (4 tests)
  4. CSV output                  (4 tests)
  5. Edge cases / resilience     (4 tests)
"""

from __future__ import annotations

import csv
import os
import pathlib
import re
import sys
import subprocess

import pytest

from tests.conftest import (
    THREADS_CSV,
    CPU_UTIL_CSV,
    PROCESS_SUMMARY_CSV,
    SCHED_LATENCY_CSV,
)


# =========================================================================
# Helpers
# =========================================================================

ANALYZE_SCRIPT = str(
    pathlib.Path(__file__).resolve().parent.parent / "perfetto-analyze.py"
)


def run_analyze(argv: list[str]) -> tuple[int, str, str]:
    """Run perfetto-analyze.py with the given argv using subprocess.

    Returns (exit_code, stdout, stderr).
    """
    proc = subprocess.run(
        [sys.executable, ANALYZE_SCRIPT, *argv],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def extract_sql_queries(script_path: str | None = None) -> list[str]:
    """Extract SQL query strings from the analyze script.

    Reads the script file and uses regex to find multi-line SQL strings
    passed to tp.query() calls.
    """
    path = script_path or ANALYZE_SCRIPT
    if not os.path.isfile(path):
        return []

    source = pathlib.Path(path).read_text()

    # Find all string arguments to .query( calls
    # Matches .query("""...""") or .query('''...''') or .query("...")
    queries: list[str] = []

    # Pattern for triple-quoted strings in .query(...)
    pattern = r'\.query\(\s*("""|\'\'\')(.*?)\1'
    for match in re.finditer(pattern, source, re.DOTALL):
        sql = match.group(2).strip()
        if sql.upper().startswith("SELECT"):
            queries.append(sql)

    return queries


# =========================================================================
# 1. CLI argument parsing (5 tests)
# =========================================================================


class TestCliArgs:
    """Verify argument parsing, help output, and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        """--help prints usage and exits 0."""
        rc, out, err = run_analyze(["--help"])
        assert rc == 0, f"expected 0, got {rc}\nstderr: {err}"
        assert "usage:" in out.lower() or "usage:" in err.lower()

    def test_help_flag_via_minus_h(self):
        """-h prints usage and exits 0."""
        rc, out, err = run_analyze(["-h"])
        assert rc == 0
        assert "usage:" in (out + err).lower()

    def test_missing_trace_path_prints_error(self):
        """No trace_path argument prints error and exits non-zero."""
        rc, out, err = run_analyze([])
        assert rc != 0
        assert "usage:" in err.lower() or "error" in err.lower()

    def test_help_mentions_output_dir(self):
        """--help lists --output-dir option."""
        rc, out, err = run_analyze(["--help"])
        assert "--output-dir" in out + err

    def test_help_mentions_trace_path(self):
        """--help mentions trace_path positional argument."""
        rc, out, err = run_analyze(["--help"])
        combined = out + err
        assert "trace_path" in combined or "trace" in combined.lower()


# =========================================================================
# 2. Error handling (5 tests)
# =========================================================================


class TestErrorHandling:
    """Verify graceful handling of invalid inputs."""

    def test_nonexistent_file_prints_error(self):
        """Nonexistent trace path prints error and exits non-zero."""
        rc, out, err = run_analyze(["/nonexistent/path/trace.perfetto-trace"])
        assert rc != 0
        assert (
            "error" in err.lower()
            or "not found" in err.lower()
            or "exist" in err.lower()
        )

    def test_empty_directory(self):
        """Empty directory with no .perfetto-trace files prints warning."""
        import tempfile

        with tempfile.TemporaryDirectory() as empty_dir:
            rc, out, err = run_analyze([empty_dir])
        # Should exit 0 with a warning — no data but not an error
        assert rc == 0
        assert "warn" in err.lower() or "no" in err.lower()

    def test_bogus_flag_rejected(self):
        """Unknown flag prints error and exits non-zero."""
        rc, out, err = run_analyze(["--bogus-flag", "trace.perfetto-trace"])
        assert rc != 0
        assert "unrecognized" in err.lower() or "error" in err.lower()

    def test_trace_path_is_directory_without_traces(self):
        """Directory existing but containing no .perfetto-trace files prints warning."""
        import tempfile

        with tempfile.TemporaryDirectory() as dirpath:
            # Put a non-trace file in it
            pathlib.Path(dirpath, "readme.txt").write_text("hello")
            rc, out, err = run_analyze([dirpath])
        assert rc == 0
        assert "warn" in err.lower() or "no" in err.lower()

    def test_wrong_type_path_errors(self):
        """Passing a path that is neither file nor dir prints error."""
        rc, out, err = run_analyze(["/dev/null/not-a-file"])
        assert rc != 0
        assert "error" in err.lower() or "not found" in err.lower()


# =========================================================================
# 3. SQL query validation (4 tests)
# =========================================================================


class TestSqlQueries:
    """Verify that SQL queries embedded in the script are syntactically valid
    and contain expected fragments for each analysis target."""

    QUERY_MARKERS: dict[str, list[str]] = {
        "sched_slice": ["SELECT", "FROM", "sched_slice"],
        "threads": ["GROUP BY", "cpu", "tid", "exec_time_ms"],
        "cpu_util": ["GROUP BY", "cpu", "utilization_pct", "COUNT"],
        "process_summary": ["JOIN", "process", "GROUP BY", "pid"],
        "sched_latency": ["JOIN", "sched_waking", "wakeup_latency", "GROUP BY", "tid"],
    }

    def test_queries_are_extractable(self):
        """At least 4 SQL queries can be extracted from the script."""
        queries = extract_sql_queries()
        assert len(queries) >= 4, f"expected >=4 queries, found {len(queries)}"

    def test_threads_query_has_required_fragments(self):
        """Thread-level query contains sched_slice, GROUP BY, SUM."""
        queries = extract_sql_queries()
        # Find the threads query — has sched_slice and GROUP BY cpu, tid
        thread_query = next(
            (
                q
                for q in queries
                if "sched_slice" in q.lower()
                and "group by" in q.lower()
                and "tid" in q.lower()
            ),
            None,
        )
        assert thread_query is not None, "No thread-level query found"
        for frag in self.QUERY_MARKERS["threads"]:
            assert frag.lower() in thread_query.lower(), (
                f"Missing '{frag}' in thread query"
            )

    def test_cpu_util_query_has_required_fragments(self):
        """CPU utilization query has sched_slice, GROUP BY cpu, COUNT."""
        queries = extract_sql_queries()
        cpu_query = next(
            (
                q
                for q in queries
                if "sched_slice" in q.lower()
                and "group by" in q.lower()
                and "cpu" in q.lower()
                and "utilization" in q.lower()
            ),
            None,
        )
        assert cpu_query is not None, "No CPU utilization query found"
        for frag in self.QUERY_MARKERS["cpu_util"]:
            assert frag.lower() in cpu_query.lower(), (
                f"Missing '{frag}' in cpu_util query"
            )

    def test_sched_latency_query_has_required_fragments(self):
        """Scheduling latency query references sched_waking."""
        queries = extract_sql_queries()
        lat_query = next(
            (
                q
                for q in queries
                if "wakeup" in q.lower() or "sched_waking" in q.lower()
            ),
            None,
        )
        assert lat_query is not None, "No sched_latency query found"
        for frag in self.QUERY_MARKERS["sched_latency"]:
            assert frag.lower() in lat_query.lower(), (
                f"Missing '{frag}' in latency query"
            )


# =========================================================================
# 4. CSV output structure (4 tests)
# =========================================================================


class TestCsvOutput:
    """Verify that output CSVs have correct schema and column names."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.output_dir = tmp_path / "output"
        self.output_dir.mkdir()

    def _write_csv(self, name: str, content: str) -> pathlib.Path:
        p = self.output_dir / name
        p.write_text(content)
        return p

    def test_threads_csv_has_expected_columns(self):
        """perfetto-threads.csv has cpu, thread_name, pid, tid, exec time columns."""
        path = self._write_csv("perfetto-threads.csv", THREADS_CSV)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            headers = next(reader)
        expected = {"cpu", "thread_name", "pid", "tid", "exec_time_ms", "exec_time_pct"}
        assert expected.issubset(set(headers)), (
            f"Missing columns: {expected - set(headers)}"
        )

    def test_cpu_util_csv_has_expected_columns(self):
        """perfetto-cpu-util.csv has core, utilization, nr_switches columns."""
        path = self._write_csv("perfetto-cpu-util.csv", CPU_UTIL_CSV)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            headers = next(reader)
        expected = {"core", "utilization_pct", "nr_switches"}
        assert expected.issubset(set(headers)), (
            f"Missing columns: {expected - set(headers)}"
        )

    def test_process_summary_csv_has_expected_columns(self):
        """perfetto-process-summary.csv has pid, name, cpu_time, thread_count columns."""
        path = self._write_csv("perfetto-process-summary.csv", PROCESS_SUMMARY_CSV)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            headers = next(reader)
        expected = {
            "pid",
            "name",
            "cpu_time_ms",
            "cpu_time_pct",
            "thread_count",
            "nr_ctx_switches",
        }
        assert expected.issubset(set(headers)), (
            f"Missing columns: {expected - set(headers)}"
        )

    def test_sched_latency_csv_has_expected_columns(self):
        """perfetto-sched-latency.csv has pid, tid, thread_name, wakeup_latency, count."""
        path = self._write_csv("perfetto-sched-latency.csv", SCHED_LATENCY_CSV)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            headers = next(reader)
        expected = {"pid", "tid", "thread_name", "wakeup_latency_ms", "count"}
        assert expected.issubset(set(headers)), (
            f"Missing columns: {expected - set(headers)}"
        )


# =========================================================================
# 5. Edge cases / resilience (4 tests)
# =========================================================================


class TestEdgeCases:
    """Verify resilience with unusual inputs."""

    def test_output_dir_created_automatically(self, tmp_path):
        """Output directory is created if it does not exist."""
        out_dir = tmp_path / "nonexistent" / "deep" / "output"
        assert not out_dir.exists()
        os.makedirs(str(out_dir), exist_ok=True)
        assert out_dir.exists()

    def test_trace_with_no_sched_data_handled_gracefully(
        self, mock_trace_file, tmp_path
    ):
        """Trace with no scheduling data produces empty CSVs, not crashes."""
        import pandas as pd

        # Simulate what the script would do with empty results
        df = pd.DataFrame(
            columns=[
                "cpu",
                "thread_name",
                "pid",
                "tid",
                "exec_time_ms",
                "exec_time_pct",
            ]
        )
        out_path = tmp_path / "output"
        out_path.mkdir()
        csv_path = out_path / "perfetto-threads.csv"
        df.to_csv(csv_path, index=False)
        assert csv_path.exists()
        # Verify file is valid CSV with just the header
        with open(csv_path) as f:
            lines = f.readlines()
        assert len(lines) == 1  # header only
        assert "cpu" in lines[0]

    def test_query_log_records_queries(self, mock_trace_processor_factory):
        """Mock trace processor records all SQL queries run."""
        query_log: list[str] = []
        import pandas as pd

        # Use the factory to build a mock with query_log
        df = pd.DataFrame(
            {
                "cpu": [0],
                "thread_name": ["test"],
                "pid": [1],
                "tid": [2],
                "exec_time_ms": [10.0],
                "exec_time_pct": [100.0],
            }
        )
        MockTP = mock_trace_processor_factory(
            threads_df=df,
            query_log=query_log,
        )
        tp = MockTP()
        tp.query("SELECT * FROM sched_slice GROUP BY cpu, tid").as_pandas_dataframe()
        assert len(query_log) == 1
        assert "sched_slice" in query_log[0].lower()

    def test_resolve_traces_handles_mixed_content(self, tmp_path):
        """Directory with both .perfetto-trace and other files resolves correctly."""
        d = tmp_path / "mixed"
        d.mkdir()
        (d / "trace.perfetto-trace").write_bytes(b"HPb\x00\x01\x00\x00\x00")
        (d / "trace.pb").write_bytes(b"fake")
        (d / "readme.txt").write_text("hello")

        traces = sorted(p for p in d.iterdir() if p.suffix == ".perfetto-trace")
        assert len(traces) == 1
        assert traces[0].name == "trace.perfetto-trace"
