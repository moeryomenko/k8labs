"""Tests for dist-analyze.py — EEVDF CPU execution-time distribution pipeline.

Test-first design, red until the engineer implements the script.
The module/function/CLI names used here are the contract the implementation must build:

    research/analysis/dist-analyze.py  (module: dist_analyze)

    Constants:
      SLICES_COLUMNS       ts_start_us,ts_end_us,duration_us,cpu,tid,
                           thread_name,pod
      RUNTIME_COLUMNS      ts,cpu,pid,tid,thread_name,pod,runtime_ns
      SUMMARY_COLUMNS      cell,replicate,pod,slice_count,total_exec_ms,mean_us,
                           median_us,p50_us,p95_us,p99_us,max_us,throttle_ratio,
                           cpu_weight,cpu_max,quality
      PERCENTILE_STEPS     (1,11,21,31,41,51,61,71,81,91,99)  # 1..99, deciles
      COVERAGE_THRESHOLD   0.80
      SYSTEM_POD           "system"
      QUERY_SLICES         SQL: per-slice rows from sched_slice (guard window)
      QUERY_RUNTIME        SQL: sched_stat_runtime samples (guard window)

    Pure core (testable with synthetic CSV inputs, no trace):
      load_pid_map(cell_dir: Path) -> dict[int, str]
      assign_pods(slices_df, pid_map) -> pd.DataFrame
      assign_runtime_pods(runtime_df, pid_map) -> pd.DataFrame
      compute_stats(durations_us) -> dict[str, float]
      compute_percentiles(durations_us) -> dict[str, float]
      compute_throttle_ratio(cgroup_csv: Path) -> float
      compute_cpu_limits(cgroup_csv: Path) -> tuple[int, int]
      compute_coverage(first_ts_us, last_ts_us, duration_s) -> float
      quality_for(coverage: float) -> str
      retained_window(first_ts_ns, last_ts_ns, guard_s=2.0) -> tuple[int, int]
      parse_request_limit(cell: str) -> tuple[int | None, int | None]
      sanity_check(summary_df, workload_by_cell) -> list[str]

    Thin trace layer (exercised with a mock TraceProcessor):
      load_trace(trace_path: Path)
      trace_event_bounds(tp) -> tuple[int, int]
      query_slices(tp, window_start_us, window_end_us) -> pd.DataFrame
      query_runtime(tp, window_start_us, window_end_us) -> pd.DataFrame

    main(argv: list[str] | None = None) -> int

CLI: --data-dir <dir> --output-dir <dir> --family <name>
     [--workload stress-ng|cpu-burner|api-server|db-simulator] [--duration 90]
Writes output/distribution/<family>/<cell>/{dist-slices,dist-runtime,
dist-summary}.csv + dist-percentiles.json. Exits non-zero when the
sanity gate reports violated facts (each listed on stderr).

Covered behavior:
  slice extraction + summary/percentile column contracts
  pid->pod mapping, three-pod identical-thread-name case
  sanity gate: saturating/throttled, light cpu-burner, monotonicity
  determinism: byte-identical reruns on staged data
  coverage quality gate: good vs degraded
  output layout under output/distribution/<family>/<cell>/

Run from research/analysis:
    python3 -m pytest tests/test_dist_analyze.py -q
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    DIST_3POD_RUNTIME,
    DIST_3POD_SLICES,
    DIST_COVERAGE_THRESHOLD,
    DIST_DEGRADED_SLICES,
    DIST_PERCENTILE_STEPS,
    DIST_RUNTIME_COLUMNS,
    DIST_SANITY_SLICES,
    DIST_SLICES_COLUMNS,
    DIST_SUMMARY_COLUMNS,
    DIST_SYSTEM_POD,
    write_dist_cgroup_csv,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_SCRIPT = ANALYSIS_DIR / "dist-analyze.py"

# Hand-computed stats for durations [10,20,30,40,50,60,70,80,90,100] us
# (pandas linear interpolation, verified against the pinned method).
D10_MEAN = 55.0
D10_MEDIAN = 55.0
D10_P50 = 55.0
D10_P95 = 95.5
D10_P99 = 99.1
D10_MAX = 100.0
D10_TOTAL_MS = 0.55
D10_PERCENTILES = {
    "p1": 10.9,
    "p11": 19.9,
    "p21": 28.9,
    "p31": 37.9,
    "p41": 46.9,
    "p51": 55.9,
    "p61": 64.9,
    "p71": 73.9,
    "p81": 82.9,
    "p91": 91.9,
    "p99": 99.1,
}


# =========================================================================
# Helpers
# =========================================================================


def load_dist_analyze_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("dist_analyze", DIST_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dist(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run dist-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(DIST_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    env: dict[str, str],
    family: str = "dist-family",
    workload: str = "stress-ng",
    extra: list[str] | None = None,
):
    """Run the script against a fixture and return (rc, stderr, output dir)."""
    out_dir = tmp_path / "output"
    argv = [
        "--data-dir",
        str(fixture_dir),
        "--output-dir",
        str(out_dir),
        "--family",
        family,
        "--workload",
        workload,
    ] + (extra or [])
    rc, _out, err = run_dist(argv, env=env)
    return rc, err, out_dir


def dist_slices_df() -> pd.DataFrame:
    return pd.DataFrame(DIST_3POD_SLICES)


def dist_runtime_df() -> pd.DataFrame:
    return pd.DataFrame(DIST_3POD_RUNTIME)


def summary_row(
    cell: str,
    pod: str,
    *,
    replicate: int = 1,
    slice_count: int,
    total_exec_ms: float,
    mean_us: float,
    median_us: float,
    p50_us: float,
    p95_us: float,
    p99_us: float,
    max_us: float,
    throttle_ratio: float,
    cpu_weight: int,
    cpu_max: int,
    quality: str = "good",
) -> tuple:
    return (
        cell,
        replicate,
        pod,
        slice_count,
        total_exec_ms,
        mean_us,
        median_us,
        p50_us,
        p95_us,
        p99_us,
        max_us,
        throttle_ratio,
        cpu_weight,
        cpu_max,
        quality,
    )


def sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root*."""
    import hashlib

    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root))
            manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        module = load_dist_analyze_module()
        for name in (
            "load_pid_map",
            "assign_pods",
            "assign_runtime_pods",
            "compute_stats",
            "compute_percentiles",
            "compute_throttle_ratio",
            "compute_cpu_limits",
            "compute_coverage",
            "quality_for",
            "retained_window",
            "parse_request_limit",
            "sanity_check",
            "load_trace",
            "trace_event_bounds",
            "query_slices",
            "query_runtime",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )

    def test_module_exposes_pinned_constants(self):
        module = load_dist_analyze_module()
        assert list(module.SLICES_COLUMNS) == DIST_SLICES_COLUMNS
        assert list(module.RUNTIME_COLUMNS) == DIST_RUNTIME_COLUMNS
        assert list(module.SUMMARY_COLUMNS) == DIST_SUMMARY_COLUMNS
        assert tuple(module.PERCENTILE_STEPS) == DIST_PERCENTILE_STEPS
        assert module.COVERAGE_THRESHOLD == DIST_COVERAGE_THRESHOLD
        assert module.SYSTEM_POD == DIST_SYSTEM_POD

    def test_slice_query_has_required_fragments(self):
        module = load_dist_analyze_module()
        sql = module.QUERY_SLICES.lower()
        for fragment in (
            "select",
            "sched_slice",
            "thread",
            "tid",
            "ts",
            "dur",
            "where",
        ):
            assert fragment in sql, f"QUERY_SLICES missing fragment: {fragment}"

    def test_runtime_query_has_required_fragments(self):
        module = load_dist_analyze_module()
        sql = module.QUERY_RUNTIME.lower()
        for fragment in ("select", "sched_stat_runtime", "runtime", "pid", "tid"):
            assert fragment in sql, f"QUERY_RUNTIME missing fragment: {fragment}"


# =========================================================================
# Pid->pod map from eevdf-<pod>-pids.csv pod/pid columns
# =========================================================================


class TestLoadPidMap:
    """The map is built EXCLUSIVELY from the pod/pid columns."""

    def test_three_pods_map_built_from_pid_columns(
        self, dist_three_pod_cell_dir: pathlib.Path
    ):
        module = load_dist_analyze_module()
        pid_map = module.load_pid_map(
            dist_three_pod_cell_dir / "co-located-a-b-c" / "replicate-1"
        )
        assert pid_map == {101: "a", 201: "b", 301: "c"}

    def test_duplicate_pid_across_samples_dedupes(self, tmp_path: pathlib.Path):
        """Multiple samples of the same pid produce one map entry."""
        from tests.conftest import write_eevdf_pids_csv

        d = tmp_path / "cell" / "replicate-1"
        write_eevdf_pids_csv(d / "eevdf-a-pids.csv", "a", [101], samples=5)
        module = load_dist_analyze_module()
        assert module.load_pid_map(d) == {101: "a"}

    def test_no_eevdf_files_returns_empty_map(self, dist_empty_cell_dir: pathlib.Path):
        module = load_dist_analyze_module()
        assert (
            module.load_pid_map(dist_empty_cell_dir / "no-pids-cell" / "replicate-1")
            == {}
        )

    def test_ignores_non_eevdf_files(self, dist_three_pod_cell_dir: pathlib.Path):
        """cgroup-<pod>.csv / metadata.json / trace are NOT map sources."""
        d = dist_three_pod_cell_dir / "co-located-a-b-c" / "replicate-1"
        module = load_dist_analyze_module()
        pid_map = module.load_pid_map(d)
        assert set(pid_map.values()) == {"a", "b", "c"}
        assert len(pid_map) == 3

    def test_deterministic_map_across_calls(
        self, dist_three_pod_cell_dir: pathlib.Path
    ):
        d = dist_three_pod_cell_dir / "co-located-a-b-c" / "replicate-1"
        module = load_dist_analyze_module()
        assert module.load_pid_map(d) == module.load_pid_map(d)


class TestAssignPods:
    """sched_slice tids map to pods; unmapped tids become 'system'."""

    def test_three_pods_identical_thread_names_attributed_by_tid(
        self, dist_three_pod_cell_dir: pathlib.Path
    ):
        """All threads share name 'stress-ng-cpu'; attribution must be
        by tid from the pid map, never by thread name."""
        d = dist_three_pod_cell_dir / "co-located-a-b-c" / "replicate-1"
        module = load_dist_analyze_module()
        pid_map = module.load_pid_map(d)
        result = module.assign_pods(dist_slices_df(), pid_map)
        assert set(result["thread_name"]) == {"stress-ng-cpu"}
        assert list(result["tid"].map(pid_map).fillna("?"))  # map covers 101/201/301
        assert result.loc[result["tid"] == 101, "pod"].eq("a").all()
        assert result.loc[result["tid"] == 201, "pod"].eq("b").all()
        assert result.loc[result["tid"] == 301, "pod"].eq("c").all()

    def test_unmapped_tids_labeled_system(self, dist_three_pod_cell_dir: pathlib.Path):
        d = dist_three_pod_cell_dir / "co-located-a-b-c" / "replicate-1"
        module = load_dist_analyze_module()
        pid_map = module.load_pid_map(d)
        result = module.assign_pods(dist_slices_df(), pid_map)
        assert result.loc[result["tid"] == 901, "pod"].eq(DIST_SYSTEM_POD).all()
        assert result.loc[result["tid"] == 902, "pod"].eq(DIST_SYSTEM_POD).all()
        assert set(result["pod"]) == {"a", "b", "c", DIST_SYSTEM_POD}

    def test_output_columns_match_pinned_contract(self):
        module = load_dist_analyze_module()
        result = module.assign_pods(dist_slices_df(), {101: "a"})
        assert list(result.columns) == DIST_SLICES_COLUMNS

    def test_empty_slices_produce_pinned_schema(self):
        module = load_dist_analyze_module()
        empty = pd.DataFrame(columns=DIST_SLICES_COLUMNS[:6])  # type: ignore
        result = module.assign_pods(empty, {})
        assert list(result.columns) == DIST_SLICES_COLUMNS
        assert len(result) == 0


class TestAssignRuntimePods:
    """sched_stat_runtime samples get pod attribution."""

    def test_runtime_rows_get_pod(self):
        module = load_dist_analyze_module()
        result = module.assign_runtime_pods(
            dist_runtime_df(), {101: "a", 201: "b", 301: "c"}
        )
        assert result.loc[result["tid"] == 101, "pod"].eq("a").all()
        assert result.loc[result["tid"] == 201, "pod"].eq("b").all()
        assert result.loc[result["tid"] == 301, "pod"].eq("c").all()

    def test_unmapped_runtime_tid_labeled_system(self):
        module = load_dist_analyze_module()
        result = module.assign_runtime_pods(dist_runtime_df(), {101: "a"})
        assert result.loc[result["tid"] == 901, "pod"].eq(DIST_SYSTEM_POD).all()

    def test_runtime_output_columns_match_pinned_contract(self):
        module = load_dist_analyze_module()
        result = module.assign_runtime_pods(dist_runtime_df(), {101: "a"})
        assert list(result.columns) == DIST_RUNTIME_COLUMNS


# =========================================================================
# Summary statistics and percentile table (hand-computed)
# =========================================================================


class TestComputeStats:
    """mean/median/p50/p95/p99/max with pinned linear interpolation."""

    def test_exact_stats_on_10_durations(self):
        module = load_dist_analyze_module()
        stats = module.compute_stats([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert stats["mean_us"] == pytest.approx(D10_MEAN, abs=1e-9)
        assert stats["median_us"] == pytest.approx(D10_MEDIAN, abs=1e-9)
        assert stats["p50_us"] == pytest.approx(D10_P50, abs=1e-9)
        assert stats["p95_us"] == pytest.approx(D10_P95, abs=1e-9)
        assert stats["p99_us"] == pytest.approx(D10_P99, abs=1e-9)
        assert stats["max_us"] == pytest.approx(D10_MAX, abs=1e-9)

    def test_stats_keys_pinned(self):
        module = load_dist_analyze_module()
        stats = module.compute_stats([10, 20, 30])
        assert list(stats.keys()) == [
            "mean_us",
            "median_us",
            "p50_us",
            "p95_us",
            "p99_us",
            "max_us",
        ]

    def test_single_duration(self):
        module = load_dist_analyze_module()
        stats = module.compute_stats([42])
        assert stats["mean_us"] == pytest.approx(42.0)
        assert stats["p99_us"] == pytest.approx(42.0)
        assert stats["max_us"] == pytest.approx(42.0)

    def test_empty_durations_yield_zeros(self):
        module = load_dist_analyze_module()
        stats = module.compute_stats([])
        for key in ("mean_us", "median_us", "p50_us", "p95_us", "p99_us", "max_us"):
            assert stats[key] == 0.0, f"{key} not zero for empty input"


class TestComputePercentiles:
    """Full percentile table 1..99 at 1-decile steps (11 values)."""

    def test_keys_are_decile_steps(self):
        module = load_dist_analyze_module()
        table = module.compute_percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert list(table.keys()) == [f"p{k}" for k in DIST_PERCENTILE_STEPS]

    def test_exact_values_on_10_durations(self):
        module = load_dist_analyze_module()
        table = module.compute_percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        for key, expected in D10_PERCENTILES.items():
            assert table[key] == pytest.approx(expected, abs=1e-9), key

    def test_table_is_monotonic(self):
        module = load_dist_analyze_module()
        table = module.compute_percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        values = [table[f"p{k}"] for k in DIST_PERCENTILE_STEPS]
        assert values == sorted(values)

    def test_empty_durations_yield_zero_table(self):
        module = load_dist_analyze_module()
        table = module.compute_percentiles([])
        assert list(table.keys()) == [f"p{k}" for k in DIST_PERCENTILE_STEPS]
        assert all(v == 0.0 for v in table.values())


class TestThrottleRatio:
    """throttle_ratio = nr_throttled / nr_periods from the LAST cgroup sample."""

    def test_last_sample_ratio(self, tmp_path: pathlib.Path):
        path = write_dist_cgroup_csv(
            tmp_path / "cgroup-a.csv",
            "a",
            nr_periods=1000,
            nr_throttled=990,
            cpu_weight=59,
            cpu_max_quota=10000,
        )
        module = load_dist_analyze_module()
        assert module.compute_throttle_ratio(path) == pytest.approx(0.99, abs=1e-9)

    def test_zero_periods_yields_zero(self, tmp_path: pathlib.Path):
        path = write_dist_cgroup_csv(
            tmp_path / "cgroup-a.csv",
            "a",
            nr_periods=0,
            nr_throttled=0,
            cpu_weight=59,
            cpu_max_quota=10000,
        )
        module = load_dist_analyze_module()
        assert module.compute_throttle_ratio(path) == 0.0


class TestComputeCpuLimits:
    """cpu_weight / cpu_max (quota us) read from the last cgroup sample."""

    def test_weight_and_quota(self, tmp_path: pathlib.Path):
        path = write_dist_cgroup_csv(
            tmp_path / "cgroup-a.csv",
            "a",
            nr_periods=1000,
            nr_throttled=10,
            cpu_weight=59,
            cpu_max_quota=50000,
        )
        module = load_dist_analyze_module()
        assert module.compute_cpu_limits(path) == (59, 50000)


# =========================================================================
# Trace coverage quality gate
# =========================================================================


class TestCoverage:
    """retained coverage = (last - first retained event ts) / duration."""

    def test_full_coverage(self):
        module = load_dist_analyze_module()
        # events span 2.5s..87s of a 90s measurement
        cov = module.compute_coverage(2_500_000, 87_000_000, 90)
        assert cov == pytest.approx(84_500_000 / 90_000_000, abs=1e-9)

    def test_degraded_span(self):
        module = load_dist_analyze_module()
        cov = module.compute_coverage(2_500_000, 52_500_000, 90)
        assert cov == pytest.approx(50_000_000 / 90_000_000, abs=1e-9)

    def test_zero_span_no_events(self):
        module = load_dist_analyze_module()
        assert module.compute_coverage(0, 0, 90) == 0.0

    def test_reversed_span_yields_zero(self):
        module = load_dist_analyze_module()
        assert module.compute_coverage(87_000_000, 2_500_000, 90) == 0.0

    def test_span_longer_than_duration_not_clamped(self):
        """Coverage may exceed 1.0 when capture starts before the window;
        the gate is '>= 0.80', so such a cell still qualifies as good."""
        module = load_dist_analyze_module()
        cov = module.compute_coverage(0, 100_000_000, 90)
        assert cov == pytest.approx(1.111111, abs=1e-6)


class TestQualityFor:
    """quality: 'good' when coverage >= 0.80, else 'degraded'."""

    def test_threshold_boundary_is_good(self):
        module = load_dist_analyze_module()
        assert module.quality_for(0.80) == "good"

    def test_below_threshold_is_degraded(self):
        module = load_dist_analyze_module()
        assert module.quality_for(0.7999) == "degraded"

    def test_zero_is_degraded(self):
        module = load_dist_analyze_module()
        assert module.quality_for(0.0) == "degraded"

    def test_above_threshold_is_good(self):
        module = load_dist_analyze_module()
        assert module.quality_for(0.9389) == "good"


class TestRetainedWindow:
    """Guard window: trace minus 2s at each end, returned in microseconds."""

    def test_guard_window_math(self):
        module = load_dist_analyze_module()
        assert module.retained_window(0, 90_000_000_000, 2.0) == (2_000_000, 88_000_000)

    def test_default_guard_is_two_seconds(self):
        module = load_dist_analyze_module()
        assert module.retained_window(0, 90_000_000_000) == (2_000_000, 88_000_000)

    def test_trace_shorter_than_guards_yields_empty_window(self):
        module = load_dist_analyze_module()
        start, end = module.retained_window(0, 1_000_000_000, 2.0)
        assert start == end  # empty window: ts >= start AND ts < end matches nothing


# =========================================================================
# Data integrity sanity gate (pure function)
# =========================================================================


def stress_rows() -> list[tuple]:
    """Five saturating stress-ng cells (limit<demand throttle >= 0.95,
    limit>=demand < 0.05) with monotonic percentiles."""
    return [
        summary_row(
            "request=100m-limit=100m",
            "stress-ng",
            slice_count=10,
            total_exec_ms=0.55,
            mean_us=55.0,
            median_us=55.0,
            p50_us=55.0,
            p95_us=95.5,
            p99_us=99.1,
            max_us=100.0,
            throttle_ratio=0.99,
            cpu_weight=17,
            cpu_max=10000,
        ),
        summary_row(
            "request=100m-limit=1000m",
            "stress-ng",
            slice_count=10,
            total_exec_ms=0.55,
            mean_us=55.0,
            median_us=55.0,
            p50_us=55.0,
            p95_us=95.5,
            p99_us=99.1,
            max_us=100.0,
            throttle_ratio=0.97,
            cpu_weight=17,
            cpu_max=100000,
        ),
        summary_row(
            "request=500m-limit=500m",
            "stress-ng",
            slice_count=10,
            total_exec_ms=0.55,
            mean_us=55.0,
            median_us=55.0,
            p50_us=55.0,
            p95_us=95.5,
            p99_us=99.1,
            max_us=100.0,
            throttle_ratio=0.96,
            cpu_weight=59,
            cpu_max=50000,
        ),
        summary_row(
            "request=500m-limit=2000m",
            "stress-ng",
            slice_count=10,
            total_exec_ms=0.55,
            mean_us=55.0,
            median_us=55.0,
            p50_us=55.0,
            p95_us=95.5,
            p99_us=99.1,
            max_us=100.0,
            throttle_ratio=0.01,
            cpu_weight=59,
            cpu_max=200000,
        ),
        summary_row(
            "request=1000m-limit=2000m",
            "stress-ng",
            slice_count=10,
            total_exec_ms=0.55,
            mean_us=55.0,
            median_us=55.0,
            p50_us=55.0,
            p95_us=95.5,
            p99_us=99.1,
            max_us=100.0,
            throttle_ratio=0.02,
            cpu_weight=100,
            cpu_max=200000,
        ),
    ]


class TestSanityGate:
    """Violated facts are returned as messages; empty means pass."""

    def _gate(self, module, rows, workload_by_cell):
        df = pd.DataFrame(  # type: ignore
            rows,
            columns=DIST_SUMMARY_COLUMNS,  # type: ignore
        )
        return module.sanity_check(df, workload_by_cell)

    def test_saturating_stress_ng_cells_pass(self):
        """limit<demand ratio >= 0.95 and limit>=demand ratio < 0.05 -> no facts."""
        module = load_dist_analyze_module()
        workload = {r[0]: "stress-ng" for r in stress_rows()}
        assert self._gate(module, stress_rows(), workload) == []

    def test_under_throttled_saturating_cell_violated(self):
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=100m-limit=100m",
                "stress-ng",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.90,
                cpu_weight=17,
                cpu_max=10000,
            ),
        ]
        violations = self._gate(module, rows, {"request=100m-limit=100m": "stress-ng"})
        assert len(violations) == 1
        msg = violations[0]
        assert "request=100m-limit=100m" in msg
        assert "throttle_ratio" in msg
        assert "< 0.95" in msg

    def test_over_throttled_at_demand_violated(self):
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=500m-limit=2000m",
                "stress-ng",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.10,
                cpu_weight=59,
                cpu_max=200000,
            ),
        ]
        violations = self._gate(module, rows, {"request=500m-limit=2000m": "stress-ng"})
        assert len(violations) == 1
        msg = violations[0]
        assert "request=500m-limit=2000m" in msg
        assert "throttle_ratio" in msg
        assert ">= 0.05" in msg

    def test_light_cpu_burner_cells_pass(self):
        """cpu-burner 30% cells: ratio < 0.05 at limit >= 300m -> no facts."""
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=500m-limit=500m",
                "cpu-burner",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.001,
                cpu_weight=59,
                cpu_max=50000,
            ),
            summary_row(
                "request=1000m-limit=2000m",
                "cpu-burner",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.001,
                cpu_weight=100,
                cpu_max=200000,
            ),
        ]
        workload = {r[0]: "cpu-burner" for r in rows}
        assert self._gate(module, rows, workload) == []

    def test_throttled_light_cpu_burner_violated(self):
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=500m-limit=500m",
                "cpu-burner",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.20,
                cpu_weight=59,
                cpu_max=50000,
            ),
        ]
        violations = self._gate(module, rows, {"request=500m-limit=500m": "cpu-burner"})
        assert len(violations) == 1
        msg = violations[0]
        assert "cpu-burner" in msg
        assert "throttle_ratio" in msg
        assert ">= 0.05" in msg

    def test_percentile_monotonicity_violated(self):
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=100m-limit=100m",
                "stress-ng",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=90.0,
                p95_us=80.0,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.99,
                cpu_weight=17,
                cpu_max=10000,
            ),
        ]
        violations = self._gate(module, rows, {"request=100m-limit=100m": "stress-ng"})
        assert len(violations) == 1
        msg = violations[0]
        assert "monotonicity" in msg
        assert "request=100m-limit=100m" in msg

    def test_multi_pod_label_skips_throttle_rule(self):
        """A co-located label has many request/limit pairs; the throttle-ratio
        fact is ambiguous there, so only monotonicity is enforced."""
        module = load_dist_analyze_module()
        cell = "a_request=500m-a_limit=-b_request=500m-b_limit="
        rows = [
            summary_row(
                cell,
                "pod-a",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.50,
                cpu_weight=59,
                cpu_max=50000,
            ),
        ]
        violations = self._gate(module, rows, {cell: "stress-ng"})
        assert violations == []

    def test_unlimited_cell_no_throttle_fact(self):
        """request=none-limit=none has no limit to classify -> no fact."""
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=none-limit=none",
                "stress-ng",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.99,
                cpu_weight=59,
                cpu_max=100000,
            ),
        ]
        violations = self._gate(module, rows, {"request=none-limit=none": "stress-ng"})
        assert violations == []

    def test_http_workload_only_monotonicity(self):
        """api-server/db-simulator are not saturating: no throttle facts."""
        module = load_dist_analyze_module()
        rows = [
            summary_row(
                "request=100m-limit=100m",
                "api-server",
                slice_count=10,
                total_exec_ms=0.55,
                mean_us=55.0,
                median_us=55.0,
                p50_us=55.0,
                p95_us=95.5,
                p99_us=99.1,
                max_us=100.0,
                throttle_ratio=0.99,
                cpu_weight=17,
                cpu_max=10000,
            ),
        ]
        violations = self._gate(module, rows, {"request=100m-limit=100m": "api-server"})
        assert violations == []


class TestParseRequestLimit:
    """Parse request/limit millicores from pinned cell labels."""

    def test_standard_pair(self):
        module = load_dist_analyze_module()
        assert module.parse_request_limit("request=100m-limit=100m") == (100, 100)

    def test_none_pair(self):
        module = load_dist_analyze_module()
        assert module.parse_request_limit("request=none-limit=none") == (None, None)

    def test_unlimited_limit_only(self):
        module = load_dist_analyze_module()
        assert module.parse_request_limit("request=-limit=1000m") == (None, 1000)

    def test_first_pair_wins_on_multi_pod_label(self):
        module = load_dist_analyze_module()
        cell = "a_request=500m-a_limit=-b_request=500m-b_limit="
        assert module.parse_request_limit(cell) == (500, None)

    def test_no_pairs_returns_nones(self):
        module = load_dist_analyze_module()
        assert module.parse_request_limit("co-located-a-b-c") == (None, None)


# =========================================================================
# Thin trace layer (exercised with a mock TraceProcessor)
# =========================================================================


class TestTraceLayer:
    """query_slices / query_runtime / trace_event_bounds / load_trace."""

    def test_query_slices_returns_pinned_columns(
        self, dist_mock_trace_processor_factory
    ):
        module = load_dist_analyze_module()
        slices_df = dist_slices_df()
        tp = dist_mock_trace_processor_factory(slices_df=slices_df)()
        result = module.query_slices(tp, 2_000_000, 88_000_000)
        assert list(result.columns) == DIST_SLICES_COLUMNS[:6]
        assert len(result) == len(slices_df)

    def test_query_runtime_returns_pinned_columns(
        self, dist_mock_trace_processor_factory
    ):
        module = load_dist_analyze_module()
        runtime_df = dist_runtime_df()
        tp = dist_mock_trace_processor_factory(runtime_df=runtime_df)()
        result = module.query_runtime(tp, 2_000_000, 88_000_000)
        # trace layer returns the SOURCE columns; pod is added by assign_runtime_pods
        assert list(result.columns) == [
            "ts",
            "cpu",
            "pid",
            "tid",
            "thread_name",
            "runtime_ns",
        ]
        assert len(result) == len(runtime_df)

    def test_trace_event_bounds(self, dist_mock_trace_processor_factory):
        module = load_dist_analyze_module()
        tp = dist_mock_trace_processor_factory(
            bounds_df=pd.DataFrame(
                [{"first_ts_ns": 2_000_000_000, "last_ts_ns": 88_000_000_000}]
            )
        )()
        assert module.trace_event_bounds(tp) == (2_000_000_000, 88_000_000_000)

    def test_load_trace_returns_processor(self, dist_mock_trace_processor):
        module = load_dist_analyze_module()
        tp = module.load_trace("fake-trace.perfetto-trace")
        assert tp is not None

    def test_load_trace_failure_surfaces_nonzero(
        self, dist_mock_trace_processor_factory, monkeypatch, capsys, tmp_path
    ):
        """A corrupt trace must fail loudly (non-zero exit, message names the
        failure), never silently produce empty outputs."""
        import sys
        from unittest import mock

        module = load_dist_analyze_module()
        failing = dist_mock_trace_processor_factory(fail_on_load=True)
        fake_module = mock.MagicMock()
        fake_module.TraceProcessor = failing
        monkeypatch.setitem(sys.modules, "perfetto.trace_processor", fake_module)

        from tests.conftest import build_dist_cell

        data_dir = build_dist_cell(
            tmp_path / "data",
            "request=100m-limit=100m",
            pod="stress-ng",
            pids=[1001],
            nr_periods=1000,
            nr_throttled=990,
            cpu_weight=17,
            cpu_max_quota=10000,
        )
        rc = module.main(
            [
                "--data-dir",
                str(data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                "dist-stress-ng",
                "--workload",
                "stress-ng",
            ]
        )
        err = capsys.readouterr().err.lower()
        assert rc != 0
        assert "corrupt" in err or "trace" in err


# =========================================================================
# CLI contract and determinism
# =========================================================================


class TestCli:
    """--data-dir/--output-dir/--family/--workload/--duration contract."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_dist(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        for flag in (
            "--data-dir",
            "--output-dir",
            "--family",
            "--workload",
            "--duration",
        ):
            assert flag in combined

    def test_missing_required_flags_exits_nonzero(self):
        rc, _out, err = run_dist([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        out_dir = tmp_path / "output"
        rc, _out, err = run_dist(
            [
                "--data-dir",
                str(tmp_path / "missing"),
                "--output-dir",
                str(out_dir),
                "--family",
                "dist-stress-ng",
            ]
        )
        assert rc != 0
        assert "missing" in err

    def test_two_runs_produce_byte_identical_outputs(
        self,
        dist_three_pod_cell_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        dist_fake_perfetto_env,
    ):
        """Determinism: two runs on the same staged data yield
        identical SHA-256 manifests — no wall-clock values in outputs."""
        env = dist_fake_perfetto_env()
        rc1, _, err1 = run_dist(
            [
                "--data-dir",
                str(dist_three_pod_cell_dir),
                "--output-dir",
                str(tmp_path / "out1"),
                "--family",
                "dist-family",
                "--workload",
                "stress-ng",
            ],
            env=env,
        )
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, _, err2 = run_dist(
            [
                "--data-dir",
                str(dist_three_pod_cell_dir),
                "--output-dir",
                str(tmp_path / "out2"),
                "--family",
                "dist-family",
                "--workload",
                "stress-ng",
            ],
            env=env,
        )
        assert rc2 == 0, f"second run failed: {err2}"
        m1 = sha256_manifest(tmp_path / "out1" / "distribution")
        m2 = sha256_manifest(tmp_path / "out2" / "distribution")
        assert m1 == m2
        # The additive per-replicate slice files mean a cell emits the four pinned
        # outputs PLUS one dist-slices-replicate-<n>.csv per replicate. The
        # three-pod fixture has a single replicate, so the expected set is
        # exact; asserting the set rather than a bare count keeps this test
        # from silently breaking again when a replicate file is added.
        cell_rel = "dist-family/co-located-a-b-c"
        expected = {
            f"{cell_rel}/dist-slices.csv",
            f"{cell_rel}/dist-runtime.csv",
            f"{cell_rel}/dist-summary.csv",
            f"{cell_rel}/dist-percentiles.json",
            f"{cell_rel}/dist-slices-replicate-1.csv",
        }
        assert set(m1) == expected


# =========================================================================
# End-to-end: staged cell data -> pinned outputs
# =========================================================================


class TestEndToEndThreePod:
    """Happy path on the three-pod collision fixture."""

    def _run(self, dist_three_pod_cell_dir, tmp_path, env):
        rc, err, out_dir = run_ok(
            dist_three_pod_cell_dir,
            tmp_path,
            env=env,
            family="dist-family",
            workload="stress-ng",
        )
        assert rc == 0, f"stderr: {err}"
        cell_out = out_dir / "distribution" / "dist-family" / "co-located-a-b-c"
        assert cell_out.is_dir(), f"missing cell output dir: {cell_out}"
        return cell_out

    def test_output_layout_matches_req017(
        self, dist_three_pod_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()
        cell_out = self._run(dist_three_pod_cell_dir, tmp_path, env)
        for name in (
            "dist-slices.csv",
            "dist-runtime.csv",
            "dist-summary.csv",
            "dist-percentiles.json",
        ):
            assert (cell_out / name).is_file(), f"missing {name}"

    def test_slices_columns_and_pod_attribution(
        self, dist_three_pod_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()
        cell_out = self._run(dist_three_pod_cell_dir, tmp_path, env)
        slices = pd.read_csv(cell_out / "dist-slices.csv")
        assert list(slices.columns) == DIST_SLICES_COLUMNS
        assert len(slices) == 33
        # tid-based attribution despite identical thread names
        assert set(slices["thread_name"]) == {"stress-ng-cpu"}
        assert slices.loc[slices["tid"] == 101, "pod"].eq("a").all()
        assert slices.loc[slices["tid"] == 201, "pod"].eq("b").all()
        assert slices.loc[slices["tid"] == 301, "pod"].eq("c").all()
        assert slices.loc[slices["tid"] == 901, "pod"].eq(DIST_SYSTEM_POD).all()
        assert slices.loc[slices["tid"] == 902, "pod"].eq(DIST_SYSTEM_POD).all()
        # deterministic ordering: sorted by ts_start_us
        assert slices["ts_start_us"].is_monotonic_increasing

    def test_runtime_columns_and_pod_attribution(
        self, dist_three_pod_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()
        cell_out = self._run(dist_three_pod_cell_dir, tmp_path, env)
        runtime = pd.read_csv(cell_out / "dist-runtime.csv")
        assert list(runtime.columns) == DIST_RUNTIME_COLUMNS
        assert len(runtime) == 5
        assert runtime.loc[runtime["tid"] == 101, "pod"].eq("a").all()
        assert runtime.loc[runtime["tid"] == 901, "pod"].eq(DIST_SYSTEM_POD).all()
        assert runtime["ts"].is_monotonic_increasing

    def test_summary_schema_and_exact_values(
        self, dist_three_pod_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()
        cell_out = self._run(dist_three_pod_cell_dir, tmp_path, env)
        summary = pd.read_csv(cell_out / "dist-summary.csv")
        assert list(summary.columns) == DIST_SUMMARY_COLUMNS
        assert set(summary["pod"]) == {"a", "b", "c", DIST_SYSTEM_POD}
        a = summary[summary["pod"] == "a"].iloc[0]
        assert a["cell"] == "co-located-a-b-c"
        assert a["replicate"] == 1
        assert a["slice_count"] == 10
        assert a["total_exec_ms"] == pytest.approx(D10_TOTAL_MS, abs=1e-9)
        assert a["mean_us"] == pytest.approx(D10_MEAN, abs=1e-9)
        assert a["median_us"] == pytest.approx(D10_MEDIAN, abs=1e-9)
        assert a["p50_us"] == pytest.approx(D10_P50, abs=1e-9)
        assert a["p95_us"] == pytest.approx(D10_P95, abs=1e-9)
        assert a["p99_us"] == pytest.approx(D10_P99, abs=1e-9)
        assert a["max_us"] == pytest.approx(D10_MAX, abs=1e-9)
        assert a["throttle_ratio"] == pytest.approx(0.0, abs=1e-9)
        assert a["cpu_weight"] == 59
        assert a["cpu_max"] == 50000
        assert a["quality"] == "good"
        sys_row = summary[summary["pod"] == DIST_SYSTEM_POD].iloc[0]
        assert sys_row["slice_count"] == 3
        assert sys_row["mean_us"] == pytest.approx(40.0, abs=1e-9)
        assert sys_row["max_us"] == pytest.approx(100.0, abs=1e-9)

    def test_percentiles_json_structure(
        self, dist_three_pod_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()
        cell_out = self._run(dist_three_pod_cell_dir, tmp_path, env)
        table = json.loads((cell_out / "dist-percentiles.json").read_text())
        assert "1" in table  # replicate -> pod -> percentile table
        assert set(table["1"].keys()) == {"a", "b", "c", DIST_SYSTEM_POD}
        pod_table = table["1"]["a"]
        assert list(pod_table.keys()) == [f"p{k}" for k in DIST_PERCENTILE_STEPS]
        for key, expected in D10_PERCENTILES.items():
            assert pod_table[key] == pytest.approx(expected, abs=1e-9)


class TestEndToEndSanityGate:
    """Sanity gate via the CLI: pass exits 0, violations exit non-zero loudly."""

    def test_saturating_stress_cells_pass(
        self, dist_stress_saturating_data_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_SANITY_SLICES, runtime_rows=[])
        rc, err, out_dir = run_ok(
            dist_stress_saturating_data_dir,
            tmp_path,
            env=env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc == 0, f"stderr: {err}"
        summary_path = (
            out_dir
            / "distribution"
            / "dist-stress-ng"
            / "request=100m-limit=100m"
            / "dist-summary.csv"
        )
        assert summary_path.is_file()
        summary = pd.read_csv(summary_path)
        assert summary["throttle_ratio"].iloc[0] == pytest.approx(0.99, abs=1e-9)
        assert summary["cpu_max"].iloc[0] == 10000
        assert summary["quality"].iloc[0] == "good"

    def test_light_cpu_burner_cells_pass(
        self, dist_cpu_burner_data_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_SANITY_SLICES, runtime_rows=[])
        rc, err, _out_dir = run_ok(
            dist_cpu_burner_data_dir,
            tmp_path,
            env=env,
            family="dist-burner",
            workload="cpu-burner",
        )
        assert rc == 0, f"stderr: {err}"

    def test_violation_exits_nonzero_and_lists_fact(
        self, dist_sanity_violation_data_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_SANITY_SLICES, runtime_rows=[])
        rc, err, _out_dir = run_ok(
            dist_sanity_violation_data_dir,
            tmp_path,
            env=env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc != 0, "sanity gate must fail loudly"
        assert "request=100m-limit=100m" in err
        assert "throttle_ratio" in err
        assert "< 0.95" in err


class TestEndToEndCoverage:
    """Quality flag flips good/degraded with the retained span."""

    def test_good_coverage(
        self, dist_degraded_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_SANITY_SLICES, runtime_rows=[])
        rc, err, out_dir = run_ok(
            dist_degraded_cell_dir,
            tmp_path,
            env=env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc == 0, f"stderr: {err}"
        summary = pd.read_csv(
            out_dir
            / "distribution"
            / "dist-stress-ng"
            / "coverage-a"
            / "dist-summary.csv"
        )
        assert summary["quality"].eq("good").all()

    def test_degraded_coverage_flagged_but_exit_zero(
        self, dist_degraded_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_DEGRADED_SLICES, runtime_rows=[])
        rc, err, out_dir = run_ok(
            dist_degraded_cell_dir,
            tmp_path,
            env=env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc == 0, f"degraded is a flag, not a gate failure: {err}"
        summary = pd.read_csv(
            out_dir
            / "distribution"
            / "dist-stress-ng"
            / "coverage-a"
            / "dist-summary.csv"
        )
        assert summary["quality"].eq("degraded").all()


class TestEndToEndAggregation:
    """Multiple replicates aggregate deterministically."""

    def test_two_replicates_produce_two_summary_rows(
        self, dist_two_replicate_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env(slices_rows=DIST_SANITY_SLICES, runtime_rows=[])
        rc, err, out_dir = run_ok(
            dist_two_replicate_cell_dir,
            tmp_path,
            env=env,
            family="dist-stress-ng",
            workload="stress-ng",
        )
        assert rc == 0, f"stderr: {err}"
        cell_out = (
            out_dir / "distribution" / "dist-stress-ng" / "request=100m-limit=100m"
        )
        summary = pd.read_csv(cell_out / "dist-summary.csv")
        assert list(summary["replicate"]) == [1, 2]
        assert summary["slice_count"].eq(3).all()  # 3 slices per replicate
        slices = pd.read_csv(cell_out / "dist-slices.csv")
        assert len(slices) == 6  # 3 slices x 2 replicates

    def test_no_pid_files_all_system(
        self, dist_empty_cell_dir, tmp_path, dist_fake_perfetto_env
    ):
        env = dist_fake_perfetto_env()  # DIST_3POD_SLICES; no eevdf pids on disk
        rc, err, out_dir = run_ok(
            dist_empty_cell_dir,
            tmp_path,
            env=env,
            family="dist-family",
            workload="stress-ng",
        )
        assert rc == 0, f"stderr: {err}"
        cell_out = out_dir / "distribution" / "dist-family" / "no-pids-cell"
        slices = pd.read_csv(cell_out / "dist-slices.csv")
        assert len(slices) == 33
        assert slices["pod"].eq(DIST_SYSTEM_POD).all()
