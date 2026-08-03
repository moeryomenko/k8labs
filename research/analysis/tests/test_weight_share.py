"""Tests for weight-share-analyze.py — achieved vs theoretical CPU share.

TASK-014 test-first design, red until TASK-015 implements the script.
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract TASK-015 must build:

    research/analysis/weight-share-analyze.py  (module: weight_share_analyze)
      load_summary(data_dir: Path) -> pd.DataFrame
      compute_weight_shares(summary_df: pd.DataFrame) -> pd.DataFrame
      check_cgroup_completeness(data_dir: Path, summary_df: pd.DataFrame) -> set[str]
      main(argv: list[str] | None = None) -> int

CLI: --data-dir <dir> --output-dir <dir>; writes weight-share-summary.csv with
columns cell,pod,achieved_share,weight_share,ratio_error.

Covered requirements:
  VC-WS-01 exact share math + output CSV schema
  VC-WS-02 2-pod and 3-pod cells
  VC-WS-03 missing cgroup file -> skip cell + warn, no crash
  VC-EMPTY-01 empty input -> header-only output, no crash
  VC-CLI-01 --data-dir/--output-dir contract and exit codes

Run from research/analysis:
    python3 -m pytest tests/test_weight_share.py -q
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import FAMILY_SUMMARY_COLUMNS, WS_CELL_100, WS_CELL_500

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
WEIGHT_SHARE_SCRIPT = ANALYSIS_DIR / "weight-share-analyze.py"
OUTPUT_CSV = "weight-share-summary.csv"
OUTPUT_COLUMNS = ["cell", "pod", "achieved_share", "weight_share", "ratio_error"]


# =========================================================================
# Helpers
# =========================================================================


def load_weight_share_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location(
        "weight_share_analyze", WEIGHT_SHARE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {WEIGHT_SHARE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_weight_share(argv: list[str]) -> tuple[int, str, str]:
    """Run weight-share-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(WEIGHT_SHARE_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    fixture_dir: pathlib.Path, tmp_path: pathlib.Path, extra: list[str] | None = None
):
    """Run the script against a fixture and return (rc, stderr, output path)."""
    out_dir = tmp_path / "output"
    rc, _out, err = run_weight_share(
        ["--data-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or [])
    )
    return rc, err, out_dir / OUTPUT_CSV


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        """weight-share-analyze.py exposes load_summary/compute_weight_shares/main."""
        module = load_weight_share_module()
        for name in (
            "load_summary",
            "compute_weight_shares",
            "check_cgroup_completeness",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# VC-WS-01 — exact share math (pure function)
# =========================================================================


class TestComputeWeightShares:
    """Exact achieved/weight share math on small fixtures."""

    @pytest.fixture
    def summary_df(self, family_a_data_dir: pathlib.Path) -> pd.DataFrame:
        return pd.read_csv(family_a_data_dir / "summary.csv")

    def test_2pod_cell_exact_math(self, summary_df: pd.DataFrame):
        """weights 59/100, usage 60000/100000 -> achieved 0.375/0.625, error +-0.003931."""
        module = load_weight_share_module()
        result = module.compute_weight_shares(summary_df)
        rows = result[result["cell"] == "a=500m;b=500m"].set_index("pod")

        assert set(rows.index) == {"a", "b"}
        assert rows.loc["a", "achieved_share"] == pytest.approx(0.375, abs=1e-9)
        assert rows.loc["b", "achieved_share"] == pytest.approx(0.625, abs=1e-9)
        assert rows.loc["a", "weight_share"] == pytest.approx(59 / 159, abs=1e-9)
        assert rows.loc["b", "weight_share"] == pytest.approx(100 / 159, abs=1e-9)
        assert rows.loc["a", "ratio_error"] == pytest.approx(0.375 - 59 / 159, abs=1e-9)
        assert rows.loc["b", "ratio_error"] == pytest.approx(
            0.625 - 100 / 159, abs=1e-9
        )

    def test_3pod_cell_exact_math(self, summary_df: pd.DataFrame):
        """weights 59/100/40, usage 60000/100000/40000 -> achieved 0.3/0.5/0.2."""
        module = load_weight_share_module()
        result = module.compute_weight_shares(summary_df)
        rows = result[result["cell"] == "a=300m;b=600m;c=600m"].set_index("pod")

        assert set(rows.index) == {"a", "b", "c"}
        assert rows.loc["a", "achieved_share"] == pytest.approx(0.3, abs=1e-9)
        assert rows.loc["b", "achieved_share"] == pytest.approx(0.5, abs=1e-9)
        assert rows.loc["c", "achieved_share"] == pytest.approx(0.2, abs=1e-9)
        assert rows.loc["a", "weight_share"] == pytest.approx(59 / 199, abs=1e-9)
        assert rows.loc["b", "weight_share"] == pytest.approx(100 / 199, abs=1e-9)
        assert rows.loc["c", "weight_share"] == pytest.approx(40 / 199, abs=1e-9)

    def test_ratio_errors_conserve_to_zero(self, summary_df: pd.DataFrame):
        """Signed ratio_error per cell sums to ~0 (shares sum to 1)."""
        module = load_weight_share_module()
        result = module.compute_weight_shares(summary_df)
        for cell in ("a=500m;b=500m", "a=300m;b=600m;c=600m"):
            cell_rows = result[result["cell"] == cell]
            assert cell_rows["ratio_error"].sum() == pytest.approx(0.0, abs=1e-9)

    def test_output_columns_match_contract(self, summary_df: pd.DataFrame):
        """Result DataFrame has exactly the pinned output columns."""
        module = load_weight_share_module()
        result = module.compute_weight_shares(summary_df)
        assert list(result.columns) == OUTPUT_COLUMNS

    def test_aggregation_sums_replicates_then_divides(self):
        """Pinned aggregation: achieved = sum(usage per pod) / sum(all pods).

        With per-replicate ratios 0.375 (60000/100000) and 0.45 (90000/110000),
        aggregate-then-divide gives 150000/360000 = 0.4166667, NOT the mean of
        per-replicate shares (0.4125). This pins the VC-WS-01 math.
        """
        module = load_weight_share_module()
        df = pd.DataFrame(  # type: ignore
            [
                ("a-cell", 1, 0, 0, 0, 60000, 59, 50000),
                ("b-cell", 1, 0, 0, 0, 100000, 100, 50000),
                ("a-cell", 2, 0, 0, 0, 90000, 59, 50000),
                ("b-cell", 2, 0, 0, 0, 110000, 100, 50000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        result = module.compute_weight_shares(df).set_index("pod")
        assert result.loc["a", "achieved_share"] == pytest.approx(
            150000 / 360000, abs=1e-9
        )
        assert result.loc["a", "achieved_share"] != pytest.approx(0.4125, abs=1e-9)

    def test_single_pod_unprefixed_label(self):
        """cell_label without a '-' prefix -> pod equals the whole label, share 1.0."""
        module = load_weight_share_module()
        df = pd.DataFrame(  # type: ignore
            [("a=500m", 1, 0, 0, 0, 12345, 59, 50000)],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        result = module.compute_weight_shares(df)
        assert len(result) == 1
        assert result.iloc[0]["pod"] == "a=500m"
        assert result.iloc[0]["cell"] == "a=500m"
        assert result.iloc[0]["achieved_share"] == pytest.approx(1.0)
        assert result.iloc[0]["weight_share"] == pytest.approx(1.0)

    def test_zero_total_usage_does_not_crash(self):
        """Zero total usage must not divide by zero: achieved_share becomes 0.0."""
        module = load_weight_share_module()
        df = pd.DataFrame(  # type: ignore
            [
                ("a-cell", 1, 0, 0, 0, 0, 59, 50000),
                ("b-cell", 1, 0, 0, 0, 0, 100, 50000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        result = module.compute_weight_shares(df)
        assert bool(result["achieved_share"].isna().any()) is False
        assert result["achieved_share"].eq(0.0).all()
        assert result.set_index("pod").loc["a", "weight_share"] == pytest.approx(
            59 / 159
        )


# =========================================================================
# load_summary
# =========================================================================


class TestLoadSummary:
    """Reading summary.csv from a data dir."""

    def test_reads_summary_rows(self, family_a_data_dir: pathlib.Path):
        """load_summary returns every pod/replicate row with the 8-column schema."""
        module = load_weight_share_module()
        df = module.load_summary(family_a_data_dir)
        assert list(df.columns) == FAMILY_SUMMARY_COLUMNS
        assert len(df) == 2 * 3 + 3 * 2  # 6 rows for 2-pod cell, 6 for 3-pod cell

    def test_missing_summary_raises(self, tmp_path: pathlib.Path):
        """Missing summary.csv raises FileNotFoundError with a clear message."""
        module = load_weight_share_module()
        with pytest.raises(FileNotFoundError):
            module.load_summary(tmp_path / "does-not-exist")


# =========================================================================
# VC-WS-03 — missing cgroup file handling (pure function + CLI)
# =========================================================================


class TestCgroupCompleteness:
    """A cell missing a per-pod cgroup file is reported as incomplete."""

    def test_complete_fixture_has_no_incomplete_cells(
        self, family_a_data_dir: pathlib.Path
    ):
        module = load_weight_share_module()
        summary = module.load_summary(family_a_data_dir)
        assert module.check_cgroup_completeness(family_a_data_dir, summary) == set()

    def test_missing_cgroup_file_reported(
        self, incomplete_cgroup_data_dir: pathlib.Path
    ):
        """Only the incomplete cell is flagged; the complete cell is not."""
        module = load_weight_share_module()
        summary = module.load_summary(incomplete_cgroup_data_dir)
        incomplete = module.check_cgroup_completeness(
            incomplete_cgroup_data_dir, summary
        )
        assert incomplete == {"x=200m;y=200m"}


# =========================================================================
# VC-CLI-01 — CLI contract
# =========================================================================


class TestCli:
    """--data-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_weight_share(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--data-dir" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_weight_share([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --data-dir -> non-zero exit and a clear message."""
        out_dir = tmp_path / "output"
        rc, _out, err = run_weight_share(
            ["--data-dir", str(tmp_path / "missing"), "--output-dir", str(out_dir)]
        )
        assert rc != 0
        # "missing" is the nonexistent dir name: the real script must name the
        # path it could not find. The Python "can't open file" fallback for the
        # missing script does not contain it, so this stays red in the red phase.
        assert "missing" in err


# =========================================================================
# End-to-end: fixture data -> CSV output
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract CSV."""

    def test_happy_path_outputs_exact_csv(
        self, family_a_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-WS-01 + VC-CLI-01: exit 0, CSV with exact shares for both cells."""
        rc, err, csv_path = run_ok(family_a_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert csv_path.exists(), f"missing output: {csv_path}"
        result = pd.read_csv(csv_path)
        assert list(result.columns) == OUTPUT_COLUMNS
        assert len(result) == 5  # 2 pods + 3 pods
        rows = result[result["cell"] == "a=500m;b=500m"].set_index("pod")
        assert rows.loc["a", "achieved_share"] == pytest.approx(0.375, abs=1e-9)
        assert rows.loc["a", "weight_share"] == pytest.approx(59 / 159, abs=1e-9)

    def test_empty_summary_outputs_header_only(
        self, empty_summary_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-EMPTY-01: exit 0, header-only output, no crash."""
        rc, err, csv_path = run_ok(empty_summary_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert csv_path.exists()
        text = csv_path.read_text()
        assert text.splitlines()[0] == ",".join(OUTPUT_COLUMNS)
        assert len(text.splitlines()) == 1  # header only

    def test_missing_cgroup_skips_cell_with_warning(
        self, incomplete_cgroup_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-WS-03: exit 0, incomplete cell skipped, warning names the cell."""
        rc, err, csv_path = run_ok(incomplete_cgroup_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert csv_path.exists()
        result = pd.read_csv(csv_path)
        assert set(result["cell"]) == {"a=500m;b=500m"}
        assert "x=200m;y=200m" in err
        assert "warn" in err.lower() or "skip" in err.lower()


# =========================================================================
# FIX-4 (REQ-1) — real runner layout: pod names with dashes + cell dir names
#
# summary cell_label = "pod-a/pod-b/pod-c - <cell>" where <cell> is the full
# matrix cell string that NAMES the directory under <data-dir>/<timestamp>/.
# Naive first-dash splitting yields pod "pod" / cell "a-a_request=..." (the
# TASK-022 verified bug) — the analyzer must resolve cells from the filesystem
# (cell dir names) and map rows by cell_label suffix ("^(pod)-<cell>$").
# =========================================================================


class TestRealLayout:
    """FIX-4 REQ-1: ingest the REAL runner output layout."""

    def test_check_cgroup_completeness_resolves_real_labels(
        self, real_weight_share_data_dir: pathlib.Path
    ):
        """Every expected (replicate, pod) combo is found -> no incomplete cell.

        Pod names pod-a/pod-b/pod-c contain dashes; the completeness gate must
        resolve labels via the filesystem cell dir names, not first-dash split.
        """
        module = load_weight_share_module()
        summary = module.load_summary(real_weight_share_data_dir)
        assert (
            module.check_cgroup_completeness(real_weight_share_data_dir, summary)
            == set()
        )

    def test_cli_exit_zero_and_csv_written(
        self, real_weight_share_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-6: analyzer exits 0 on the real fixture and writes the CSV."""
        rc, err, csv_path = run_ok(real_weight_share_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert csv_path.exists()

    def test_cli_real_layout_cells_are_dir_names(
        self, real_weight_share_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Output cells are the REAL cell dir names (never the naive split)."""
        rc, err, csv_path = run_ok(real_weight_share_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        result = pd.read_csv(csv_path)
        assert list(result.columns) == OUTPUT_COLUMNS
        assert set(result["cell"]) == {WS_CELL_500, WS_CELL_100}
        assert set(result["pod"]) == {"pod-a", "pod-b", "pod-c"}
        assert len(result) == 6  # 3 pods x 2 cells

    def test_cli_real_layout_exact_shares(
        self, real_weight_share_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-1: exact achieved/weight shares per pod, BestEffort included.

        cell1 (500/500): achieved 12/33, 20/33, 1/33; weight 59/160, 100/160,
        1/160. cell2 (100/500): achieved 6/37, 30/37, 1/37; weight 17/118,
        100/118, 1/118. pod-c is BestEffort with weight 1 and IS present.
        """
        rc, err, csv_path = run_ok(real_weight_share_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        result = pd.read_csv(csv_path)
        rows = result[result["cell"] == WS_CELL_500].set_index("pod")
        assert rows.loc["pod-a", "achieved_share"] == pytest.approx(12 / 33, abs=1e-9)
        assert rows.loc["pod-b", "achieved_share"] == pytest.approx(20 / 33, abs=1e-9)
        assert rows.loc["pod-c", "achieved_share"] == pytest.approx(1 / 33, abs=1e-9)
        assert rows.loc["pod-a", "weight_share"] == pytest.approx(59 / 160, abs=1e-9)
        assert rows.loc["pod-b", "weight_share"] == pytest.approx(100 / 160, abs=1e-9)
        assert rows.loc["pod-c", "weight_share"] == pytest.approx(1 / 160, abs=1e-9)
        assert rows.loc["pod-a", "ratio_error"] == pytest.approx(
            12 / 33 - 59 / 160, abs=1e-9
        )
        rows2 = result[result["cell"] == WS_CELL_100].set_index("pod")
        assert rows2.loc["pod-a", "achieved_share"] == pytest.approx(6 / 37, abs=1e-9)
        assert rows2.loc["pod-b", "achieved_share"] == pytest.approx(30 / 37, abs=1e-9)
        assert rows2.loc["pod-c", "achieved_share"] == pytest.approx(1 / 37, abs=1e-9)
        assert rows2.loc["pod-a", "weight_share"] == pytest.approx(17 / 118, abs=1e-9)
        assert rows2.loc["pod-b", "weight_share"] == pytest.approx(100 / 118, abs=1e-9)
        assert rows2.loc["pod-c", "weight_share"] == pytest.approx(1 / 118, abs=1e-9)

    def test_cli_real_layout_no_skipped_cells(
        self, real_weight_share_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Every cell resolves and is complete: no 'skipping cell' warning."""
        rc, err, _ = run_ok(real_weight_share_data_dir, tmp_path)
        assert rc == 0
        assert "skipping cell" not in err
