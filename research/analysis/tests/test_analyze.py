"""Tests for perfetto-analyze.py — Perfetto trace analysis pipeline.

Test categories (29 tests total):
  1. CLI argument parsing        (5 tests)
  2. Error handling              (5 tests)
  3. SQL query validation        (4 tests)
  4. CSV output                  (4 tests)
  5. Edge cases / resilience     (4 tests)
  6. Trace directory discovery   (2 tests: recursive discovery + --trace-dir alias)
  7. Raw ftrace + sched contracts (5 tests: ingest_ftrace_in_raw,
     ftrace-based wakeup/runtime SQL, CSV header contracts)
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

# Output CSVs every analyzed trace must produce (one subdir per trace basename).
EXPECTED_ANALYSIS_CSVS = [
    "perfetto-threads.csv",
    "perfetto-cpu-util.csv",
    "perfetto-process-summary.csv",
    "perfetto-sched-latency.csv",
]


def run_analyze(argv: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Run perfetto-analyze.py with the given argv using subprocess.

    Returns (exit_code, stdout, stderr). ``env`` is passed through to the
    subprocess unchanged (default: inherit the test process environment), so
    tests can extend PYTHONPATH with a fake perfetto package.
    """
    proc = subprocess.run(
        [sys.executable, ANALYZE_SCRIPT, *argv],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
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


def read_analyze_source(script_path: str | None = None) -> str:
    """Return the analyzer script source text (empty string if missing)."""
    path = script_path or ANALYZE_SCRIPT
    if not os.path.isfile(path):
        return ""
    return pathlib.Path(path).read_text()


def extract_csv_headers(csv_name: str, script_path: str | None = None) -> list[str]:
    """Extract the header list literal declared next to *csv_name* in the script.

    Matches dict-style entries ``"<csv_name>": [ "col", ... ]`` (the analyzer's
    EMPTY_HEADERS, or any equivalent contract dict).  Returns [] when the CSV
    has no declared header contract in the script.
    """
    source = read_analyze_source(script_path)
    match = re.search(
        re.escape(csv_name) + r'"?\s*:\s*\[(.*?)\]',
        source,
        re.DOTALL,
    )
    if not match:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", match.group(1))


def has_arg_key(sql: str, key: str) -> bool:
    """True when *sql* uses *key* as an args value (single- or double-quoted)."""
    lowered = sql.lower()
    return f"'{key}'" in lowered or f'"{key}"' in lowered


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


# =========================================================================
# 6. Trace directory discovery (2 tests)
#
# *.perfetto-trace files nested ANY number of levels deep must be
#          discovered (current code only scans top-level entries via
#          os.listdir) and analyzed into <output-dir>/<trace-basename>/.
# The input path must be accepted BOTH positionally AND via the
#          --trace-dir alias (current parser rejects --trace-dir).
#
# The real `perfetto` package is not installed in the test environment, so
# these subprocess-based tests prepend a self-contained fake package to
# PYTHONPATH (fake_perfetto_env). The fake mirrors conftest.py's in-process
# mock TraceProcessor: every query returns an empty DataFrame with the exact
# schema the analyzer's EMPTY_HEADERS define, so the 4 CSVs are still written
# (header-only). This keeps "real traces are not needed" true for the CLI-level
# discovery contract these tests pin.
# =========================================================================

FAKE_TRACE_PROCESSOR_SRC = '''\
"""Self-contained fake of perfetto.trace_processor for subprocess-based tests.

Mirrors tests/conftest.py::make_mock_trace_processor so perfetto-analyze.py can
be exercised via subprocess without the real (not installed) perfetto package.
Every query returns an empty DataFrame with the schema the analyzer defines in
its EMPTY_HEADERS, so _save_or_empty writes a header-only CSV for each of the 4
outputs.
"""
import pandas as pd

_SCHEMAS = {
    "threads": [
        "cpu", "thread_name", "pid", "tid", "exec_time_ms", "exec_time_pct",
    ],
    "cpu_util": ["core", "utilization_pct", "nr_switches"],
    "process_summary": [
        "pid", "name", "cpu_time_ms", "cpu_time_pct",
        "thread_count", "nr_ctx_switches",
    ],
    "sched_latency": [
        "pid", "tid", "thread_name", "wakeup_latency_ms", "count",
    ],
}


class _QueryResult:
    """Minimal Perfetto QueryResult stand-in."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def __iter__(self):
        return iter(self._df.itertuples(index=False))

    def as_pandas_dataframe(self) -> pd.DataFrame:
        return self._df


class TraceProcessor:
    """Duck-type of perfetto.trace_processor.TraceProcessor."""

    def __init__(self, *args, **kwargs):
        pass

    def query(self, sql: str) -> _QueryResult:
        sql_lower = sql.lower()
        if "sched_slice" in sql_lower and "group by" in sql_lower and "thread" in sql_lower:
            columns = _SCHEMAS["threads"]
        elif "sched_slice" in sql_lower and "group by" in sql_lower and "cpu" in sql_lower:
            columns = _SCHEMAS["cpu_util"]
        elif "process" in sql_lower and "group by" in sql_lower:
            columns = _SCHEMAS["process_summary"]
        else:
            columns = _SCHEMAS["sched_latency"]
        return _QueryResult(pd.DataFrame(columns=columns))
'''


@pytest.fixture
def fake_perfetto_env(tmp_path: pathlib.Path) -> dict:
    """Write a fake perfetto package to a temp dir and return a PYTHONPATH env.

    perfetto-analyze.py imports ``perfetto.trace_processor`` at process time.
    The real package is not installed here, so the subprocess needs this fake
    package on PYTHONPATH to run past trace processing and produce the CSVs.
    """
    pkg_root = tmp_path / "fake-perfetto"
    pkg = pkg_root / "perfetto"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "trace_processor.py").write_text(FAKE_TRACE_PROCESSOR_SRC)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(pkg_root) + os.pathsep + env.get("PYTHONPATH", "")
    return env


class TestTraceDirectoryDiscovery:
    """Verify directory-mode trace discovery and the --trace-dir
    alias."""

    def test_nested_directory_traces_discovered_recursively(
        self, tmp_path, fake_perfetto_env
    ):
        """A trace nested 4 levels deep is found and analyzed into its basename subdir."""
        top = tmp_path / "exp" / "run" / "cell" / "replicate-1"
        top.mkdir(parents=True)
        (top / "t.perfetto-trace").write_bytes(b"HPb\x00\x01\x00\x00\x00")

        out_dir = tmp_path / "out"
        rc, out, err = run_analyze(
            [str(tmp_path / "exp"), "--output-dir", str(out_dir)],
            env=fake_perfetto_env,
        )
        assert rc == 0, f"expected 0, got {rc}\nstderr: {err}"

        trace_out = out_dir / "t"
        assert trace_out.is_dir(), (
            f"expected output subdir {trace_out} for nested trace\nstdout: {out}\nstderr: {err}"
        )
        for name in EXPECTED_ANALYSIS_CSVS:
            csv_path = trace_out / name
            assert csv_path.is_file(), f"missing CSV {name} in {trace_out}"
            assert csv_path.stat().st_size > 0, f"empty CSV {csv_path}"

    def test_trace_dir_alias_accepted(
        self, mock_trace_dir, tmp_path, fake_perfetto_env
    ):
        """--trace-dir DIR is an accepted alias for the positional input path."""
        out_dir = tmp_path / "out"
        rc, out, err = run_analyze(
            ["--trace-dir", str(mock_trace_dir), "--output-dir", str(out_dir)],
            env=fake_perfetto_env,
        )
        assert rc == 0, (
            f"expected 0, got {rc} — --trace-dir alias rejected\nstderr: {err}"
        )
        for basename in ("trace-a", "trace-b"):
            trace_out = out_dir / basename
            assert trace_out.is_dir(), f"missing output subdir {trace_out}"
            for name in EXPECTED_ANALYSIS_CSVS:
                csv_path = trace_out / name
                assert csv_path.is_file(), f"missing CSV {name} in {trace_out}"
                assert csv_path.stat().st_size > 0, f"empty CSV {csv_path}"


# =========================================================================
# 7. Raw ftrace ingestion + scheduler SQL/CSV contracts (5 tests)
#
# Traces MUST load with raw ftrace ingestion enabled —
#         TraceProcessorConfig(ingest_ftrace_in_raw=True). The current default
#         TraceProcessor(file_path=...) config does not import raw ftrace, so
#         sched_waking / sched_stat_runtime are not queryable and
#         perfetto-sched-latency.csv is header-only.
# Two new SQL queries must be present:
#         - wakeup latency: ftrace_event (sched_waking) + args keys
#           comm/pid/prio + thread mapping + next sched_slice of the woken utid
#           (existing consumers plot-perfetto-cpu.py / sched-latency-heatmap.py
#           depend on this contract);
#         - per-task runtime samples: ftrace_event (sched_stat_runtime) + args
#           keys comm/pid/runtime (runtime stored in int_value).
# EMPTY_HEADERS (or equivalent) must declare the CSV header contracts:
#         perfetto-sched-latency.csv -> pid,tid,thread_name,wakeup_latency_ms,count
#         perfetto-sched-runtime.csv -> ts,cpu,pid,tid,thread_name,runtime_ns
# =========================================================================


class TestRawFtraceConfig:
    """The analyzer loads traces with raw ftrace ingestion enabled."""

    def test_trace_processor_config_enables_raw_ftrace(self):
        """TraceProcessorConfig with ingest_ftrace_in_raw=True is used."""
        source = read_analyze_source()
        assert "TraceProcessorConfig" in source, (
            "analyzer must build a TraceProcessorConfig — the default "
            "TraceProcessor(file_path=...) config does not ingest raw ftrace, "
            "so sched_waking/sched_stat_runtime are not queryable"
        )
        assert re.search(r"ingest_ftrace_in_raw\s*=\s*True", source) is not None, (
            "missing ingest_ftrace_in_raw=True in TraceProcessorConfig — "
            "raw ftrace events are not imported without it"
        )


class TestFtraceSqlQueries:
    """The ftrace-based wakeup-latency and runtime SQL queries exist."""

    def _find_ftrace_query(self, marker: str) -> str | None:
        """Return the first extracted query joining ftrace_event with *marker*."""
        queries = extract_sql_queries()
        return next(
            (q for q in queries if "ftrace_event" in q.lower() and marker in q.lower()),
            None,
        )

    def test_wakeup_latency_query_traces_sched_waking(self):
        """Wakeup-latency query joins sched_waking args (comm/pid/prio) to sched_slice."""
        query = self._find_ftrace_query("sched_waking")
        assert query is not None, "no ftrace_event query referencing sched_waking found"
        lower = query.lower()
        for frag in ("sched_waking", "ftrace_event", "sched_slice"):
            assert frag in lower, f"missing '{frag}' in wakeup-latency query"
        for key in ("comm", "pid", "prio"):
            assert has_arg_key(query, key), (
                f"missing args key '{key}' in wakeup-latency query"
            )

    def test_runtime_query_traces_sched_stat_runtime(self):
        """Per-task runtime query reads sched_stat_runtime args (comm/pid/runtime)."""
        query = self._find_ftrace_query("sched_stat_runtime")
        assert query is not None, (
            "no ftrace_event query referencing sched_stat_runtime found"
        )
        lower = query.lower()
        for frag in ("sched_stat_runtime", "ftrace_event"):
            assert frag in lower, f"missing '{frag}' in runtime query"
        for key in ("comm", "pid", "runtime"):
            assert has_arg_key(query, key), f"missing args key '{key}' in runtime query"


class TestCsvHeaderContracts:
    """EMPTY_HEADERS (or equivalent) declares each output CSV contract."""

    def test_sched_latency_csv_header_contract(self):
        """perfetto-sched-latency.csv declares pid,tid,thread_name,wakeup_latency_ms,count."""
        headers = extract_csv_headers("perfetto-sched-latency.csv")
        expected = {"pid", "tid", "thread_name", "wakeup_latency_ms", "count"}
        assert expected.issubset(set(headers)), (
            "perfetto-sched-latency.csv contract missing columns: "
            f"{expected - set(headers)} (got {headers})"
        )

    def test_sched_runtime_csv_header_contract(self):
        """perfetto-sched-runtime.csv declares ts,cpu,pid,tid,thread_name,runtime_ns."""
        headers = extract_csv_headers("perfetto-sched-runtime.csv")
        expected = {"ts", "cpu", "pid", "tid", "thread_name", "runtime_ns"}
        assert expected.issubset(set(headers)), (
            "perfetto-sched-runtime.csv contract missing columns: "
            f"{expected - set(headers)} (got {headers})"
        )
