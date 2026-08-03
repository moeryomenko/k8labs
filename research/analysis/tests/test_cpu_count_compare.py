"""Tests for cpu-count-compare.py — 2-CPU vs 4-CPU weight-share comparison.

TASK-V07 test-first design, red until the script is implemented.
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract the engineer must build:

    research/analysis/cpu-count-compare.py  (module: cpu_count_compare)
      OUTPUT_COMPARISON_CSV = "cpu-count-compare.csv"
      OUTPUT_DETAIL_CSV     = "cpu-count-detail.csv"
      OUTPUT_SCALED_CSV     = "cpu-count-4v-scaled.csv"
      OUTPUT_VERDICT_CSV    = "cpu-count-verdict.txt"
      OUTPUT_PNG            = "cpu-count-compare.png"
      VERDICT_FORMAT        = "mean |ratio_error| {:.3f} -> {:.3f}"
      SCALED_VERDICT_FORMAT = "scaled-4v mean |ratio_error| {:.3f}"
      load_summary_csv(path: Path) -> pd.DataFrame
      ratio_label(cell: str) -> str
      build_comparison(df_2cpu, df_4cpu) -> pd.DataFrame
      build_detail(df_2cpu, df_4cpu) -> pd.DataFrame
      build_scaled_block(df_scaled) -> pd.DataFrame
      verdict_line(comparison_df: pd.DataFrame) -> str
      scaled_verdict_line(scaled_df: pd.DataFrame) -> str
      main(argv: list[str] | None = None) -> int

CLI: --csv-2cpu <file> --csv-4cpu <file> [--csv-4v-scaled <file>]
     --output-dir <dir>. The input files are weight-share-summary.csv outputs
     from weight-share-analyze.py (columns cell,pod,achieved_share,
     weight_share,ratio_error). --csv-4v-scaled is OPTIONAL: when provided the
     scaled-4v block file + verdict line are emitted; when omitted the block
     is skipped with a warning (exit still 0).

Outputs (written to --output-dir):
    cpu-count-compare.csv      cell,ratio_label,error_2cpu,error_4cpu,delta,
                               missing_in          (one row per cell)
    cpu-count-detail.csv       cell,pod,ratio_error_2cpu,ratio_error_4cpu,
                               delta               (one row per pod)
    cpu-count-4v-scaled.csv    cell,ratio_label,error_scaled  (only with
                               --csv-4v-scaled)
    cpu-count-verdict.txt      verdict line(s), one per line
    cpu-count-compare.png      lazy matplotlib (Agg), non-fatal

error_2cpu/error_4cpu = per-cell mean |ratio_error| of the pod rows;
delta = error_4cpu - error_2cpu (negative is an improvement); missing_in is
"both" when the cell is in both runs, "2cpu" when it is only in the 4-CPU
file, "4cpu" when it is only in the 2-CPU file (the missing side is NaN,
never a crash). Verdict means are computed over cells present in BOTH runs.

Covered requirements:
  REQ-1 (VC-CC-01) exact per-cell mean |ratio_error| + delta math on fixtures
  REQ-2 (VC-CC-02) comparison CSV schema; cells in only one run marked missing
  REQ-3 (VC-CC-03) verdict line deterministic per pinned format
  REQ-4 (VC-CC-04) scaled 4-vCPU block as a separate section/file
  REQ-5 (VC-EMPTY-01/VC-MPL-01) empty inputs -> header-only outputs + message;
        matplotlib lazy and non-fatal
  REQ-6 (VC-CLI-01) CLI --output-dir contract: exit 0 on fixtures, non-zero
        + clear message on missing input

Run from research/analysis:
    python3 -m pytest tests/test_cpu_count_compare.py -q
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ANALYSIS_DIR / "cpu-count-compare.py"

OUTPUT_COMPARISON_CSV = "cpu-count-compare.csv"
OUTPUT_DETAIL_CSV = "cpu-count-detail.csv"
OUTPUT_SCALED_CSV = "cpu-count-4v-scaled.csv"
OUTPUT_VERDICT_CSV = "cpu-count-verdict.txt"
OUTPUT_PNG = "cpu-count-compare.png"

COMPARISON_COLUMNS = [
    "cell",
    "ratio_label",
    "error_2cpu",
    "error_4cpu",
    "delta",
    "missing_in",
]
DETAIL_COLUMNS = ["cell", "pod", "ratio_error_2cpu", "ratio_error_4cpu", "delta"]
SCALED_COLUMNS = ["cell", "ratio_label", "error_scaled"]

WEIGHT_SHARE_COLUMNS = ["cell", "pod", "achieved_share", "weight_share", "ratio_error"]

# Pinned verdict numbers for the happy-path fixtures (REQ-1/REQ-3).
VERDICT_2CPU_MEAN = 0.024
VERDICT_4CPU_MEAN = 0.010
VERDICT_SCALED_MEAN = 0.008
VERDICT_LINE = f"mean |ratio_error| {VERDICT_2CPU_MEAN:.3f} -> {VERDICT_4CPU_MEAN:.3f}"
SCALED_VERDICT_LINE = f"scaled-4v mean |ratio_error| {VERDICT_SCALED_MEAN:.3f}"

# Fixture specs: (cell, a_mc, b_mc, e) where pod-a ratio_error = +e and
# pod-b ratio_error = -e (signed errors sum to 0 per cell, so the per-cell
# mean |ratio_error| equals e exactly). Cells mirror weight-share.yaml (2-CPU)
# and weight-share-4v.yaml (scaled). The 2-CPU means sum to 6*0.024=0.144, the
# 4-CPU to 6*0.010=0.060 and the scaled to 6*0.008=0.048.
CELLS_2CPU = [
    ("a=500m;b=500m", 500, 500, 0.030),
    ("a=250m;b=1000m", 250, 1000, 0.024),
    ("a=100m;b=500m", 100, 500, 0.018),
    ("a=100m;b=1000m", 100, 1000, 0.027),
    ("a=800m;b=800m", 800, 800, 0.021),
    ("a=200m;b=500m", 200, 500, 0.024),
]
CELLS_4CPU = [
    ("a=500m;b=500m", 500, 500, 0.012),
    ("a=250m;b=1000m", 250, 1000, 0.010),
    ("a=100m;b=500m", 100, 500, 0.008),
    ("a=100m;b=1000m", 100, 1000, 0.011),
    ("a=800m;b=800m", 800, 800, 0.009),
    ("a=200m;b=500m", 200, 500, 0.010),
]
CELLS_4V = [
    ("a=1500m;b=1500m", 1500, 1500, 0.008),
    ("a=600m;b=3000m", 600, 3000, 0.009),
    ("a=500m;b=1000m", 500, 1000, 0.007),
    ("a=500m;b=3000m", 500, 3000, 0.008),
    ("a=1000m;b=1000m", 1000, 1000, 0.010),
    ("a=750m;b=1500m", 750, 1500, 0.006),
]

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_module():
    """Import the not-yet-existing script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("cpu_count_compare", SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pod_rows(cell: str, a_mc: int, b_mc: int, err: float) -> list[tuple]:
    """Two pod rows (pod-a +err, pod-b -err) consistent with the requests."""
    total = a_mc + b_mc
    weight_a = a_mc / total
    weight_b = b_mc / total
    return [
        (cell, "pod-a", weight_a + err, weight_a, err),
        (cell, "pod-b", weight_b - err, weight_b, -err),
    ]


def write_weight_share_csv(path: pathlib.Path, spec: list[tuple]) -> pathlib.Path:
    """Write a weight-share-summary.csv with the pinned 5-column schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(WEIGHT_SHARE_COLUMNS)]
    for cell, a_mc, b_mc, err in spec:
        for row in _pod_rows(cell, a_mc, b_mc, err):
            lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_fixtures(
    root: pathlib.Path,
    omit_2cpu: set[str] | None = None,
    omit_4cpu: set[str] | None = None,
) -> dict[str, pathlib.Path]:
    """Write the 2-CPU / 4-CPU / scaled-4v fixture CSVs and return their paths."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "2cpu": root / "weight-share-2cpu-summary.csv",
        "4cpu": root / "weight-share-4cpu-summary.csv",
        "4v": root / "weight-share-4v-scaled-summary.csv",
    }
    spec2 = [c for c in CELLS_2CPU if c[0] not in (omit_2cpu or set())]
    spec4 = [c for c in CELLS_4CPU if c[0] not in (omit_4cpu or set())]
    write_weight_share_csv(files["2cpu"], spec2)
    write_weight_share_csv(files["4cpu"], spec4)
    write_weight_share_csv(files["4v"], CELLS_4V)
    return files


@pytest.fixture
def fixtures(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Happy-path fixture pair: full 2-CPU, 4-CPU and scaled-4v summaries."""
    return write_fixtures(tmp_path / "fixtures")


@pytest.fixture
def missing_cell_fixtures(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    """Fixture pair where one cell exists in each run only (REQ-2)."""
    return write_fixtures(
        tmp_path / "fixtures-missing",
        omit_2cpu={"a=100m;b=1000m"},  # present only in the 4-CPU file
        omit_4cpu={"a=800m;b=800m"},  # present only in the 2-CPU file
    )


def run_script(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run cpu-count-compare.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
        env=env or AGG_ENV,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    files: dict[str, pathlib.Path],
    tmp_path: pathlib.Path,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
    include_scaled: bool = True,
) -> tuple[int, str, str, pathlib.Path]:
    """Run the script against fixture CSVs and return (rc, out, err, out_dir)."""
    out_dir = tmp_path / "output"
    argv = [
        "--csv-2cpu",
        str(files["2cpu"]),
        "--csv-4cpu",
        str(files["4cpu"]),
        "--output-dir",
        str(out_dir),
    ]
    if include_scaled:
        argv += ["--csv-4v-scaled", str(files["4v"])]
    rc, out, err = run_script(argv + (extra or []), env=env)
    return rc, out, err, out_dir


def expected_comparison_rows() -> list[tuple]:
    """Expected per-cell rows for the happy-path fixtures (REQ-1 math)."""
    rows: list[tuple] = []
    for (cell, _a, _b, e2), (_c, _d, _e, e4) in zip(
        CELLS_2CPU, CELLS_4CPU, strict=True
    ):
        rows.append((cell, f"{_a}/{_b}", e2, e4, e4 - e2, "both"))
    return sorted(rows, key=lambda r: r[0])


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_api(self):
        """cpu-count-compare.py exposes the pinned output names + functions."""
        module = load_module()
        assert module.OUTPUT_COMPARISON_CSV == OUTPUT_COMPARISON_CSV
        assert module.OUTPUT_DETAIL_CSV == OUTPUT_DETAIL_CSV
        assert module.OUTPUT_SCALED_CSV == OUTPUT_SCALED_CSV
        assert module.OUTPUT_VERDICT_CSV == OUTPUT_VERDICT_CSV
        assert module.OUTPUT_PNG == OUTPUT_PNG
        assert module.VERDICT_FORMAT == "mean |ratio_error| {:.3f} -> {:.3f}"
        assert module.SCALED_VERDICT_FORMAT == "scaled-4v mean |ratio_error| {:.3f}"
        for name in (
            "load_summary_csv",
            "ratio_label",
            "build_comparison",
            "build_detail",
            "build_scaled_block",
            "verdict_line",
            "scaled_verdict_line",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# ratio_label / load_summary_csv
# =========================================================================


class TestRatioLabel:
    """Short human-readable ratio label derived from a cell."""

    def test_extracts_first_two_millicore_requests(self):
        """'a=500m;b=500m' -> '500/500'; scaled cells keep their ratio."""
        module = load_module()
        assert module.ratio_label("a=500m;b=500m") == "500/500"
        assert module.ratio_label("a=250m;b=1000m") == "250/1000"
        assert module.ratio_label("a=1500m;b=1500m") == "1500/1500"
        assert module.ratio_label("a=750m;b=1500m") == "750/1500"

    def test_less_than_two_requests_falls_back_to_cell(self):
        """Cells without two request tokens fall back to the cell string."""
        module = load_module()
        assert module.ratio_label("single-pod-cell") == "single-pod-cell"
        assert module.ratio_label("") == ""


class TestLoadSummaryCsv:
    """Reading a weight-share-summary.csv input file."""

    def test_reads_rows_with_pinned_schema(self, fixtures: dict[str, pathlib.Path]):
        """2-CPU fixture: 6 cells x 2 pods = 12 rows with the 5-column schema."""
        module = load_module()
        df = module.load_summary_csv(fixtures["2cpu"])
        assert list(df.columns) == WEIGHT_SHARE_COLUMNS
        assert len(df) == 12
        assert df["cell"].nunique() == 6

    def test_missing_path_raises_with_clear_message(self, tmp_path: pathlib.Path):
        """A nonexistent input path raises FileNotFoundError naming the path."""
        module = load_module()
        missing = tmp_path / "does-not-exist.csv"
        with pytest.raises(FileNotFoundError) as excinfo:
            module.load_summary_csv(missing)
        assert "does-not-exist.csv" in str(excinfo.value)


# =========================================================================
# VC-CC-01/02 — build_comparison (REQ-1 exact math, REQ-2 missing cells)
# =========================================================================


class TestBuildComparison:
    """Per-cell mean |ratio_error|, delta and missing-cell marking."""

    def test_exact_per_cell_means_and_delta(self, fixtures: dict[str, pathlib.Path]):
        """REQ-1: per-cell error_2cpu/error_4cpu/delta match the fixture math."""
        module = load_module()
        df2 = module.load_summary_csv(fixtures["2cpu"])
        df4 = module.load_summary_csv(fixtures["4cpu"])
        table = module.build_comparison(df2, df4).set_index("cell")
        for cell, ratio, e2, e4, _d, _m in expected_comparison_rows():
            row = table.loc[cell]
            assert row["ratio_label"] == ratio
            assert row["error_2cpu"] == pytest.approx(e2, abs=1e-9)
            assert row["error_4cpu"] == pytest.approx(e4, abs=1e-9)
            assert row["delta"] == pytest.approx(e4 - e2, abs=1e-9)
            assert row["missing_in"] == "both"

    def test_columns_and_sorted_rows(self, fixtures: dict[str, pathlib.Path]):
        """REQ-2: pinned column order, one row per cell, sorted by cell."""
        module = load_module()
        table = module.build_comparison(
            module.load_summary_csv(fixtures["2cpu"]),
            module.load_summary_csv(fixtures["4cpu"]),
        )
        assert list(table.columns) == COMPARISON_COLUMNS
        assert len(table) == 6
        cells = table["cell"].tolist()
        assert cells == sorted(cells)
        assert all(m == "both" for m in table["missing_in"])

    def test_overall_means_match_pinned_verdict(
        self, fixtures: dict[str, pathlib.Path]
    ):
        """REQ-1/REQ-3: mean |ratio_error| is 0.024 (2-CPU) and 0.010 (4-CPU)."""
        module = load_module()
        table = module.build_comparison(
            module.load_summary_csv(fixtures["2cpu"]),
            module.load_summary_csv(fixtures["4cpu"]),
        )
        assert table["error_2cpu"].mean() == pytest.approx(VERDICT_2CPU_MEAN, abs=1e-9)
        assert table["error_4cpu"].mean() == pytest.approx(VERDICT_4CPU_MEAN, abs=1e-9)

    def test_cells_present_in_only_one_run_marked_missing(
        self, missing_cell_fixtures: dict[str, pathlib.Path]
    ):
        """REQ-2: one-sided cells are marked; no crash; matched cells intact."""
        module = load_module()
        table = module.build_comparison(
            module.load_summary_csv(missing_cell_fixtures["2cpu"]),
            module.load_summary_csv(missing_cell_fixtures["4cpu"]),
        ).set_index("cell")
        assert len(table) == 6  # every cell still appears
        # 800/800 exists only in the 2-CPU file -> missing from 4-CPU side.
        row = table.loc["a=800m;b=800m"]
        assert row["missing_in"] == "2cpu"
        assert row["error_2cpu"] == pytest.approx(0.021, abs=1e-9)
        assert pd.isna(row["error_4cpu"])
        assert pd.isna(row["delta"])
        # 100/1000 exists only in the 4-CPU file -> missing from 2-CPU side.
        row = table.loc["a=100m;b=1000m"]
        assert row["missing_in"] == "4cpu"
        assert pd.isna(row["error_2cpu"])
        assert row["error_4cpu"] == pytest.approx(0.011, abs=1e-9)
        assert pd.isna(row["delta"])
        # Matched cells keep exact deltas.
        row = table.loc["a=500m;b=500m"]
        assert row["missing_in"] == "both"
        assert row["delta"] == pytest.approx(0.012 - 0.030, abs=1e-9)


# =========================================================================
# VC-CC-01 — build_detail (per-pod rows)
# =========================================================================


class TestBuildDetail:
    """Per-pod signed ratio_error detail rows."""

    def test_exact_per_pod_rows(self, fixtures: dict[str, pathlib.Path]):
        """REQ-1: pod-a +e / pod-b -e per run, delta between runs."""
        module = load_module()
        detail = module.build_detail(
            module.load_summary_csv(fixtures["2cpu"]),
            module.load_summary_csv(fixtures["4cpu"]),
        )
        assert list(detail.columns) == DETAIL_COLUMNS
        assert len(detail) == 12  # 6 cells x 2 pods
        rows = detail[detail["cell"] == "a=500m;b=500m"].set_index("pod")
        assert rows.loc["pod-a", "ratio_error_2cpu"] == pytest.approx(0.030, abs=1e-9)
        assert rows.loc["pod-a", "ratio_error_4cpu"] == pytest.approx(0.012, abs=1e-9)
        assert rows.loc["pod-a", "delta"] == pytest.approx(-0.018, abs=1e-9)
        assert rows.loc["pod-b", "ratio_error_2cpu"] == pytest.approx(-0.030, abs=1e-9)
        assert rows.loc["pod-b", "ratio_error_4cpu"] == pytest.approx(-0.012, abs=1e-9)
        assert rows.loc["pod-b", "delta"] == pytest.approx(0.018, abs=1e-9)

    def test_missing_cell_pods_get_nan_side(
        self, missing_cell_fixtures: dict[str, pathlib.Path]
    ):
        """REQ-2: pods of a one-sided cell have NaN on the missing side."""
        module = load_module()
        detail = module.build_detail(
            module.load_summary_csv(missing_cell_fixtures["2cpu"]),
            module.load_summary_csv(missing_cell_fixtures["4cpu"]),
        )
        rows = detail[detail["cell"] == "a=800m;b=800m"].set_index("pod")
        assert len(rows) == 2
        # 800/800 exists only in the 2-CPU file: the present side keeps its
        # real per-pod ratio_error, the 4-CPU side and delta are NaN.
        assert rows.loc["pod-a", "ratio_error_2cpu"] == pytest.approx(0.021, abs=1e-9)
        assert pd.isna(rows.loc["pod-a", "ratio_error_4cpu"])
        assert pd.isna(rows.loc["pod-a", "delta"])


# =========================================================================
# VC-CC-04 — build_scaled_block (REQ-4)
# =========================================================================


class TestBuildScaledBlock:
    """Scaled 4-vCPU block section from the weight-share-4v summary."""

    def test_exact_scaled_rows(self, fixtures: dict[str, pathlib.Path]):
        """REQ-4: per-cell error_scaled matches the scaled fixture math."""
        module = load_module()
        block = module.build_scaled_block(module.load_summary_csv(fixtures["4v"]))
        assert list(block.columns) == SCALED_COLUMNS
        assert len(block) == 6
        rows = block.set_index("cell")
        for cell, a_mc, _b_mc, err in CELLS_4V:
            assert rows.loc[cell, "ratio_label"] == f"{a_mc}/{_b_mc}"
            assert rows.loc[cell, "error_scaled"] == pytest.approx(err, abs=1e-9)

    def test_rows_sorted_by_cell(self, fixtures: dict[str, pathlib.Path]):
        """REQ-4: scaled rows are deterministically sorted by cell."""
        module = load_module()
        block = module.build_scaled_block(module.load_summary_csv(fixtures["4v"]))
        assert block["cell"].tolist() == sorted(block["cell"].tolist())


# =========================================================================
# VC-CC-03 — verdict lines (REQ-3)
# =========================================================================


class TestVerdict:
    """Deterministic verdict lines in the pinned format."""

    def test_verdict_line_format(self, fixtures: dict[str, pathlib.Path]):
        """REQ-3: 'mean |ratio_error| 0.024 -> 0.010' on the fixtures."""
        module = load_module()
        table = module.build_comparison(
            module.load_summary_csv(fixtures["2cpu"]),
            module.load_summary_csv(fixtures["4cpu"]),
        )
        assert module.verdict_line(table) == VERDICT_LINE

    def test_scaled_verdict_line_format(self, fixtures: dict[str, pathlib.Path]):
        """REQ-4: 'scaled-4v mean |ratio_error| 0.008' on the fixtures."""
        module = load_module()
        block = module.build_scaled_block(module.load_summary_csv(fixtures["4v"]))
        assert module.scaled_verdict_line(block) == SCALED_VERDICT_LINE


# =========================================================================
# VC-CLI-01 — CLI contract (REQ-6)
# =========================================================================


class TestCli:
    """--csv-2cpu/--csv-4cpu/--csv-4v-scaled/--output-dir contract."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_script(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--csv-2cpu" in combined
        assert "--csv-4cpu" in combined
        assert "--csv-4v-scaled" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_script([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_input_file_exits_nonzero_with_message(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """REQ-6: a nonexistent --csv-4cpu path -> non-zero + clear message.

        "missing-4cpu" is the nonexistent filename: the real script must name
        the path it could not find. The Python "can't open file" fallback for
        the missing script does not contain it, so this stays red in the red
        phase.
        """
        out_dir = tmp_path / "output"
        rc, _out, err = run_script(
            [
                "--csv-2cpu",
                str(fixtures["2cpu"]),
                "--csv-4cpu",
                str(tmp_path / "missing-4cpu.csv"),
                "--output-dir",
                str(out_dir),
            ]
        )
        assert rc != 0
        assert "missing-4cpu.csv" in err


# =========================================================================
# End-to-end: fixture data -> outputs (REQ-1..4, REQ-6)
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract outputs."""

    def test_happy_path_all_outputs(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """VC-CC-01/02/03/04 + VC-CLI-01: exit 0, exact CSVs + verdict lines."""
        rc, out, err, out_dir = run_ok(fixtures, tmp_path)
        assert rc == 0, f"stderr: {err}"

        comparison = pd.read_csv(out_dir / OUTPUT_COMPARISON_CSV)
        assert list(comparison.columns) == COMPARISON_COLUMNS
        assert len(comparison) == 6
        assert comparison["missing_in"].eq("both").all()
        assert comparison["error_2cpu"].mean() == pytest.approx(
            VERDICT_2CPU_MEAN, abs=1e-9
        )
        assert comparison["error_4cpu"].mean() == pytest.approx(
            VERDICT_4CPU_MEAN, abs=1e-9
        )

        detail = pd.read_csv(out_dir / OUTPUT_DETAIL_CSV)
        assert list(detail.columns) == DETAIL_COLUMNS
        assert len(detail) == 12

        scaled = pd.read_csv(out_dir / OUTPUT_SCALED_CSV)
        assert list(scaled.columns) == SCALED_COLUMNS
        assert scaled["error_scaled"].mean() == pytest.approx(
            VERDICT_SCALED_MEAN, abs=1e-9
        )

        verdict_lines = (out_dir / OUTPUT_VERDICT_CSV).read_text().splitlines()
        assert verdict_lines == [VERDICT_LINE, SCALED_VERDICT_LINE]
        # The verdict line is also printed to stdout.
        assert VERDICT_LINE in out
        assert SCALED_VERDICT_LINE in out

    def test_scaled_omitted_warns_and_skips_block(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """--csv-4v-scaled is optional: skip the block with a warning, exit 0."""
        rc, out, err, out_dir = run_ok(fixtures, tmp_path, include_scaled=False)
        assert rc == 0, f"stderr: {err}"
        assert not (out_dir / OUTPUT_SCALED_CSV).exists()
        assert (out_dir / OUTPUT_COMPARISON_CSV).exists()
        verdict_lines = (out_dir / OUTPUT_VERDICT_CSV).read_text().splitlines()
        assert verdict_lines == [VERDICT_LINE]
        assert "scaled" in err.lower() or "warn" in err.lower()

    def test_empty_input_outputs_header_only(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """REQ-5: header-only 2-CPU input -> header-only outputs + warning.

        Pinned to exit 0 with a stderr warning, matching the repo convention
        for empty inputs (see TEST-DESIGN.md decision D-2).
        """
        (fixtures["2cpu"]).write_text(",".join(WEIGHT_SHARE_COLUMNS) + "\n")
        rc, _out, err, out_dir = run_ok(fixtures, tmp_path)
        assert rc == 0, f"stderr: {err}"
        comparison_lines = (out_dir / OUTPUT_COMPARISON_CSV).read_text().splitlines()
        assert comparison_lines == [",".join(COMPARISON_COLUMNS)]
        assert err, "empty input must produce a message on stderr"

    def test_two_runs_byte_identical(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """Same input -> byte-identical comparison CSV and verdict file."""
        rc1, _o1, e1, out1 = run_ok(fixtures, tmp_path / "run1")
        rc2, _o2, e2, out2 = run_ok(fixtures, tmp_path / "run2")
        assert rc1 == 0 and rc2 == 0, f"stderr: {e1} / {e2}"
        for name in (OUTPUT_COMPARISON_CSV, OUTPUT_DETAIL_CSV, OUTPUT_VERDICT_CSV):
            assert (out1 / name).read_bytes() == (out2 / name).read_bytes()


# =========================================================================
# VC-MPL-01 — matplotlib lazy, headless and non-fatal (REQ-5)
# =========================================================================


class TestMatplotlib:
    """Plotting is lazy, uses MPLBACKEND=Agg and never blocks the CSVs."""

    @pytest.mark.skipif(
        not importlib.util.find_spec("matplotlib"),
        reason="matplotlib not installed",
    )
    def test_png_rendered(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """A valid PNG is emitted next to the CSVs."""
        rc, _out, err, out_dir = run_ok(fixtures, tmp_path)
        assert rc == 0, f"stderr: {err}"
        png_path = out_dir / OUTPUT_PNG
        assert png_path.exists(), f"missing plot: {png_path}"
        assert png_path.read_bytes()[:4] == b"\x89PNG"

    def test_matplotlib_import_failure_is_nonfatal(
        self, fixtures: dict[str, pathlib.Path], tmp_path: pathlib.Path
    ):
        """REQ-5: broken matplotlib must not block the CSV + verdict outputs."""
        stub_dir = tmp_path / "stub-matplotlib"
        stub_dir.mkdir()
        (stub_dir / "matplotlib.py").write_text(
            'raise ImportError("stubbed out for tests")\n'
        )
        env = {**os.environ, "PYTHONPATH": str(stub_dir), "MPLBACKEND": "Agg"}
        rc, _out, err, out_dir = run_ok(fixtures, tmp_path, env=env)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / OUTPUT_COMPARISON_CSV).exists(), (
            "CSV must be written even without matplotlib"
        )
        assert (out_dir / OUTPUT_VERDICT_CSV).exists()
        assert "matplotlib" in err.lower() or "warn" in err.lower()
