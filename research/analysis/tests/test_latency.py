"""Tests for latency-analyze.py — latency interference analysis.

TASK-016 test-first design, red until the script is implemented.
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract the implementation must build:

    research/analysis/latency-analyze.py  (module: latency_analyze)
      load_summary(data_dir: Path) -> pd.DataFrame
      discover_latency_csvs(data_dir: Path, summary_df: pd.DataFrame)
          -> dict[str, list[Path]]
      compute_cell_latencies(latency_paths: list[Path]) -> tuple[float, float, float] | None
      build_cell_table(summary_df: pd.DataFrame, cell_latencies: dict[str, tuple])
          -> pd.DataFrame
      correlation_summary(cell_table: pd.DataFrame) -> pd.DataFrame
      main(argv: list[str] | None = None) -> int

Percentiles come from research/analysis/latency_stats.py
(percentiles_from_csv, linear interpolation, empty -> 0.0) — the analyzer
reuses that module; it must NOT reimplement percentile math.

CLI: --data-dir <dir> --output-dir <dir>; writes latency-summary.csv
(cell,p50,p95,p99,throttled_usec,usage_usec,throttling_ratio),
latency-correlation.csv (metric,correlation) and a lazy, non-fatal
latency-vs-throttling.png.

Covered requirements:
  REQ-3 (VC-LAT-01) percentile + throttling join, exact values
  REQ-4 (VC-LAT-02) missing/empty latency.csv -> skip cell + warn, no crash
  REQ-6 (VC-CLI-01) --data-dir/--output-dir contract and exit codes
  REQ-6 (VC-EMPTY-01) empty input -> header-only output, no crash
  REQ-7 (VC-MPL-01) matplotlib lazy import, headless/non-fatal

Run from research/analysis:
    python3 -m pytest tests/test_latency.py -q
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    FAMILY_SUMMARY_COLUMNS,
    LAT_CELL_250,
    LAT_CELL_500,
    write_latency_csv,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
LATENCY_SCRIPT = ANALYSIS_DIR / "latency-analyze.py"
SUMMARY_CSV = "latency-summary.csv"
CORRELATION_CSV = "latency-correlation.csv"
OUTPUT_PNG = "latency-vs-throttling.png"
SUMMARY_COLUMNS = [
    "cell",
    "p50",
    "p95",
    "p99",
    "throttled_usec",
    "usage_usec",
    "throttling_ratio",
]
CORRELATION_COLUMNS = ["metric", "correlation"]

# Family D fixture expected values (hand-computed with latency_stats semantics).
CELL1 = "req=100m-lim=200m"  # latencies 1..20, throttled 900/1000 periods
CELL2 = "req=500m-lim=1000m"  # latencies 1..10, throttled 100/1000 periods
CELL1_P50, CELL1_P95, CELL1_P99 = 10.5, 19.05, 19.81
CELL2_P50, CELL2_P95, CELL2_P99 = 5.5, 9.55, 9.91

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_latency_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("latency_analyze", LATENCY_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {LATENCY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_latency(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run latency-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(LATENCY_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
        env=env or AGG_ENV,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
):
    """Run the script against a fixture and return (rc, stderr, output dir)."""
    out_dir = tmp_path / "output"
    rc, _out, err = run_latency(
        ["--data-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or []),
        env=env,
    )
    return rc, err, out_dir


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        """latency-analyze.py exposes the pinned public functions."""
        module = load_latency_module()
        for name in (
            "load_summary",
            "discover_latency_csvs",
            "compute_cell_latencies",
            "build_cell_table",
            "correlation_summary",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# load_summary
# =========================================================================


class TestLoadSummary:
    """Reading summary.csv from a data dir."""

    def test_reads_summary_rows(self, family_d_data_dir: pathlib.Path):
        """load_summary returns every (cell, replicate) row, 8-column schema."""
        module = load_latency_module()
        df = module.load_summary(family_d_data_dir)
        assert list(df.columns) == FAMILY_SUMMARY_COLUMNS
        assert len(df) == 4  # 2 cells x 2 replicates

    def test_missing_summary_raises(self, tmp_path: pathlib.Path):
        """Missing summary.csv raises FileNotFoundError with a clear message."""
        module = load_latency_module()
        with pytest.raises(FileNotFoundError):
            module.load_summary(tmp_path / "does-not-exist")


# =========================================================================
# VC-LAT-02 — latency.csv discovery (missing -> empty list)
# =========================================================================


class TestDiscoverLatencyCsvs:
    """Locating latency.csv files per cell, sorted."""

    def test_finds_sorted_per_cell_files(self, family_d_data_dir: pathlib.Path):
        """Each cell maps to its 2 sorted replicate latency.csv paths."""
        module = load_latency_module()
        summary = module.load_summary(family_d_data_dir)
        found = module.discover_latency_csvs(family_d_data_dir, summary)
        assert set(found) == {CELL1, CELL2}
        for cell in (CELL1, CELL2):
            assert len(found[cell]) == 2
            names = [p.name for p in found[cell]]
            assert names == ["latency.csv", "latency.csv"]

    def test_missing_latency_cell_empty(self, missing_latency_data_dir: pathlib.Path):
        """A cell without latency.csv maps to an empty list."""
        module = load_latency_module()
        summary = module.load_summary(missing_latency_data_dir)
        found = module.discover_latency_csvs(missing_latency_data_dir, summary)
        assert len(found[CELL1]) == 2
        assert found[CELL2] == []


# =========================================================================
# compute_cell_latencies — percentiles via latency_stats semantics
# =========================================================================


class TestComputeCellLatencies:
    """Per-cell p50/p95/p99 from the latency.csv files (latency_stats reuse)."""

    def test_latencies_1_to_20(self, tmp_path: pathlib.Path):
        """n=20: p50 10.5, p95 19.05, p99 19.81 (linear interpolation)."""
        module = load_latency_module()
        path = write_latency_csv(tmp_path / "latency.csv", list(range(1, 21)))
        assert module.compute_cell_latencies([path]) == pytest.approx(
            (CELL1_P50, CELL1_P95, CELL1_P99)
        )

    def test_latencies_1_to_10(self, tmp_path: pathlib.Path):
        """n=10: p50 5.5, p95 9.55, p99 9.91."""
        module = load_latency_module()
        path = write_latency_csv(tmp_path / "latency.csv", list(range(1, 11)))
        assert module.compute_cell_latencies([path]) == pytest.approx(
            (CELL2_P50, CELL2_P95, CELL2_P99)
        )

    def test_matches_latency_stats_module(self, tmp_path: pathlib.Path):
        """Percentiles agree with research/analysis/latency_stats.percentiles_from_csv."""
        import latency_stats

        module = load_latency_module()
        path = write_latency_csv(tmp_path / "latency.csv", list(range(1, 21)))
        stats = latency_stats.percentiles_from_csv(path)
        assert module.compute_cell_latencies([path]) == pytest.approx(
            (stats[50.0], stats[95.0], stats[99.0])
        )

    def test_empty_file_list_is_none(self):
        """A cell with no latency.csv yields None (skipped)."""
        module = load_latency_module()
        assert module.compute_cell_latencies([]) is None

    def test_header_only_files_are_none(self, tmp_path: pathlib.Path):
        """A cell whose only latency.csv is header-only yields None."""
        module = load_latency_module()
        empty = write_latency_csv(tmp_path / "latency.csv", [])
        assert module.compute_cell_latencies([empty]) is None

    def test_mixed_empty_and_data_mean(self, tmp_path: pathlib.Path):
        """An empty file contributes 0.0 to the per-replicate mean.

        Empty + [1..20] -> (0 + 10.5)/2 = 5.25, (0 + 19.05)/2 = 9.525,
        (0 + 19.81)/2 = 9.905. Pins the latency_stats empty->0.0 propagation.
        """
        module = load_latency_module()
        empty = write_latency_csv(tmp_path / "empty.csv", [])
        data = write_latency_csv(tmp_path / "data.csv", list(range(1, 21)))
        assert module.compute_cell_latencies([empty, data]) == pytest.approx(
            (5.25, 9.525, 9.905)
        )


# =========================================================================
# VC-LAT-01 — build_cell_table (percentiles joined with throttling)
# =========================================================================


class TestBuildCellTable:
    """Per-cell rows joining latency percentiles with summary throttling."""

    @pytest.fixture
    def full_latencies(self, family_d_data_dir: pathlib.Path) -> dict:
        module = load_latency_module()
        summary = module.load_summary(family_d_data_dir)
        found = module.discover_latency_csvs(family_d_data_dir, summary)
        return {
            cell: module.compute_cell_latencies(paths) for cell, paths in found.items()
        }

    def test_exact_rows(self, family_d_data_dir: pathlib.Path, full_latencies: dict):
        """Both cells carry exact percentiles and aggregate throttling stats."""
        module = load_latency_module()
        summary = module.load_summary(family_d_data_dir)
        table = module.build_cell_table(summary, full_latencies).set_index("cell")
        assert table.loc[CELL1, "p50"] == pytest.approx(CELL1_P50)
        assert table.loc[CELL1, "p95"] == pytest.approx(CELL1_P95)
        assert table.loc[CELL1, "p99"] == pytest.approx(CELL1_P99)
        assert table.loc[CELL1, "throttled_usec"] == 2 * 9000000  # sum across reps
        assert table.loc[CELL1, "usage_usec"] == 2 * 12000000
        assert table.loc[CELL1, "throttling_ratio"] == pytest.approx(1800 / 2000)
        assert table.loc[CELL2, "p50"] == pytest.approx(CELL2_P50)
        assert table.loc[CELL2, "p99"] == pytest.approx(CELL2_P99)
        assert table.loc[CELL2, "throttling_ratio"] == pytest.approx(200 / 2000)

    def test_output_columns_match_contract(
        self, family_d_data_dir: pathlib.Path, full_latencies: dict
    ):
        module = load_latency_module()
        summary = module.load_summary(family_d_data_dir)
        table = module.build_cell_table(summary, full_latencies)
        assert list(table.columns) == SUMMARY_COLUMNS

    def test_cell_without_latency_is_skipped(
        self, missing_latency_data_dir: pathlib.Path
    ):
        """A cell whose latencies are None is absent from the table."""
        module = load_latency_module()
        summary = module.load_summary(missing_latency_data_dir)
        found = module.discover_latency_csvs(missing_latency_data_dir, summary)
        latencies = {
            cell: module.compute_cell_latencies(paths) for cell, paths in found.items()
        }
        table = module.build_cell_table(summary, latencies)
        assert set(table["cell"]) == {CELL1}

    def test_throttling_ratio_aggregate_then_divide(self):
        """Pinned: ratio = sum(nr_throttled)/sum(nr_periods), not mean of ratios.

        Per-replicate ratios 0.4 (400/1000) and 0.6 (300/500) give aggregate
        700/1500 = 0.466666..., NOT the mean of per-replicate ratios 0.5.
        """
        module = load_latency_module()
        df = pd.DataFrame(
            [
                (CELL1, 1, 1000, 400, 0, 1000, 17, 20000),
                (CELL1, 2, 500, 300, 0, 2000, 17, 20000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        table = module.build_cell_table(df, {CELL1: (1.0, 2.0, 3.0)})
        row = table[table["cell"] == CELL1].iloc[0]
        assert row["throttling_ratio"] == pytest.approx(700 / 1500)
        assert row["throttling_ratio"] != pytest.approx(0.5, abs=1e-9)

    def test_empty_summary_returns_empty_table(self):
        """Empty summary -> empty DataFrame with the pinned columns."""
        module = load_latency_module()
        empty = pd.DataFrame(columns=FAMILY_SUMMARY_COLUMNS)  # type: ignore
        table = module.build_cell_table(empty, {})
        assert list(table.columns) == SUMMARY_COLUMNS
        assert len(table) == 0


# =========================================================================
# Correlation summary
# =========================================================================


class TestCorrelationSummary:
    """Pearson p50/p95/p99 vs throttled_usec (pandas-native, no scipy)."""

    @pytest.fixture
    def cell_table(self, family_d_data_dir: pathlib.Path) -> pd.DataFrame:
        module = load_latency_module()
        summary = module.load_summary(family_d_data_dir)
        found = module.discover_latency_csvs(family_d_data_dir, summary)
        latencies = {
            cell: module.compute_cell_latencies(paths) for cell, paths in found.items()
        }
        return module.build_cell_table(summary, latencies)

    def test_metrics_and_exact_correlation(self, cell_table: pd.DataFrame):
        """Fixture cells are perfectly correlated: r = 1.0 for all metrics."""
        module = load_latency_module()
        corr = module.correlation_summary(cell_table).set_index("metric")
        assert list(corr.index) == [
            "p50_vs_throttled_usec",
            "p95_vs_throttled_usec",
            "p99_vs_throttled_usec",
        ]
        assert corr.loc["p99_vs_throttled_usec", "correlation"] == pytest.approx(1.0)
        assert corr.loc["p50_vs_throttled_usec", "correlation"] == pytest.approx(1.0)

    def test_output_columns(self, cell_table: pd.DataFrame):
        module = load_latency_module()
        assert (
            list(module.correlation_summary(cell_table).columns) == CORRELATION_COLUMNS
        )

    def test_constant_metric_yields_nan(self):
        """Zero-variance input must yield NaN correlation, not a crash."""
        module = load_latency_module()
        table = pd.DataFrame(
            [
                ("a", 10.0, 10.0, 10.0, 1000, 2000, 0.5),
                ("b", 10.0, 10.0, 10.0, 2000, 4000, 0.5),
            ],
            columns=SUMMARY_COLUMNS,  # type: ignore
        )
        corr = module.correlation_summary(table).set_index("metric")
        assert pd.isna(corr.loc["p99_vs_throttled_usec", "correlation"])


# =========================================================================
# VC-CLI-01 — CLI contract
# =========================================================================


class TestCli:
    """--data-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_latency(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--data-dir" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_latency([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --data-dir -> non-zero exit and a clear message."""
        out_dir = tmp_path / "output"
        rc, _out, err = run_latency(
            ["--data-dir", str(tmp_path / "missing"), "--output-dir", str(out_dir)]
        )
        assert rc != 0
        assert "missing" in err


# =========================================================================
# End-to-end: fixture data -> CSV output
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract CSVs."""

    def test_happy_path_outputs_exact_csvs(
        self, family_d_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-LAT-01 + VC-CLI-01: exit 0, exact summary and correlation CSVs."""
        rc, err, out_dir = run_ok(family_d_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        summary_path = out_dir / SUMMARY_CSV
        assert summary_path.exists(), f"missing output: {summary_path}"
        table = pd.read_csv(summary_path).set_index("cell")
        assert list(table.columns) == SUMMARY_COLUMNS[1:]
        assert table.loc[CELL1, "p50"] == pytest.approx(CELL1_P50)
        assert table.loc[CELL1, "p99"] == pytest.approx(CELL1_P99)
        assert table.loc[CELL1, "throttling_ratio"] == pytest.approx(0.9)
        assert table.loc[CELL2, "throttling_ratio"] == pytest.approx(0.1)
        corr = pd.read_csv(out_dir / CORRELATION_CSV).set_index("metric")
        assert list(corr.columns) == ["correlation"]
        assert corr.loc["p99_vs_throttled_usec", "correlation"] == pytest.approx(1.0)

    def test_missing_latency_skips_cell_with_warning(
        self, missing_latency_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-LAT-02: exit 0, broken cell skipped, warning names the cell."""
        rc, err, out_dir = run_ok(missing_latency_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        summary_path = out_dir / SUMMARY_CSV
        assert summary_path.exists()
        table = pd.read_csv(summary_path)
        assert set(table["cell"]) == {CELL1}
        assert CELL2 in err
        assert "warn" in err.lower() or "skip" in err.lower()

    def test_empty_summary_outputs_header_only(
        self, empty_summary_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-EMPTY-01: exit 0, header-only CSVs, no crash."""
        rc, err, out_dir = run_ok(empty_summary_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        summary_lines = (out_dir / SUMMARY_CSV).read_text().splitlines()
        assert summary_lines == [",".join(SUMMARY_COLUMNS)]
        corr_lines = (out_dir / CORRELATION_CSV).read_text().splitlines()
        assert corr_lines == [",".join(CORRELATION_COLUMNS)]

    @pytest.mark.skipif(
        not importlib.util.find_spec("matplotlib"), reason="matplotlib not installed"
    )
    def test_png_rendered(
        self, family_d_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: real matplotlib (Agg) emits a valid PNG."""
        rc, err, out_dir = run_ok(family_d_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        png_path = out_dir / OUTPUT_PNG
        assert png_path.exists(), f"missing plot: {png_path}"
        assert png_path.read_bytes()[:4] == b"\x89PNG"

    def test_matplotlib_import_failure_is_nonfatal(
        self, family_d_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: broken matplotlib must not block the CSV outputs."""
        stub_dir = tmp_path / "stub-matplotlib"
        stub_dir.mkdir()
        (stub_dir / "matplotlib.py").write_text(
            'raise ImportError("stubbed out for tests")\n'
        )
        env = {**os.environ, "PYTHONPATH": str(stub_dir), "MPLBACKEND": "Agg"}
        rc, err, out_dir = run_ok(family_d_data_dir, tmp_path, env=env)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / SUMMARY_CSV).exists(), (
            "CSV must be written even without matplotlib"
        )
        assert "matplotlib" in err.lower() or "warn" in err.lower()


# =========================================================================
# FIX-4 (REQ-3) — real runner layout: pod-prefixed cell_labels + nesting
#
# Family D cell_labels carry a pod prefix (ls-api-, batch-stress-) absent from
# the cell directory names, and latency.csv nests at
# <data-dir>/<timestamp>/<cell>/replicate-<N>/. The analyzer's
# `<cell_label>/**/latency.csv` glob misses everything (TASK-022 verified).
# batch-stress itself contains a dash, so first-dash splitting would turn
# `batch-stress-<cell>` into pod "batch" — the analyzer must resolve cells via
# the filesystem dir names and aggregate BOTH pods' summary rows per cell.
# =========================================================================


class TestRealLayout:
    """FIX-4 REQ-3: discover and analyze the REAL latency-interference layout."""

    def test_discover_latency_finds_nested_files(
        self, real_latency_data_dir: pathlib.Path
    ):
        """Discovery keys are the cell DIR names; both pod rows map to one cell.

        cell_label = ls-api-<cell> and batch-stress-<cell> both resolve to the
        same cell dir; each cell maps to its 2 sorted nested latency.csv files.
        """
        module = load_latency_module()
        summary = module.load_summary(real_latency_data_dir)
        found = module.discover_latency_csvs(real_latency_data_dir, summary)
        assert set(found) == {LAT_CELL_250, LAT_CELL_500}
        for cell in (LAT_CELL_250, LAT_CELL_500):
            assert len(found[cell]) == 2
            assert [p.name for p in found[cell]] == ["latency.csv", "latency.csv"]
            assert all("replicate-" in str(p) for p in found[cell])

    def test_cli_exit_zero_and_csv_written(
        self, real_latency_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-6: analyzer exits 0 on the real fixture and writes both CSVs."""
        rc, err, out_dir = run_ok(real_latency_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / SUMMARY_CSV).exists()
        assert (out_dir / CORRELATION_CSV).exists()

    def test_cli_real_layout_exact_rows(
        self, real_latency_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-3: one row per cell dir with exact percentiles + aggregates.

        cell1 (250m): p50/p95/p99 10.5/19.05/19.81; throttled_usec 18000000
        (both pods x 2 reps); usage 24200000; ratio 1800/4000. cell2 (500m):
        5.5/9.55/9.91; throttled 2000000; usage 16200000; ratio 200/4000.
        """
        rc, err, out_dir = run_ok(real_latency_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        table = pd.read_csv(out_dir / SUMMARY_CSV).set_index("cell")
        assert list(table.columns) == SUMMARY_COLUMNS[1:]
        assert set(table.index) == {LAT_CELL_250, LAT_CELL_500}
        assert len(table) == 2  # both pod rows aggregate into one cell row
        row1 = table.loc[LAT_CELL_250]
        assert row1["p50"] == pytest.approx(CELL1_P50)
        assert row1["p95"] == pytest.approx(CELL1_P95)
        assert row1["p99"] == pytest.approx(CELL1_P99)
        assert row1["throttled_usec"] == 18000000
        assert row1["usage_usec"] == 24200000
        assert row1["throttling_ratio"] == pytest.approx(1800 / 4000)
        row2 = table.loc[LAT_CELL_500]
        assert row2["p50"] == pytest.approx(CELL2_P50)
        assert row2["p95"] == pytest.approx(CELL2_P95)
        assert row2["p99"] == pytest.approx(CELL2_P99)
        assert row2["throttled_usec"] == 2000000
        assert row2["usage_usec"] == 16200000
        assert row2["throttling_ratio"] == pytest.approx(200 / 4000)

    def test_cli_real_layout_correlation(
        self, real_latency_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """The correlation CSV is emitted with the pinned metric rows."""
        rc, err, out_dir = run_ok(real_latency_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        corr = pd.read_csv(out_dir / CORRELATION_CSV).set_index("metric")
        assert list(corr.index) == [
            "p50_vs_throttled_usec",
            "p95_vs_throttled_usec",
            "p99_vs_throttled_usec",
        ]
        # 2 cells, monotonically increasing -> r = 1.0
        assert corr.loc["p99_vs_throttled_usec", "correlation"] == pytest.approx(1.0)

    def test_cli_real_layout_no_missing_latency_warning(
        self, real_latency_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Every cell resolves: no 'no latency.csv files' warning."""
        rc, err, _ = run_ok(real_latency_data_dir, tmp_path)
        assert rc == 0
        assert "no latency.csv files" not in err
