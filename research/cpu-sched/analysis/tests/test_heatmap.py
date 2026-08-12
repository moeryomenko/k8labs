"""Tests for interaction-heatmap.py — request x limit interaction heatmap.

Test-first design, red until the script is implemented.
The module/function/CLI names used here are the contract the implementation must build:

    research/cpu-sched/analysis/interaction-heatmap.py  (module: interaction_heatmap)
      parse_cell_label(label: str) -> tuple[int, int] | None
      build_heatmap(summary_df: pd.DataFrame, value: str = "throttling_ratio")
          -> pd.DataFrame
      main(argv: list[str] | None = None) -> int

CLI: --data-dir <dir> --output-dir <dir> [--value {throttling_ratio,usage}]
writes heatmap-<value>.csv (pivot table: rows=request, cols=limit) and
heatmap-<value>.png (matplotlib; rendering is lazy and non-fatal so the CSV
is produced headless).

Covered behavior:
  pivot table CSV (rows=request, cols=limit, values=ratio|usage)
  matplotlib heatmap emission, headless-safe
  empty input -> header-only output, no crash
  --data-dir/--output-dir contract and exit codes

Run from research/cpu-sched/analysis:
    python3 -m pytest tests/test_heatmap.py -q
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import FAMILY_SUMMARY_COLUMNS

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
HEATMAP_SCRIPT = ANALYSIS_DIR / "interaction-heatmap.py"
VALUE_CSV = "heatmap-throttling_ratio.csv"
USAGE_CSV = "heatmap-usage.csv"
RATIO_PNG = "heatmap-throttling_ratio.png"

# Expected throttling ratios per (request, limit) in the Family B fixture.
EXPECTED_RATIOS = {
    (100, 200): 0.9,
    (100, 500): 0.5,
    (100, 1000): 0.2,
    (100, 2000): float("nan"),
    (500, 200): float("nan"),
    (500, 500): float("nan"),
    (500, 1000): 0.8,
    (500, 2000): 0.1,
}

EXPECTED_USAGE = {
    (100, 200): 27727824,
    (100, 500): 62563172,
    (100, 1000): 101000000,
    (100, 2000): float("nan"),
    (500, 200): float("nan"),
    (500, 500): float("nan"),
    (500, 1000): 89123456,
    (500, 2000): 180000000,
}

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_heatmap_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("interaction_heatmap", HEATMAP_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {HEATMAP_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_heatmap(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run interaction-heatmap.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(HEATMAP_SCRIPT), *argv],
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
    rc, _out, err = run_heatmap(
        ["--data-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or []),
        env=env,
    )
    return rc, err, out_dir


def assert_pivot_values(csv_path: pathlib.Path, expected: dict[tuple[int, int], float]):
    """Read a heatmap CSV and assert every (request, limit) cell value."""
    df = pd.read_csv(csv_path)
    assert df.columns[0] == "request"
    pivot = df.set_index("request")
    for (req, limit), value in expected.items():
        col = str(limit)
        if pd.isna(value):
            assert (
                col not in pivot.columns or pivot.loc[req, col] != pivot.loc[req, col]
            ), f"expected NaN at ({req}, {limit}), got {pivot.loc[req, col]}"
        else:
            assert pivot.loc[req, col] == pytest.approx(value, abs=1e-9), (
                f"mismatch at ({req}, {limit})"
            )


def assert_is_png(path: pathlib.Path):
    """Assert *path* points to a valid PNG (magic bytes + IHDR)."""
    assert path.exists(), f"File does not exist: {path}"
    assert path.stat().st_size > 0, f"File is empty: {path}"
    data = path.read_bytes()
    assert data[:4] == b"\x89PNG", f"Not a valid PNG (bad magic): {path}"
    assert len(data) > 16 and b"IHDR" in data[12:16], f"Missing IHDR in PNG: {path}"


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        """interaction-heatmap.py exposes parse_cell_label/build_heatmap/main."""
        module = load_heatmap_module()
        for name in ("parse_cell_label", "build_heatmap", "main"):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# parse_cell_label
# =========================================================================


class TestParseCellLabel:
    """Cell label -> (request_m, limit_m) decoding."""

    def test_valid_label(self):
        module = load_heatmap_module()
        assert module.parse_cell_label("request=100m-limit=200m") == (100, 200)

    def test_three_digit_values(self):
        module = load_heatmap_module()
        assert module.parse_cell_label("request=1000m-limit=2000m") == (1000, 2000)

    def test_empty_request_is_unparseable(self):
        """request=-limit=100m (throttling-limits.yaml shape) must yield None."""
        module = load_heatmap_module()
        assert module.parse_cell_label("request=-limit=100m") is None

    def test_prefixed_co_located_label_is_unparseable(self):
        """Family A prefixed labels ('ls-...') must yield None, not crash."""
        module = load_heatmap_module()
        assert module.parse_cell_label("ls-request=100m-limit=200m") is None

    def test_garbage_label_is_unparseable(self):
        module = load_heatmap_module()
        assert module.parse_cell_label("not-a-cell-label") is None


# =========================================================================
# Pivot table construction
# =========================================================================


class TestBuildHeatmap:
    """The request x limit pivot table with exact fixture values."""

    @pytest.fixture
    def summary_df(self, family_b_data_dir: pathlib.Path) -> pd.DataFrame:
        return pd.read_csv(family_b_data_dir / "summary.csv")

    def test_throttling_ratio_pivot_exact_values(self, summary_df: pd.DataFrame):
        """Pivot rows=request, cols=limit with exact per-cell ratios."""
        module = load_heatmap_module()
        pivot = module.build_heatmap(summary_df, value="throttling_ratio")
        assert list(pivot["request"]) == [100, 500]
        assert list(pivot.columns[1:]) == [200, 500, 1000, 2000]
        for (req, limit), value in EXPECTED_RATIOS.items():
            got = pivot.loc[pivot["request"] == req, limit].iloc[0]
            if pd.isna(value):
                assert pd.isna(got), f"expected NaN at ({req}, {limit}), got {got}"
            else:
                assert got == pytest.approx(value, abs=1e-9), (
                    f"mismatch at ({req}, {limit})"
                )

    def test_usage_pivot_exact_values(self, summary_df: pd.DataFrame):
        """--value usage produces the mean usage_usec per (request, limit)."""
        module = load_heatmap_module()
        pivot = module.build_heatmap(summary_df, value="usage")
        for (req, limit), value in EXPECTED_USAGE.items():
            got = pivot.loc[pivot["request"] == req, limit].iloc[0]
            if pd.isna(value):
                assert pd.isna(got), f"expected NaN at ({req}, {limit}), got {got}"
            else:
                assert got == pytest.approx(value, abs=1e-9), (
                    f"mismatch at ({req}, {limit})"
                )

    def test_invalid_value_name_raises(self, summary_df: pd.DataFrame):
        """Unknown metric name is a clear error, not a silent empty table."""
        module = load_heatmap_module()
        with pytest.raises(ValueError):
            module.build_heatmap(summary_df, value="bogus")

    def test_mean_across_replicates(self):
        """Cell value is the MEAN of per-replicate ratios (0.4 and 0.6 -> 0.5)."""
        module = load_heatmap_module()
        df = pd.DataFrame(
            [
                ("request=100m-limit=500m", 1, 1000, 400, 0, 1000, 17, 50000),
                ("request=100m-limit=500m", 2, 1000, 600, 0, 2000, 17, 50000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        pivot = module.build_heatmap(df, value="throttling_ratio")
        got = pivot.loc[pivot["request"] == 100, 500].iloc[0]
        assert got == pytest.approx(0.5, abs=1e-9)

    def test_unparseable_rows_are_skipped(self):
        """Rows with unparseable labels are skipped with a warning, no crash."""
        module = load_heatmap_module()
        df = pd.DataFrame(
            [
                ("request=100m-limit=500m", 1, 1000, 500, 0, 1000, 17, 50000),
                ("request=-limit=100m", 1, 1000, 1000, 0, 1000, 17, 10000),
                ("ls-request=100m-limit=200m", 1, 1000, 100, 0, 1000, 17, 20000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        pivot = module.build_heatmap(df, value="throttling_ratio")
        assert list(pivot["request"]) == [100]
        assert 500 in pivot.columns

    def test_zero_periods_yields_nan_not_crash(self):
        """nr_periods == 0 must yield NaN ratio, never a division exception."""
        module = load_heatmap_module()
        df = pd.DataFrame(
            [("request=100m-limit=500m", 1, 0, 0, 0, 0, 17, 50000)],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        pivot = module.build_heatmap(df, value="throttling_ratio")
        got = pivot.loc[pivot["request"] == 100, 500].iloc[0]
        assert pd.isna(got)

    def test_empty_input_returns_request_column_only(self):
        """Empty input -> DataFrame with a single 'request' column (header only)."""
        module = load_heatmap_module()
        empty = pd.DataFrame(columns=FAMILY_SUMMARY_COLUMNS)  # type: ignore
        pivot = module.build_heatmap(empty, value="throttling_ratio")
        assert list(pivot.columns) == ["request"]
        assert len(pivot) == 0


# =========================================================================
# CLI contract
# =========================================================================


class TestCli:
    """--data-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_heatmap(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--data-dir" in combined
        assert "--output-dir" in combined
        assert "--value" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_heatmap([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --data-dir -> non-zero exit and a clear message."""
        out_dir = tmp_path / "output"
        rc, _out, err = run_heatmap(
            ["--data-dir", str(tmp_path / "missing"), "--output-dir", str(out_dir)]
        )
        assert rc != 0
        # "missing" is the nonexistent dir name: the real script must name the
        # path it could not find. The Python "can't open file" fallback for the
        # missing script does not contain it, so this stays red in the red phase.
        assert "missing" in err


# =========================================================================
# End-to-end: fixture data -> CSV pivot + PNG
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract outputs."""

    def test_happy_path_ratio_csv(
        self, family_b_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Exit 0 and an exact throttling-ratio pivot CSV."""
        rc, err, out_dir = run_ok(family_b_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / VALUE_CSV
        assert csv_path.exists(), f"missing output: {csv_path}"
        assert_pivot_values(csv_path, EXPECTED_RATIOS)

    def test_usage_value_flag_writes_usage_csv(
        self, family_b_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """--value usage produces heatmap-usage.csv with usage means."""
        rc, err, out_dir = run_ok(
            family_b_data_dir, tmp_path, extra=["--value", "usage"]
        )
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / USAGE_CSV
        assert csv_path.exists(), f"missing output: {csv_path}"
        assert_pivot_values(csv_path, EXPECTED_USAGE)

    def test_empty_summary_outputs_header_only(
        self, empty_summary_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Empty input: exit 0, header-only CSV, no crash."""
        rc, err, out_dir = run_ok(empty_summary_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / VALUE_CSV
        assert csv_path.exists()
        lines = csv_path.read_text().splitlines()
        assert lines == ["request"]  # header only

    @pytest.mark.skipif(
        not importlib.util.find_spec("matplotlib"), reason="matplotlib not installed"
    )
    def test_png_heatmap_rendered(
        self, family_b_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Real matplotlib (Agg) emits a valid heatmap PNG."""
        rc, err, out_dir = run_ok(family_b_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert_is_png(out_dir / RATIO_PNG)

    def test_matplotlib_import_failure_is_nonfatal(
        self, family_b_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Broken matplotlib must not block the CSV output.

        A stub matplotlib that raises on import is injected via PYTHONPATH.
        The script must write heatmap-throttling_ratio.csv and exit 0 with a
        warning, proving matplotlib is imported lazily and rendering is
        non-fatal.
        """
        stub_dir = tmp_path / "stub-matplotlib"
        stub_dir.mkdir()
        (stub_dir / "matplotlib.py").write_text(
            'raise ImportError("stubbed out for tests")\n'
        )
        env = {**os.environ, "PYTHONPATH": str(stub_dir), "MPLBACKEND": "Agg"}
        rc, err, out_dir = run_ok(family_b_data_dir, tmp_path, env=env)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / VALUE_CSV).exists(), (
            "CSV must be written even without matplotlib"
        )
        assert "matplotlib" in err.lower() or "warn" in err.lower()
