"""Tests for the streaming/chunked dist-analyze behavior.

The streaming rework changes `dist-analyze.py` so the guard window is processed in
time-chunked windows (a new `--chunk-s` flag, default 5 seconds) instead of
materializing the full window per replicate, with per-replicate CSVs written
incrementally and the merged `dist-slices.csv` regenerated from those files.
The outputs MUST stay byte-identical to the reference (non-chunked) run.

This module pins the NEW streaming API surface (RED against the current
implementation, which has no `--chunk-s` flag and no chunk-window / merge
helpers):

    research/analysis/dist-analyze.py  (module: dist_analyze)

    New CLI flag:
      --chunk-s <seconds>   chunk width for time-chunked processing
                            (optional, default 5)

    New pure helpers:
      chunk_windows(start_us, end_us, chunk_s=5.0) -> list[tuple[int, int]]
          Deterministic partition of the guard window [start_us, end_us)
          into consecutive half-open windows `chunk_s` seconds wide (the
          last window may be shorter).  Empty window -> [].

      merge_per_replicate_slices(cell_out: Path) -> pd.DataFrame
          Read every dist-slices-replicate-<n>.csv in cell_out (sorted by
          filename), concat + sort_values(["ts_start_us", "tid"],
          kind="mergesort"), return the merged DataFrame (written by main()
          to dist-slices.csv).  No files -> empty DataFrame with the pinned
          SLICES_COLUMNS schema.

Pinned behaviors (all exercised through the CLI on synthetic fixtures):

  a. A chunked run (--chunk-s 5) produces BYTE-IDENTICAL outputs vs the
     reference run (no --chunk-s) on the same small fixture:
     dist-slices.csv, dist-runtime.csv, dist-summary.csv,
     dist-percentiles.json AND every per-replicate
     dist-slices-replicate-<n>.csv.
  b. The merged dist-slices.csv equals the union (partition) of the
     per-replicate files: row-count equality + merged == concat+sort of the
     per-replicate rows, byte-for-byte when read back.
  c. A multi-chunk fixture (the guard window spanning several chunks, with
     slices exactly on chunk boundaries) proves the chunk boundary does not
     affect stats (p50/p95/p99) or outputs, across several chunk widths.
  d. Determinism: a chunked rerun is byte-identical.

Existing behavior is NOT touched: test_dist_analyze.py (74 tests) is
unchanged and must stay green.

The subprocess runs use a window-FILTERING fake perfetto package (this
module's own shim, not the conftest one): the real trace_processor honors the
SQL guard window, so a chunked implementation (one query per chunk) must
receive only that chunk's rows.  The conftest fake returns the full canned
dataset for every sched_slice query, which would wrongly duplicate rows when
the implementation re-queries per chunk.

Run from research/analysis:
    python3 -m pytest tests/test_dist_analyze_streaming.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    DIST_3POD_RUNTIME,
    DIST_3POD_SLICES,
    DIST_FAKE_BOUNDS,
    DIST_SLICES_COLUMNS,
    DIST_SUMMARY_COLUMNS,
    build_dist_cell,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_SCRIPT = ANALYSIS_DIR / "dist-analyze.py"

# Default guard window implied by DIST_FAKE_BOUNDS (first 2.0s, last 88.0s,
# 2s guard at each end) -> [4_000_000, 86_000_000) microseconds.
DEFAULT_WINDOW = (4_000_000, 86_000_000)
DEFAULT_CHUNK_S = 5.0
CHUNK_W_US = int(DEFAULT_CHUNK_S * 1_000_000)

# Expected per-cell output set (pinned + the per-replicate slice files).
CELL_OUTPUT_FILES = (
    "dist-slices.csv",
    "dist-runtime.csv",
    "dist-summary.csv",
    "dist-percentiles.json",
)


# =========================================================================
# Helpers
# =========================================================================


def load_dist_analyze_module():
    """Import dist-analyze.py so the pinned streaming names are callable."""
    spec = importlib.util.spec_from_file_location("dist_analyze_streaming", DIST_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dist(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run dist-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(DIST_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_analyze(
    data_dir: pathlib.Path,
    out_dir: pathlib.Path,
    env: dict[str, str],
    *,
    family: str,
    workload: str,
    chunk_s: float | None = None,
    duration: float | None = None,
) -> tuple[int, str]:
    """Run dist-analyze with the pinned CLI; returns (rc, stderr)."""
    argv = [
        "--data-dir",
        str(data_dir),
        "--output-dir",
        str(out_dir),
        "--family",
        family,
        "--workload",
        workload,
    ]
    if chunk_s is not None:
        argv += ["--chunk-s", str(chunk_s)]
    if duration is not None:
        argv += ["--duration", str(duration)]
    rc, _out, err = run_dist(argv, env)
    return rc, err


def sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root* (missing -> {})."""
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def streaming_fake_env(
    tmp_path: pathlib.Path,
    slices_rows: list[dict],
    runtime_rows: list[dict],
    bounds: dict | None = None,
) -> tuple[dict, pathlib.Path]:
    """Write a window-filtering fake perfetto package + canned data.

    The fake honors the SQL guard window (`ts >= <start> * 1000 AND
    ts < <end> * 1000`) so a chunked implementation receives exactly the
    rows of each chunk — the same contract the real trace_processor gives.
    Returns (env, data_path); subprocess runs must pass env.
    """
    pkg_root = tmp_path / "fake-perfetto-streaming"
    pkg = pkg_root / "perfetto"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "trace_processor.py").write_text(_streaming_fake_trace_processor_source())

    data_path = tmp_path / "streaming-fake-data.json"
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
        "PYTHONPATH": str(pkg_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    return env, data_path


def _streaming_fake_trace_processor_source() -> str:
    """Source of the window-filtering fake perfetto.trace_processor package.

    Same canned-data protocol as the conftest fake (DIST_FAKE_DATA JSON) but
    the sched_slice / sched_stat_runtime branches parse the SQL guard window
    (`ts >= <start> * 1000 AND ts < <end> * 1000`) and return only the rows
    inside it — matching the real trace_processor.  The bounds MIN/MAX query
    is served unfiltered.
    """
    return r"""
import json
import os
import re

import pandas as pd

_WIN_GE = re.compile(r"(?:s\.ts|r\.ts)\s*>=\s*(\d+)")
_WIN_LT = re.compile(r"(?:s\.ts|r\.ts)\s*<\s*(\d+)")


def _load():
    with open(os.environ["DIST_FAKE_DATA"]) as f:
        return json.load(f)


def _window(sql_lower):
    # The pinned queries format the window as '<ts> >= {start} * 1000 AND
    # <ts> < {end} * 1000' (start/end in us, literal in ns).  If no window is
    # present the whole canned dataset is served (unfiltered).
    ge = _WIN_GE.search(sql_lower)
    lt = _WIN_LT.search(sql_lower)
    if ge is None or lt is None:
        return None
    return int(ge.group(1)) // 1000, int(lt.group(1)) // 1000


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
            df = pd.DataFrame(data.get("runtime", []))
            win = _window(sql_lower)
            if win is not None and not df.empty:
                start, end = win
                df = df[(df["ts"] >= start) & (df["ts"] < end)]
            return QueryResult(df.reset_index(drop=True))
        if "sched_slice" in sql_lower and "min(ts)" in sql_lower:
            return QueryResult(
                pd.DataFrame([data.get("bounds", {"first_ts_ns": 0, "last_ts_ns": 0})])
            )
        if "sched_slice" in sql_lower:
            df = pd.DataFrame(data.get("slices", []))
            win = _window(sql_lower)
            if win is not None and not df.empty:
                start, end = win
                df = df[(df["ts_start_us"] >= start) & (df["ts_start_us"] < end)]
            return QueryResult(df.reset_index(drop=True))
        return QueryResult(pd.DataFrame())
"""


def partition_bytes(cell_out: pathlib.Path) -> bytes:
    """Serialize merged == concat+sort of the per-replicate slice files.

    Reads every dist-slices-replicate-<n>.csv (sorted by filename — the same
    deterministic replicate order the analyzer concatenates in), concats,
    sorts by (ts_start_us, tid) with mergesort (the pinned key/kind), and
    serializes with pandas to_csv(index=False).  Byte-for-byte comparable to
    the analyzer's dist-slices.csv.
    """
    files = sorted(cell_out.glob("dist-slices-replicate-*.csv"))
    frames = [pd.read_csv(path) for path in files]
    merged = pd.concat(frames, ignore_index=True).sort_values(
        ["ts_start_us", "tid"], kind="mergesort"
    )
    buf = io.StringIO()
    merged.to_csv(buf, index=False)
    return buf.getvalue().encode()


def read_summary_percentiles(
    cell_out: pathlib.Path,
) -> dict[str, tuple[float, float, float]]:
    """Return {pod: (p50, p95, p99)} from a cell's dist-summary.csv."""
    summary = pd.read_csv(cell_out / "dist-summary.csv")
    out: dict[str, tuple[float, float, float]] = {}
    for pod, p50, p95, p99 in zip(
        summary["pod"], summary["p50_us"], summary["p95_us"], summary["p99_us"]
    ):
        out[str(pod)] = (float(p50), float(p95), float(p99))
    return out


# =========================================================================
# Multi-chunk fixture — guard window [4s, 60s), slices every 1s with the
# 5s chunk boundaries (4s/9s/14s/...) falling EXACTLY on slice starts.
# =========================================================================

MULTI_CHUNK_BOUNDS = {"first_ts_ns": 2_000_000_000, "last_ts_ns": 62_000_000_000}
MULTI_CHUNK_WINDOW = (4_000_000, 60_000_000)
# 29 slices (was 56).  The pinned pandas linear-interpolation
# quantile evaluates p95 == p99 == 100.0 for the 10..100 us cycle at every
# count >= 30 (the 100-us block occupies the top ~9% of the sorted
# distribution), so `p50 < p95 < p99` is unsatisfiable there.  29 is the
# largest count that keeps the fixture non-degenerate (p50=50, p95=96,
# p99=100) AND keeps the 5s chunk boundaries on slice starts.
MULTI_CHUNK_SLICE_COUNT = 29  # ts 4_000_000..32_000_000 at 1s intervals


def multi_chunk_slices_rows() -> list[dict]:
    """29 slices at 1s intervals 4.0s..32.0s; durations cycle 10..100 us."""
    rows: list[dict] = []
    for i in range(MULTI_CHUNK_SLICE_COUNT):
        ts = 4_000_000 + i * 1_000_000
        dur = ((i % 10) + 1) * 10
        rows.append(
            {
                "ts_start_us": ts,
                "ts_end_us": ts + dur,
                "duration_us": dur,
                "cpu": 0,
                "tid": 1001,
                "thread_name": "stress-ng-cpu",
            }
        )
    return rows


def multi_chunk_runtime_rows() -> list[dict]:
    """28 runtime samples at 2s intervals 4.0s..58.0s (all inside the window)."""
    return [
        {
            "ts": 4_000_000 + i * 2_000_000,
            "cpu": 0,
            "pid": 1001,
            "tid": 1001,
            "thread_name": "stress-ng-cpu",
            "runtime_ns": 1_000_000,
        }
        for i in range(28)
    ]


def build_multi_chunk_cell_dir(root: pathlib.Path) -> pathlib.Path:
    """Single-pod single-replicate cell for the multi-chunk fixture."""
    return build_dist_cell(
        root,
        "multi-chunk-cell",
        pod="stress-ng",
        pids=[1001],
        nr_periods=1000,
        nr_throttled=10,
        cpu_weight=59,
        cpu_max_quota=200000,
    )


# =========================================================================
# New module surface (RED: helpers do not exist yet)
# =========================================================================


class TestModuleStreamingContract:
    """The streaming helpers are part of the pinned dist-analyze API."""

    def test_module_exposes_chunk_windows(self):
        module = load_dist_analyze_module()
        assert callable(getattr(module, "chunk_windows", None)), (
            "missing pinned streaming helper: chunk_windows"
        )

    def test_module_exposes_merge_per_replicate_slices(self):
        module = load_dist_analyze_module()
        assert callable(getattr(module, "merge_per_replicate_slices", None)), (
            "missing pinned streaming helper: merge_per_replicate_slices"
        )


# =========================================================================
# chunk_windows — the chunk boundary math
# =========================================================================


class TestChunkWindows:
    """chunk_windows partitions [start, end) into chunk_s-wide windows."""

    def test_partition_covers_window_exactly(self):
        module = load_dist_analyze_module()
        start, end = 4_000_000, 60_000_000
        windows = module.chunk_windows(start, end, DEFAULT_CHUNK_S)
        assert windows, "expected at least one chunk"
        assert windows[0][0] == start
        assert windows[-1][1] == end
        # Contiguous and non-overlapping (half-open intervals).
        for (a, b), (c, _d) in zip(windows, windows[1:]):
            assert b == c, "chunk windows must be contiguous"
        # Every window except possibly the last is exactly chunk_s wide.
        for a, b in windows[:-1]:
            assert b - a == CHUNK_W_US, "non-final chunk must be chunk_s wide"
        assert windows[-1][1] - windows[-1][0] <= CHUNK_W_US
        assert all(b > a for a, b in windows), "windows must be non-empty"

    def test_first_window_starts_at_start(self):
        module = load_dist_analyze_module()
        start, end = DEFAULT_WINDOW
        windows = module.chunk_windows(start, end, DEFAULT_CHUNK_S)
        assert windows[0] == (start, start + CHUNK_W_US)

    def test_empty_window_returns_empty_list(self):
        module = load_dist_analyze_module()
        assert module.chunk_windows(0, 0, DEFAULT_CHUNK_S) == []
        assert module.chunk_windows(10, 10, DEFAULT_CHUNK_S) == []
        # A trace shorter than 2*guard yields start == end (retained_window).
        start, end = module.retained_window(0, 1_000_000_000, 2.0)
        assert module.chunk_windows(start, end, DEFAULT_CHUNK_S) == []

    def test_single_chunk_when_chunk_exceeds_window(self):
        module = load_dist_analyze_module()
        start, end = 4_000_000, 6_000_000  # 2s window < 5s chunk
        assert module.chunk_windows(start, end, DEFAULT_CHUNK_S) == [(start, end)]

    def test_last_chunk_is_shorter_for_non_aligned_window(self):
        # Windows begin at start_us and are contiguous; the last
        # window is [start + k*chunk, end) when end is not aligned.  For
        # [4_000_000, 60_500_000) the 5s boundaries are 9M/14M/.../59M and
        # the tail window is (59_000_000, 60_500_000) — a 1.5s tail (the
        # 60M boundary is NOT on the 5s grid from 4M: 4M + k*5M == 60M has
        # no integer solution, so a 0.5s tail was a stale expectation).
        module = load_dist_analyze_module()
        start, end = 4_000_000, 60_500_000  # 56.5s -> 1.5s tail
        windows = module.chunk_windows(start, end, DEFAULT_CHUNK_S)
        assert windows[-1][1] == end
        assert windows[-1] == (59_000_000, 60_500_000)
        assert windows[-1][1] - windows[-1][0] == 1_500_000

    def test_non_aligned_start_is_partitioned(self):
        module = load_dist_analyze_module()
        start, end = 3_700_000, 15_000_000
        windows = module.chunk_windows(start, end, DEFAULT_CHUNK_S)
        assert windows[0] == (3_700_000, 8_700_000)
        assert windows[-1][1] == end

    def test_deterministic(self):
        module = load_dist_analyze_module()
        start, end = 4_000_000, 60_000_000
        assert module.chunk_windows(
            start, end, DEFAULT_CHUNK_S
        ) == module.chunk_windows(start, end, DEFAULT_CHUNK_S)

    def test_default_chunk_is_five_seconds(self):
        module = load_dist_analyze_module()
        assert module.chunk_windows(0, 10_000_000) == [
            (0, 5_000_000),
            (5_000_000, 10_000_000),
        ]


# =========================================================================
# --chunk-s CLI flag (RED: the flag does not exist yet)
# =========================================================================


class TestChunkCliFlag:
    """--chunk-s is an optional flag, default 5 seconds, same CLI otherwise."""

    def test_help_lists_chunk_s(self):
        rc, out, err = run_dist(["--help"], env={})
        assert rc == 0, f"stderr: {err}"
        assert "--chunk-s" in out + err

    def test_default_chunk_s_is_five(self):
        # parse_args([]) always SystemExits because the pinned
        # --data-dir/--output-dir/--family flags are required (pinned by the
        # 74-test test_missing_required_flags_exits_nonzero).  Invoke the
        # parser with the required flags present to reach the --chunk-s
        # default and assert it is 5.0.
        module = load_dist_analyze_module()
        parser = module.build_parser()
        args = parser.parse_args(
            ["--data-dir", "d", "--output-dir", "o", "--family", "f"]
        )
        assert float(args.chunk_s) == 5.0

    def test_chunk_s_accepts_float_value(
        self, dist_three_pod_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        rc, err = run_analyze(
            dist_three_pod_cell_dir,
            tmp_path / "out",
            env,
            family="dist-family",
            workload="stress-ng",
            chunk_s=2.5,
        )
        assert rc == 0, f"chunked run failed: {err}"

    def test_chunk_s_non_numeric_exits_nonzero(self):
        rc, _out, err = run_dist(["--chunk-s", "abc"], env={})
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()


# =========================================================================
# Byte-identity: chunked run == reference run
# =========================================================================


class TestChunkedByteIdentity:
    """A chunked run is byte-identical to the reference (no --chunk-s) run."""

    def _assert_pair_identical(
        self,
        data_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        env: dict[str, str],
        *,
        family: str,
        workload: str,
        chunk_s: float | None,
        cell_rel: str,
        duration: float | None = None,
    ) -> None:
        ref_out = tmp_path / "ref"
        chunk_out = tmp_path / "chunk"
        rc_ref, err_ref = run_analyze(
            data_dir, ref_out, env, family=family, workload=workload, duration=duration
        )
        assert rc_ref == 0, f"reference run failed: {err_ref}"
        rc_chunk, err_chunk = run_analyze(
            data_dir,
            chunk_out,
            env,
            family=family,
            workload=workload,
            chunk_s=chunk_s,
            duration=duration,
        )
        assert rc_chunk == 0, f"chunked run (--chunk-s {chunk_s}) failed: {err_chunk}"
        m_ref = sha256_manifest(ref_out / "distribution")
        m_chunk = sha256_manifest(chunk_out / "distribution")
        assert m_ref == m_chunk, (
            f"chunked output differs from reference with --chunk-s {chunk_s}"
        )
        # The byte-identical tree includes every per-replicate slice file.
        expected = set(CELL_OUTPUT_FILES) | {"dist-slices-replicate-1.csv"}
        assert set(m_ref) == {f"{cell_rel}/{name}" for name in expected}

    def test_chunked_matches_reference_byte_identical(
        self, dist_three_pod_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        self._assert_pair_identical(
            dist_three_pod_cell_dir,
            tmp_path,
            env,
            family="dist-family",
            workload="stress-ng",
            chunk_s=DEFAULT_CHUNK_S,
            cell_rel="dist-family/co-located-a-b-c",
        )

    def test_chunked_matches_reference_across_chunk_sizes(
        self, dist_three_pod_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """1s (82 chunks), 2.5s and 1000s (single chunk) all match reference."""
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        for chunk_s in (1, 2.5, 1000):
            self._assert_pair_identical(
                dist_three_pod_cell_dir,
                tmp_path / f"case-{chunk_s}",
                env,
                family="dist-family",
                workload="stress-ng",
                chunk_s=chunk_s,
                cell_rel="dist-family/co-located-a-b-c",
            )


# =========================================================================
# Multi-chunk fixture: the chunk boundary must not change stats/outputs
# (requirement c)
# =========================================================================


class TestMultiChunkFixture:
    """Slices exactly on 5s chunk boundaries; stats and outputs are stable."""

    @pytest.fixture
    def multi_chunk_data_dir(self, tmp_path: pathlib.Path) -> pathlib.Path:
        return build_multi_chunk_cell_dir(tmp_path / "multi-chunk-data")

    def _env(self, tmp_path: pathlib.Path) -> dict[str, str]:
        env, _ = streaming_fake_env(
            tmp_path,
            multi_chunk_slices_rows(),
            multi_chunk_runtime_rows(),
            MULTI_CHUNK_BOUNDS,
        )
        return env

    def test_chunk_boundary_does_not_change_stats(
        self, multi_chunk_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env = self._env(tmp_path)
        ref_out = tmp_path / "ref"
        rc_ref, err_ref = run_analyze(
            multi_chunk_data_dir,
            ref_out,
            env,
            family="dist-stress-ng",
            workload="stress-ng",
            duration=60,
        )
        assert rc_ref == 0, f"reference run failed: {err_ref}"
        ref_cell = ref_out / "distribution" / "dist-stress-ng" / "multi-chunk-cell"
        ref_percentiles = read_summary_percentiles(ref_cell)
        p50, p95, p99 = ref_percentiles["stress-ng"]
        assert p50 < p95 < p99, "fixture distribution must be non-degenerate"

        for chunk_s in (1, 5, 1000):
            chunk_out = tmp_path / f"chunk-{chunk_s}"
            rc, err = run_analyze(
                multi_chunk_data_dir,
                chunk_out,
                env,
                family="dist-stress-ng",
                workload="stress-ng",
                chunk_s=chunk_s,
                duration=60,
            )
            assert rc == 0, f"chunked run (--chunk-s {chunk_s}) failed: {err}"
            m_ref = sha256_manifest(ref_out / "distribution")
            m_chunk = sha256_manifest(chunk_out / "distribution")
            assert m_ref == m_chunk, (
                f"outputs differ from reference at --chunk-s {chunk_s}"
            )
            chunk_cell = (
                chunk_out / "distribution" / "dist-stress-ng" / "multi-chunk-cell"
            )
            assert read_summary_percentiles(chunk_cell) == ref_percentiles, (
                f"p50/p95/p99 changed at --chunk-s {chunk_s}"
            )

    def test_chunked_slice_count_preserved(
        self, multi_chunk_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """No chunk-boundary duplication or loss: every slice appears once."""
        env = self._env(tmp_path)
        ref_out = tmp_path / "ref"
        rc_ref, err_ref = run_analyze(
            multi_chunk_data_dir,
            ref_out,
            env,
            family="dist-stress-ng",
            workload="stress-ng",
            duration=60,
        )
        assert rc_ref == 0, f"reference run failed: {err_ref}"
        ref_cell = ref_out / "distribution" / "dist-stress-ng" / "multi-chunk-cell"
        ref_count = len(pd.read_csv(ref_cell / "dist-slices.csv"))

        for chunk_s in (1, 5):
            chunk_out = tmp_path / f"count-{chunk_s}"
            rc, err = run_analyze(
                multi_chunk_data_dir,
                chunk_out,
                env,
                family="dist-stress-ng",
                workload="stress-ng",
                chunk_s=chunk_s,
                duration=60,
            )
            assert rc == 0, f"chunked run (--chunk-s {chunk_s}) failed: {err}"
            chunk_cell = (
                chunk_out / "distribution" / "dist-stress-ng" / "multi-chunk-cell"
            )
            slices = pd.read_csv(chunk_cell / "dist-slices.csv")
            assert len(slices) == ref_count == MULTI_CHUNK_SLICE_COUNT, (
                f"slice count changed at --chunk-s {chunk_s}: "
                f"{len(slices)} != {ref_count}"
            )
            # Single-replicate cell: the merged file is one per-replicate file.
            rep_count = len(pd.read_csv(chunk_cell / "dist-slices-replicate-1.csv"))
            assert rep_count == ref_count


# =========================================================================
# Merged dist-slices.csv == union (partition) of the per-replicate files
# (requirement b)
# =========================================================================


class TestMergedPerReplicatePartition:
    """The merged file is exactly the concat+sort of the per-replicate files."""

    def test_merged_is_partition_of_per_replicate_single_rep(
        self, dist_three_pod_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        out_dir = tmp_path / "out"
        rc, err = run_analyze(
            dist_three_pod_cell_dir,
            out_dir,
            env,
            family="dist-family",
            workload="stress-ng",
        )
        assert rc == 0, f"run failed: {err}"
        cell_out = out_dir / "distribution" / "dist-family" / "co-located-a-b-c"
        merged = pd.read_csv(cell_out / "dist-slices.csv")
        rep1 = pd.read_csv(cell_out / "dist-slices-replicate-1.csv")
        assert len(merged) == len(rep1), "merged row count != per-replicate row count"
        assert (cell_out / "dist-slices.csv").read_bytes() == partition_bytes(cell_out)

    def test_merged_partition_two_replicates(
        self, dist_two_replicate_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        out_dir = tmp_path / "out"
        rc, err = run_analyze(
            dist_two_replicate_cell_dir,
            out_dir,
            env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc == 0, f"run failed: {err}"
        cell_out = (
            out_dir / "distribution" / "dist-stress-ng" / "request=100m-limit=100m"
        )
        merged = pd.read_csv(cell_out / "dist-slices.csv")
        rep_files = sorted(cell_out.glob("dist-slices-replicate-*.csv"))
        assert len(rep_files) == 2
        assert len(merged) == sum(len(pd.read_csv(path)) for path in rep_files), (
            "merged row count != sum of per-replicate row counts"
        )
        assert (cell_out / "dist-slices.csv").read_bytes() == partition_bytes(cell_out)

    def test_merged_partition_holds_on_chunked_run(
        self, dist_two_replicate_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        out_dir = tmp_path / "out"
        rc, err = run_analyze(
            dist_two_replicate_cell_dir,
            out_dir,
            env,
            family="dist-stress-ng",
            workload="stress-ng",
            chunk_s=DEFAULT_CHUNK_S,
        )
        assert rc == 0, f"chunked run failed: {err}"
        cell_out = (
            out_dir / "distribution" / "dist-stress-ng" / "request=100m-limit=100m"
        )
        merged = pd.read_csv(cell_out / "dist-slices.csv")
        rep_files = sorted(cell_out.glob("dist-slices-replicate-*.csv"))
        assert len(merged) == sum(len(pd.read_csv(path)) for path in rep_files), (
            "chunked merged row count != sum of per-replicate row counts"
        )
        assert (cell_out / "dist-slices.csv").read_bytes() == partition_bytes(cell_out)


# =========================================================================
# Determinism of the chunked path
# =========================================================================


class TestChunkedDeterminism:
    """Two chunked runs on the same staged data are byte-identical."""

    def test_chunked_rerun_byte_identical(
        self, dist_three_pod_cell_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        env, _ = streaming_fake_env(tmp_path, DIST_3POD_SLICES, DIST_3POD_RUNTIME)
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        rc1, err1 = run_analyze(
            dist_three_pod_cell_dir,
            out1,
            env,
            family="dist-family",
            workload="stress-ng",
            chunk_s=DEFAULT_CHUNK_S,
        )
        assert rc1 == 0, f"first chunked run failed: {err1}"
        rc2, err2 = run_analyze(
            dist_three_pod_cell_dir,
            out2,
            env,
            family="dist-family",
            workload="stress-ng",
            chunk_s=DEFAULT_CHUNK_S,
        )
        assert rc2 == 0, f"second chunked run failed: {err2}"
        m1 = sha256_manifest(out1 / "distribution")
        m2 = sha256_manifest(out2 / "distribution")
        assert m1 == m2, "chunked rerun is not byte-identical"


# =========================================================================
# merge_per_replicate_slices — the merged-file regeneration helper
# =========================================================================


class TestMergePerReplicateSlices:
    """The helper that rebuilds dist-slices.csv from the per-replicate files."""

    @staticmethod
    def _write_rep_files(cell_out: pathlib.Path) -> tuple[pd.DataFrame, pd.DataFrame]:
        rows1 = [
            (10.0, 110.0, 100.0, 0, 101, "stress-ng-cpu", "a"),
            (30.0, 230.0, 200.0, 1, 201, "stress-ng-cpu", "b"),
        ]
        rows2 = [
            (20.0, 170.0, 150.0, 0, 101, "stress-ng-cpu", "a"),
            (40.0, 290.0, 250.0, 1, 201, "stress-ng-cpu", "b"),
        ]
        df1 = pd.DataFrame(  # type: ignore
            rows1,
            columns=DIST_SLICES_COLUMNS,  # type: ignore
        )
        df2 = pd.DataFrame(  # type: ignore
            rows2,
            columns=DIST_SLICES_COLUMNS,  # type: ignore
        )
        df1.to_csv(cell_out / "dist-slices-replicate-1.csv", index=False)
        df2.to_csv(cell_out / "dist-slices-replicate-2.csv", index=False)
        return df1, df2

    def test_returns_concat_sorted_of_per_replicate_files(self, tmp_path: pathlib.Path):
        module = load_dist_analyze_module()
        cell_out = tmp_path / "cell"
        cell_out.mkdir()
        df1, df2 = self._write_rep_files(cell_out)
        merged = module.merge_per_replicate_slices(cell_out)
        expected = pd.concat([df1, df2], ignore_index=True).sort_values(
            ["ts_start_us", "tid"], kind="mergesort"
        )
        assert list(merged.columns) == DIST_SLICES_COLUMNS
        pd.testing.assert_frame_equal(merged, expected)

    def test_serializes_byte_identically_to_dist_slices(self, tmp_path: pathlib.Path):
        module = load_dist_analyze_module()
        cell_out = tmp_path / "cell"
        cell_out.mkdir()
        df1, df2 = self._write_rep_files(cell_out)
        merged = module.merge_per_replicate_slices(cell_out)
        expected = pd.concat([df1, df2], ignore_index=True).sort_values(
            ["ts_start_us", "tid"], kind="mergesort"
        )
        buf = io.StringIO()
        merged.to_csv(buf, index=False)
        exp_buf = io.StringIO()
        expected.to_csv(exp_buf, index=False)
        assert buf.getvalue() == exp_buf.getvalue()

    def test_empty_cell_returns_pinned_empty_schema(self, tmp_path: pathlib.Path):
        module = load_dist_analyze_module()
        cell_out = tmp_path / "empty"
        cell_out.mkdir()
        merged = module.merge_per_replicate_slices(cell_out)
        assert list(merged.columns) == DIST_SLICES_COLUMNS
        assert len(merged) == 0
