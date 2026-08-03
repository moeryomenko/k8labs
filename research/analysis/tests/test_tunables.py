"""Tests for tunables-analyze.py — tunable sweep under contention analysis.

TASK-016 test-first design, red until the script is implemented.
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract the implementation must build:

    research/analysis/tunables-analyze.py  (module: tunables_analyze)
      load_summary(data_dir: Path) -> pd.DataFrame
      discover_replicates(data_dir: Path, summary_df: pd.DataFrame)
          -> dict[str, list[Path]]
      build_comparison(summary_df: pd.DataFrame,
                       replicate_dirs_by_cell: dict[str, list[Path]])
          -> pd.DataFrame
      build_significance(comparison_df: pd.DataFrame) -> pd.DataFrame
      main(argv: list[str] | None = None) -> int

Per-replicate p99 comes from latency_stats.percentiles_from_csv on
<replicate>/latency.csv; per-replicate mean slice duration from the mean of
duration_us in <replicate>/eevdf-slices.csv. A replicate counts only when both
files parse. Group stats are mean / sample std (ddof=1) across replicates.

CLI: --data-dir <dir> --output-dir <dir>; writes tunables-comparison.csv
(tunable,mean_p99,std_p99,mean_slice_us,std_slice_us,n),
tunables-significance.csv
(tunable,mean_p99,default_mean_p99,diff_p99,noise_threshold,significant) and a
lazy, non-fatal tunables-p99.png.

Significance rule (pinned): compared to the tunable named exactly "default",
diff_p99 = mean_p99 - default_mean_p99 (signed); a difference is beyond noise
when abs(diff_p99) > max(std_p99_tunable, std_p99_default). No "default" group
-> header-only significance file with a warning.

Covered requirements:
  REQ-5 (VC-TUN-01) comparison + significance math, exact values
  REQ-6 (VC-CLI-01) --data-dir/--output-dir contract and exit codes
  REQ-6 (VC-EMPTY-01) empty input -> header-only output, no crash
  REQ-7 (VC-MPL-01) matplotlib lazy import, headless/non-fatal

Run from research/analysis:
    python3 -m pytest tests/test_tunables.py -q
"""

from __future__ import annotations

import importlib.util
import math
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    FAMILY_SUMMARY_COLUMNS,
    write_eevdf_slices_csv,
    write_latency_csv,
    write_summary_csv,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
TUN_SCRIPT = ANALYSIS_DIR / "tunables-analyze.py"
COMPARISON_CSV = "tunables-comparison.csv"
SIGNIFICANCE_CSV = "tunables-significance.csv"
OUTPUT_PNG = "tunables-p99.png"
COMPARISON_COLUMNS = [
    "tunable",
    "mean_p99",
    "std_p99",
    "mean_slice_us",
    "std_slice_us",
    "n",
]
SIGNIFICANCE_COLUMNS = [
    "tunable",
    "mean_p99",
    "default_mean_p99",
    "diff_p99",
    "noise_threshold",
    "significant",
]

# Family F fixture expected values (hand-computed, std is sample ddof=1).
DEFAULT = "default"
EXPECTED_COMPARISON = {
    "default": (12.0, 0.0, 1000.0, 100.0, 3),
    "base-slice-low": (6.0, 0.0, 500.0, 50.0, 3),
    "base-slice-mid": (12.5, 0.5, 1000.0, 100.0, 3),
    "base-slice-high": (18.0, 0.0, 1500.0, 150.0, 3),
}
EXPECTED_SIGNIFICANCE = {
    "base-slice-low": (6.0, 12.0, -6.0, 0.0, True),
    "base-slice-mid": (12.5, 12.0, 0.5, 0.5, False),
    "base-slice-high": (18.0, 12.0, 6.0, 0.0, True),
}

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_tunables_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("tunables_analyze", TUN_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {TUN_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tunables(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run tunables-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(TUN_SCRIPT), *argv],
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
    rc, _out, err = run_tunables(
        ["--data-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or []),
        env=env,
    )
    return rc, err, out_dir


def fixture_dirs(fixture_dir: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """Replicate dirs per tunable directly from the fixture layout."""
    return {
        tun: sorted((fixture_dir / tun).glob("replicate-*"))
        for tun in EXPECTED_COMPARISON
        if (fixture_dir / tun).exists()
    }


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        """tunables-analyze.py exposes the pinned public functions."""
        module = load_tunables_module()
        for name in (
            "load_summary",
            "discover_replicates",
            "build_comparison",
            "build_significance",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# load_summary / discovery
# =========================================================================


class TestLoadSummary:
    """Reading summary.csv from a data dir."""

    def test_reads_summary_rows(self, family_f_data_dir: pathlib.Path):
        """load_summary returns every (tunable, replicate) row, 8-column schema."""
        module = load_tunables_module()
        df = module.load_summary(family_f_data_dir)
        assert list(df.columns) == FAMILY_SUMMARY_COLUMNS
        assert len(df) == 12  # 4 tunables x 3 replicates

    def test_missing_summary_raises(self, tmp_path: pathlib.Path):
        """Missing summary.csv raises FileNotFoundError with a clear message."""
        module = load_tunables_module()
        with pytest.raises(FileNotFoundError):
            module.load_summary(tmp_path / "does-not-exist")


class TestDiscoverReplicates:
    """Locating per-cell replicate directories, sorted."""

    def test_finds_sorted_replicate_dirs(self, family_f_data_dir: pathlib.Path):
        module = load_tunables_module()
        summary = module.load_summary(family_f_data_dir)
        found = module.discover_replicates(family_f_data_dir, summary)
        assert set(found) == set(EXPECTED_COMPARISON)
        for tun in EXPECTED_COMPARISON:
            assert len(found[tun]) == 3
            assert [p.name for p in found[tun]] == [
                "replicate-1",
                "replicate-2",
                "replicate-3",
            ]


# =========================================================================
# VC-TUN-01 — comparison table (exact math)
# =========================================================================


class TestBuildComparison:
    """Group stats across replicates for each tunable set."""

    def test_exact_table(self, family_f_data_dir: pathlib.Path):
        """Every tunable row matches the hand-computed mean/std/n values."""
        module = load_tunables_module()
        summary = module.load_summary(family_f_data_dir)
        dirs = module.discover_replicates(family_f_data_dir, summary)
        table = module.build_comparison(summary, dirs).set_index("tunable")
        assert list(table.columns) == COMPARISON_COLUMNS[1:]
        assert set(table.index) == set(EXPECTED_COMPARISON)
        for tun, (
            mean_p99,
            std_p99,
            mean_slice,
            std_slice,
            n,
        ) in EXPECTED_COMPARISON.items():
            row = table.loc[tun]
            assert row["mean_p99"] == pytest.approx(mean_p99)
            assert row["std_p99"] == pytest.approx(std_p99)
            assert row["mean_slice_us"] == pytest.approx(mean_slice)
            assert row["std_slice_us"] == pytest.approx(std_slice)
            assert row["n"] == n

    def test_std_is_sample_ddof1(self, tmp_path: pathlib.Path):
        """Pinned: std uses sample ddof=1, so slice std 100.0 (not 81.65)."""
        module = load_tunables_module()
        root = tmp_path / "mini"
        for rep, slice_mean in ((1, 1000.0), (2, 1100.0), (3, 900.0)):
            rep_dir = root / DEFAULT / f"replicate-{rep}"
            write_latency_csv(rep_dir / "latency.csv", [12.0] * 10)
            write_eevdf_slices_csv(rep_dir / "eevdf-slices.csv", [slice_mean] * 3)
        write_summary_csv(
            root / "summary.csv",
            [(DEFAULT, rep, 0, 0, 0, 0, 59, 100000) for rep in (1, 2, 3)],
        )
        summary = module.load_summary(root)
        dirs = module.discover_replicates(root, summary)
        table = module.build_comparison(summary, dirs)
        row = table.iloc[0]
        assert row["mean_slice_us"] == pytest.approx(1000.0)
        assert row["std_slice_us"] == pytest.approx(100.0)  # ddof=1
        assert row["std_slice_us"] != pytest.approx(math.sqrt(20000.0 / 3.0))

    def test_default_tunable_ordered_first(self, family_f_data_dir: pathlib.Path):
        """Output rows: 'default' first, then the rest sorted by name."""
        module = load_tunables_module()
        summary = module.load_summary(family_f_data_dir)
        dirs = module.discover_replicates(family_f_data_dir, summary)
        table = module.build_comparison(summary, dirs)
        assert table["tunable"].tolist() == [
            "default",
            "base-slice-high",
            "base-slice-low",
            "base-slice-mid",
        ]

    def test_missing_slice_file_reduces_n(self, tmp_path: pathlib.Path):
        """A replicate missing eevdf-slices.csv is not counted (n drops).

        base-slice-low has latency.csv in both replicates but eevdf-slices.csv
        in only one -> n=1 and stats come from the complete replicate only.
        """
        module = load_tunables_module()
        tun = "base-slice-low"
        root = tmp_path / "mini"
        for rep in (1, 2):
            write_latency_csv(
                root / tun / f"replicate-{rep}" / "latency.csv", [6.0] * 10
            )
        write_eevdf_slices_csv(
            root / tun / "replicate-1" / "eevdf-slices.csv", [500.0] * 3
        )
        write_summary_csv(
            root / "summary.csv",
            [
                (tun, 1, 1000, 0, 0, 100000, 59, 100000),
                (tun, 2, 1000, 0, 0, 100000, 59, 100000),
            ],
        )
        summary = module.load_summary(root)
        dirs = module.discover_replicates(root, summary)
        assert len(dirs[tun]) == 2
        table = module.build_comparison(summary, dirs).set_index("tunable")
        row = table.loc[tun]
        assert row["n"] == 1
        assert row["mean_p99"] == pytest.approx(6.0)
        assert row["mean_slice_us"] == pytest.approx(500.0)

    def test_degraded_fixture_n2(self, family_f_degraded_data_dir: pathlib.Path):
        """Degraded fixture: base-slice-low drops to n=2 and slice stats shift."""
        module = load_tunables_module()
        summary = module.load_summary(family_f_degraded_data_dir)
        dirs = module.discover_replicates(family_f_degraded_data_dir, summary)
        table = module.build_comparison(summary, dirs).set_index("tunable")
        row = table.loc["base-slice-low"]
        assert row["n"] == 2
        assert row["mean_p99"] == pytest.approx(6.0)
        assert row["std_p99"] == pytest.approx(0.0)
        assert row["mean_slice_us"] == pytest.approx(475.0)  # (500 + 450) / 2
        assert row["std_slice_us"] == pytest.approx(math.sqrt(1250.0))
        assert table.loc[DEFAULT, "n"] == 3

    def test_empty_summary_returns_empty_table(self):
        """Empty summary -> empty DataFrame with the pinned columns."""
        module = load_tunables_module()
        empty = pd.DataFrame(columns=FAMILY_SUMMARY_COLUMNS)  # type: ignore
        table = module.build_comparison(empty, {})
        assert list(table.columns) == COMPARISON_COLUMNS
        assert len(table) == 0


# =========================================================================
# VC-TUN-01 — significance note
# =========================================================================


class TestBuildSignificance:
    """diff vs 'default' beyond noise = max(std_p99 of the two)."""

    def test_exact_significance(self, family_f_data_dir: pathlib.Path):
        """True/False flags match diff > max(std) with signed diff_p99."""
        module = load_tunables_module()
        summary = module.load_summary(family_f_data_dir)
        dirs = module.discover_replicates(family_f_data_dir, summary)
        comparison = module.build_comparison(summary, dirs)
        table = module.build_significance(comparison).set_index("tunable")
        assert list(table.columns) == SIGNIFICANCE_COLUMNS[1:]
        assert set(table.index) == {
            "base-slice-low",
            "base-slice-mid",
            "base-slice-high",
        }
        for tun, (mean, default, diff, threshold, sig) in EXPECTED_SIGNIFICANCE.items():
            row = table.loc[tun]
            assert row["mean_p99"] == pytest.approx(mean)
            assert row["default_mean_p99"] == pytest.approx(default)
            assert row["diff_p99"] == pytest.approx(diff)
            assert row["noise_threshold"] == pytest.approx(threshold)
            assert bool(row["significant"]) is sig

    def test_boundary_at_threshold_is_not_significant(self):
        """Strict rule: diff == threshold (0.5 == 0.5) is NOT beyond noise."""
        module = load_tunables_module()
        comparison = pd.DataFrame(
            [
                ("default", 12.0, 0.0, 1000.0, 100.0, 3),
                ("base-slice-mid", 12.5, 0.5, 1000.0, 100.0, 3),
            ],
            columns=COMPARISON_COLUMNS,  # type: ignore
        )
        table = module.build_significance(comparison).set_index("tunable")
        row = table.loc["base-slice-mid"]
        assert row["diff_p99"] == pytest.approx(0.5)
        assert row["noise_threshold"] == pytest.approx(0.5)
        assert bool(row["significant"]) is False

    def test_missing_default_returns_empty_table(self):
        """No 'default' group -> empty DataFrame with the pinned columns."""
        module = load_tunables_module()
        comparison = pd.DataFrame(
            [
                ("base-slice-low", 6.0, 0.0, 500.0, 50.0, 3),
                ("base-slice-high", 18.0, 0.0, 1500.0, 150.0, 3),
            ],
            columns=COMPARISON_COLUMNS,  # type: ignore
        )
        table = module.build_significance(comparison)
        assert list(table.columns) == SIGNIFICANCE_COLUMNS
        assert len(table) == 0


# =========================================================================
# VC-CLI-01 — CLI contract
# =========================================================================


class TestCli:
    """--data-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_tunables(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--data-dir" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_tunables([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --data-dir -> non-zero exit and a clear message."""
        out_dir = tmp_path / "output"
        rc, _out, err = run_tunables(
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
        self, family_f_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-TUN-01 + VC-CLI-01: exit 0, exact comparison and significance CSVs."""
        rc, err, out_dir = run_ok(family_f_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison = pd.read_csv(out_dir / COMPARISON_CSV).set_index("tunable")
        assert list(comparison.columns) == COMPARISON_COLUMNS[1:]
        assert comparison.loc["default", "mean_p99"] == pytest.approx(12.0)
        assert comparison.loc["base-slice-mid", "std_p99"] == pytest.approx(0.5)
        assert comparison.loc["base-slice-low", "mean_slice_us"] == pytest.approx(500.0)
        assert comparison.loc["base-slice-high", "std_slice_us"] == pytest.approx(150.0)
        significance = pd.read_csv(out_dir / SIGNIFICANCE_CSV).set_index("tunable")
        assert list(significance.columns) == SIGNIFICANCE_COLUMNS[1:]
        assert bool(significance.loc["base-slice-low", "significant"]) is True
        assert bool(significance.loc["base-slice-mid", "significant"]) is False
        assert significance.loc["base-slice-high", "diff_p99"] == pytest.approx(6.0)

    def test_no_default_significance_header_only_with_warning(
        self, family_f_no_default_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Missing 'default' tunable -> header-only significance file + warning."""
        rc, err, out_dir = run_ok(family_f_no_default_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison = pd.read_csv(out_dir / COMPARISON_CSV)
        assert set(comparison["tunable"]) == {
            "base-slice-high",
            "base-slice-low",
            "base-slice-mid",
        }
        sig_lines = (out_dir / SIGNIFICANCE_CSV).read_text().splitlines()
        assert sig_lines == [",".join(SIGNIFICANCE_COLUMNS)]
        assert "default" in err

    def test_empty_summary_outputs_header_only(
        self, empty_summary_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-EMPTY-01: exit 0, header-only CSVs, no crash."""
        rc, err, out_dir = run_ok(empty_summary_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison_lines = (out_dir / COMPARISON_CSV).read_text().splitlines()
        assert comparison_lines == [",".join(COMPARISON_COLUMNS)]
        sig_lines = (out_dir / SIGNIFICANCE_CSV).read_text().splitlines()
        assert sig_lines == [",".join(SIGNIFICANCE_COLUMNS)]

    @pytest.mark.skipif(
        not importlib.util.find_spec("matplotlib"), reason="matplotlib not installed"
    )
    def test_png_rendered(
        self, family_f_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: real matplotlib (Agg) emits a valid PNG."""
        rc, err, out_dir = run_ok(family_f_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        png_path = out_dir / OUTPUT_PNG
        assert png_path.exists(), f"missing plot: {png_path}"
        assert png_path.read_bytes()[:4] == b"\x89PNG"

    def test_matplotlib_import_failure_is_nonfatal(
        self, family_f_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: broken matplotlib must not block the CSV outputs."""
        stub_dir = tmp_path / "stub-matplotlib"
        stub_dir.mkdir()
        (stub_dir / "matplotlib.py").write_text(
            'raise ImportError("stubbed out for tests")\n'
        )
        env = {**os.environ, "PYTHONPATH": str(stub_dir), "MPLBACKEND": "Agg"}
        rc, err, out_dir = run_ok(family_f_data_dir, tmp_path, env=env)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / COMPARISON_CSV).exists(), (
            "CSV must be written even without matplotlib"
        )
        assert "matplotlib" in err.lower() or "warn" in err.lower()


# =========================================================================
# FIX-4 (REQ-4) — real runner layout + slice-optional rule
#
# Family F runs WITHOUT --eevdf: no eevdf-slices.csv exists anywhere in the
# dataset (TASK-022 verified). The analyzer currently counts a replicate only
# when BOTH latency.csv and eevdf-slices.csv parse, so the p99-only verdict is
# impossible. FIX-4 relaxes the rule: a replicate always counts for p99 when
# latency.csv parses; slice-duration columns are computed only from replicates
# that also have eevdf-slices.csv and are NaN when none do. When a tunable has
# SOME slice files the legacy "n = complete replicates" rule is preserved
# (backward compatible with the pre-FIX-4 tests). Cell dirs nest under
# <data-dir>/<timestamp>/<cell>/ and summary cell_labels carry the ls-api/
# batch-stress pod prefix, so discovery must resolve the trailing
# `-tunables=<name>` token.
# =========================================================================


class TestRealLayout:
    """FIX-4 REQ-4: discover tunables in the REAL layout, no slices anywhere."""

    def test_discover_replicates_real_layout(
        self, real_tunables_data_dir: pathlib.Path
    ):
        """Discovery keys are the extracted tunable names; 3 replicate dirs each."""
        module = load_tunables_module()
        summary = module.load_summary(real_tunables_data_dir)
        found = module.discover_replicates(real_tunables_data_dir, summary)
        assert set(found) == {"default", "base-slice-low", "base-slice-high"}
        for tun in ("default", "base-slice-low", "base-slice-high"):
            assert [p.name for p in found[tun]] == [
                "replicate-1",
                "replicate-2",
                "replicate-3",
            ]

    def test_cli_exit_zero_and_csvs_written(
        self, real_tunables_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-6: exit 0 on the real fixture; both CSVs written."""
        rc, err, out_dir = run_ok(real_tunables_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / COMPARISON_CSV).exists()
        assert (out_dir / SIGNIFICANCE_CSV).exists()

    def test_cli_real_layout_no_slices_still_emits_verdict(
        self, real_tunables_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-4: comparison rows with NaN slice columns + significance verdict.

        No eevdf-slices.csv anywhere: mean_p99 from latency.csv alone (default
        12.0, low 6.0, high 18.0), n=3, slice columns NaN. Significance: low
        diff -6 significant, high diff +6 significant. No crash, exit 0.
        """
        rc, err, out_dir = run_ok(real_tunables_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison = pd.read_csv(out_dir / COMPARISON_CSV).set_index("tunable")
        assert list(comparison.columns) == COMPARISON_COLUMNS[1:]
        assert set(comparison.index) == {"default", "base-slice-low", "base-slice-high"}
        assert comparison.loc["default", "mean_p99"] == pytest.approx(12.0)
        assert comparison.loc["base-slice-low", "mean_p99"] == pytest.approx(6.0)
        assert comparison.loc["base-slice-high", "mean_p99"] == pytest.approx(18.0)
        assert pd.isna(comparison.loc["default", "mean_slice_us"])
        assert pd.isna(comparison.loc["default", "std_slice_us"])
        assert comparison.loc["default", "n"] == 3
        significance = pd.read_csv(out_dir / SIGNIFICANCE_CSV).set_index("tunable")
        assert list(significance.columns) == SIGNIFICANCE_COLUMNS[1:]
        assert set(significance.index) == {"base-slice-low", "base-slice-high"}
        assert significance.loc["base-slice-low", "diff_p99"] == pytest.approx(-6.0)
        assert bool(significance.loc["base-slice-low", "significant"]) is True
        assert significance.loc["base-slice-high", "diff_p99"] == pytest.approx(6.0)
        assert bool(significance.loc["base-slice-high", "significant"]) is True


class TestSliceOptional:
    """FIX-4 slice-optional rule in the FLAT layout (no pod prefix).

    Proves the relaxation independent of the cell-dir discovery change, in the
    exact layout the pre-FIX-4 tests use.
    """

    def test_comparison_without_slices_emits_rows(
        self, flat_noslices_tunables_data_dir: pathlib.Path
    ):
        """latency.csv alone yields rows: n=3, slice columns NaN."""
        module = load_tunables_module()
        summary = module.load_summary(flat_noslices_tunables_data_dir)
        dirs = module.discover_replicates(flat_noslices_tunables_data_dir, summary)
        table = module.build_comparison(summary, dirs).set_index("tunable")
        assert set(table.index) == {"default", "base-slice-low", "base-slice-high"}
        assert table.loc["default", "mean_p99"] == pytest.approx(12.0)
        assert table.loc["default", "n"] == 3
        assert pd.isna(table.loc["default", "mean_slice_us"])
        assert pd.isna(table.loc["default", "std_slice_us"])

    def test_significance_without_slices_emits_verdict(
        self, flat_noslices_tunables_data_dir: pathlib.Path
    ):
        """The p99-only diff > max(std_p99) verdict is still produced."""
        module = load_tunables_module()
        summary = module.load_summary(flat_noslices_tunables_data_dir)
        dirs = module.discover_replicates(flat_noslices_tunables_data_dir, summary)
        comparison = module.build_comparison(summary, dirs)
        significance = module.build_significance(comparison).set_index("tunable")
        assert set(significance.index) == {"base-slice-low", "base-slice-high"}
        assert bool(significance.loc["base-slice-low", "significant"]) is True

    def test_cli_without_slices_exit_zero(
        self, flat_noslices_tunables_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-6: flat no-slices fixture exits 0 and populates both CSVs."""
        rc, err, out_dir = run_ok(flat_noslices_tunables_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison = pd.read_csv(out_dir / COMPARISON_CSV)
        assert len(comparison) == 3
        significance = pd.read_csv(out_dir / SIGNIFICANCE_CSV)
        assert len(significance) == 2
