"""Tests for dist-plot.py — static distribution images.

Test-first design, red until the engineer implements the script.
The module/function/CLI names used here are the contract the implementation must
build:

    research/analysis/dist-plot.py  (module: dist_plot)

    Constants:
      CELL_HISTOGRAM   "slice-histogram.png"
      FAMILY_IMAGES    ("slice-dist-comparison.png", "slice-ecdf-overlay.png",
                        "gantt-timeline.png", "runtime-trajectory.png")

    Pure core (testable with pinned dist-analyze output CSVs, no rendering):
      load_family_data(analysis_root, family, cells) -> dict[str, dict]
          # {cell: {"cell": str,            # the cell label itself (render_gantt
          #                                 # receives only the cell dict)
          #         "summary": dict,        # FIRST dist-summary row
          #         "quality": str,         # "degraded" iff ANY summary row is
          #                                 # degraded (exclusion rule)
          #         "durations_us": list[float],
          #         "slices": list[dict],   # dist-slices.csv rows (ts_start_us,
          #                                 # ts_end_us, duration_us, cpu, tid,
          #                                 # thread_name, pod) — the Gantt
          #                                 # source for CPU lanes + pods
          #         "runtime_rows": list[dict],
          #         "percentiles": dict}}
      good_cells(data) -> list[str]
      representative_cell(data) -> str      # good cell with the highest
                                            # throttle_ratio; ties -> first in
                                            # cell order; no good cell ->
                                            # ValueError
      compute_ecdf(durations_us) -> (list[float], list[float])
      cumulative_runtime_series(runtime_rows) -> pd.DataFrame
      histogram_annotations(cell_data) -> list[str]
      gantt_annotations(cell_data) -> list[str]

    Render (each writes the PNG AND returns the matplotlib Figure):
      render_cell_histogram(cell_data, out_path) -> Figure
      render_slice_comparison(cells_data, out_path) -> Figure
      render_ecdf_overlay(cells_data, out_path) -> Figure
      render_gantt(cell_data, out_path) -> Figure
      render_runtime_trajectory(cells_data, out_path) -> Figure
      render_all(cells_data, output_root) -> dict[str, Figure]

    main(argv: list[str] | None = None) -> int

CLI:
    dist-plot.py --data-dir <analysis root> --output-dir <out root>
                 --family <name> --cells <c1,c2,...>
Reads <data-dir>/distribution/<family>/<cell>/{dist-slices,dist-runtime,
dist-summary}.csv + dist-percentiles.json (the dist-analyze OUTPUT contract).
Writes <out root>/distribution/<family>/<cell>/slice-histogram.png per cell
and the four FAMILY_IMAGES under <out root>/distribution/<family>/.
Exits non-zero when a listed cell's dist-analyze output is missing or its
dist-slices.csv has zero rows, or the family has no good cells (loud failure
naming the cause).

Annotation contract (exact substrings the tests assert):
  histogram_annotations  "mean {v:g} us", "median {v:g} us",
                         "p95 {v:g} us", "p99 {v:g} us" from the summary row
  gantt_annotations      the cell label; "CPU <n>" per cpu lane;
                         "pod <name>" per pod present; when throttle_ratio>0:
                         "throttle gaps hatched" and "throttle_ratio {r:g}"
  family images          every good cell's label rendered as a legend/text
                         entry; a degraded cell's label must NEVER appear

Covered behavior:
  per-cell slice-histogram.png with log-x axis + percentile overlays
  family slice-dist-comparison / slice-ecdf-overlay /
          gantt-timeline / runtime-trajectory images
  every image non-empty, openable via Pillow (no cluster access)
  degraded cells (quality=degraded in dist-summary.csv) excluded
          from the four family images but still get their per-cell histogram
  identical SHA-256 across two runs
  output layout output/distribution/<family>/<cell>/ + <family>/

Run from research/analysis:
    python3 -m pytest tests/test_dist_plot.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import matplotlib

matplotlib.use("Agg")  # headless rendering for the in-process figure tests

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from tests.conftest import (  # noqa: E402
    DIST_PERCENTILE_STEPS,
    DIST_RUNTIME_COLUMNS,
    DIST_SLICES_COLUMNS,
    DIST_STEPS_ALT,
    DIST_STEPS_CELL_SPECS,
    DIST_STEPS_CELLS,
    DIST_STEPS_D10,
    DIST_STEPS_FILES,  # noqa: F401  (sanity: dist-plot shares the family specs)
    DIST_SUMMARY_COLUMNS,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_PLOT_SCRIPT = ANALYSIS_DIR / "dist-plot.py"

FAMILY = "dist-stress-ng"
CELL_HISTOGRAM = "slice-histogram.png"
FAMILY_IMAGES = (
    "slice-dist-comparison.png",
    "slice-ecdf-overlay.png",
    "gantt-timeline.png",
    "runtime-trajectory.png",
)
REP_CELL = DIST_STEPS_CELLS[1]  # request=100m-limit=100m, throttle_ratio 0.99
DEGRADED_IN_VARIANT = DIST_STEPS_CELLS[1]
REP_CELL_DEGRADED_FALLBACK = DIST_STEPS_CELLS[2]  # request=100m-limit=1000m, 0.97

# Hand-computed stats for the fixture D10 durations [10..100] us (pandas
# linear interpolation, same constants as dist-analyze):
D10_MEAN = 55.0
D10_MEDIAN = 55.0
D10_P95 = 95.5
D10_P99 = 99.1
# Alt durations [100..500] us -> pandas linear interpolation quantiles:
ALT_MEAN = 300.0
ALT_MEDIAN = 300.0
ALT_P95 = 480.0
ALT_P99 = 496.0

# Pinned fixture geometry for dist-runtime.csv: samples at 1s-spaced starts
# starting 2.5s, runtime_ns = (i+1) * 1.4ms -> cumulative 1.4ms..14ms over the
# 10 D10 samples.
RUNTIME_STEP_NS = 1_400_000


# =========================================================================
# Helpers
# =========================================================================


def load_dist_plot_module():
    """Import the not-yet-existing script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("dist_plot", DIST_PLOT_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_PLOT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dist_plot(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run dist-plot.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(DIST_PLOT_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def agg_env() -> dict[str, str]:
    """Environment for subprocess renders: deterministic Agg backend."""
    env = dict(os.environ)
    env["MPLBACKEND"] = "Agg"
    return env


def stats_for(durations: list[float]) -> dict[str, float]:
    """Compute the dist-summary stats for a duration list (pandas method)."""
    series = pd.Series(list(durations), dtype="float64")
    return {
        "mean_us": float(series.mean()),
        "median_us": float(series.median()),
        "p50_us": float(series.quantile(0.50)),
        "p95_us": float(series.quantile(0.95)),
        "p99_us": float(series.quantile(0.99)),
        "max_us": float(series.max()),
    }


def figure_texts(fig) -> list[str]:
    """Collect every human-visible text object from a matplotlib Figure.

    Gathers fig.text annotations, the suptitle, axis titles, axis-level text,
    axis tick labels and legend entries — this is the module-exposed label
    mechanism the tests use instead of OCR.
    """
    texts = [t.get_text() for t in fig.texts]
    if fig._suptitle is not None:
        texts.append(fig._suptitle.get_text())
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.extend(t.get_text() for t in ax.texts)
        texts.extend(label.get_text() for label in ax.get_xticklabels())
        texts.extend(label.get_text() for label in ax.get_yticklabels())
        legend = ax.get_legend()
        if legend is not None:
            texts.extend(t.get_text() for t in legend.get_texts())
    return [t for t in texts if t]


def assert_openable_png(path: pathlib.Path):
    """Assert *path* is a non-empty PNG Pillow can open."""
    assert path.exists(), f"missing image: {path}"
    assert path.stat().st_size > 0, f"empty image: {path}"
    with Image.open(path) as img:
        img.verify()  # raises on truncated/corrupt image data
    assert Image.open(path).format == "PNG"


def sha256_of(path: pathlib.Path) -> str:
    """SHA-256 hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root*."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return manifest


def load_family_data(module, data_dir: pathlib.Path) -> dict:
    """Load the six-cell fixture data through the module's own reader."""
    return module.load_family_data(data_dir, FAMILY, list(DIST_STEPS_CELLS))


# =========================================================================
# Fixture writers — dist-analyze OUTPUT trees (no traces, no cluster)
# =========================================================================


def write_cell_outputs(
    cell_dir: pathlib.Path,
    *,
    cell: str,
    durations: list[float],
    cpu_weight: int,
    cpu_max: int,
    throttle_ratio: float,
    qualities: tuple[str, ...] = ("good",),
    extra_slices: tuple[tuple[int, int, int, int, int, str, str], ...] = (),
) -> pathlib.Path:
    """Write one cell's dist-analyze OUTPUT files, including dist-runtime.csv.

    dist-slices.csv holds one row per slice (1s-spaced starts from 2.5s);
    dist-runtime.csv holds one sched_stat_runtime sample per slice with a
    constant 1.4ms delta (the kernel emits per-switch runtime deltas, so the
    cumulative trajectory reaches 1.4ms..14ms for the 10 D10 samples).
    dist-summary.csv carries one row per ``qualities`` entry (replicate axis)
    so the any-degraded-row rule is testable. dist-percentiles.json
    carries the pinned decile table for the pod.
    """
    cell_dir.mkdir(parents=True, exist_ok=True)
    stats = stats_for(durations)
    n = len(durations)

    slice_rows: list[tuple] = []
    for i, dur in enumerate(durations):
        start = 2_500_000 + i * 1_000_000
        slice_rows.append(
            (start, start + int(dur), float(dur), 0, 1001, "stress-ng-cpu", "stress-ng")
        )
    slice_rows.extend(extra_slices)
    pd.DataFrame(slice_rows, columns=DIST_SLICES_COLUMNS).to_csv(
        cell_dir / "dist-slices.csv", index=False
    )

    runtime_rows: list[tuple] = []
    for i, dur in enumerate(durations):
        ts = 2_500_000 + i * 1_000_000
        runtime_rows.append(
            (ts, 0, 1001, 1001, "stress-ng-cpu", "stress-ng", RUNTIME_STEP_NS)
        )
    pd.DataFrame(runtime_rows, columns=DIST_RUNTIME_COLUMNS).to_csv(
        cell_dir / "dist-runtime.csv", index=False
    )

    summary_rows: list[list] = []
    for rep, quality in enumerate(qualities, start=1):
        summary_rows.append(
            [
                cell,
                rep,
                "stress-ng",
                n,
                float(sum(durations)) / 1000.0,
                stats["mean_us"],
                stats["median_us"],
                stats["p50_us"],
                stats["p95_us"],
                stats["p99_us"],
                stats["max_us"],
                throttle_ratio,
                cpu_weight,
                cpu_max,
                quality,
            ]
        )
    pd.DataFrame(summary_rows, columns=DIST_SUMMARY_COLUMNS).to_csv(
        cell_dir / "dist-summary.csv", index=False
    )

    series = pd.Series(list(durations), dtype="float64")
    table = {f"p{k}": float(series.quantile(k / 100.0)) for k in DIST_PERCENTILE_STEPS}
    (cell_dir / "dist-percentiles.json").write_text(
        json.dumps({"1": {"stress-ng": table}}, indent=2, sort_keys=True) + "\n"
    )
    return cell_dir


def build_family(
    root: pathlib.Path,
    *,
    degraded_cells: tuple[str, ...] = (),
    all_degraded: bool = False,
) -> pathlib.Path:
    """Write the six-cell Family A stress-ng dist-analyze OUTPUT tree.

    Cell labels/weights/quotas/ratios mirror the pinned specs in the
    standard cell order.  ``degraded_cells`` marks specific cells
    quality=degraded; ``all_degraded`` marks every cell degraded.
    """
    for cell, weight, quota, periods, throttled in DIST_STEPS_CELL_SPECS:
        quality = "degraded" if (all_degraded or cell in degraded_cells) else "good"
        write_cell_outputs(
            root / "distribution" / FAMILY / cell,
            cell=cell,
            durations=DIST_STEPS_D10,
            cpu_weight=weight,
            cpu_max=quota,
            throttle_ratio=throttled / periods,
            qualities=(quality,),
        )
    return root


def build_missing_cell_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family fixture missing the 500m/500m cell (DIST_STEPS_CELLS[3]).

    The renderer must fail loudly naming the missing cell instead of silently
    producing partial family images.
    """
    for idx, (cell, weight, quota, periods, throttled) in enumerate(
        DIST_STEPS_CELL_SPECS
    ):
        if idx == 3:
            continue
        write_cell_outputs(
            root / "distribution" / FAMILY / cell,
            cell=cell,
            durations=DIST_STEPS_D10,
            cpu_weight=weight,
            cpu_max=quota,
            throttle_ratio=throttled / periods,
        )
    return root


def build_empty_slices_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family fixture where the no-limit cell's dist-slices.csv has no rows."""
    build_family(root)
    no_limit = root / "distribution" / FAMILY / DIST_STEPS_CELLS[0]
    pd.DataFrame(columns=DIST_SLICES_COLUMNS).to_csv(
        no_limit / "dist-slices.csv", index=False
    )
    return root


def build_gantt_cell_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Single-cell fixture for render_gantt: 2 CPU lanes + a system slice.

    The representative cell (request=100m-limit=100m, ratio 0.99) has five
    stress-ng slices on cpu 0, three on cpu 1, and one system slice on cpu 1
    — the per-CPU/pod/hatch contract of the Gantt image.
    """
    cell = REP_CELL
    write_cell_outputs(
        root / "distribution" / FAMILY / cell,
        cell=cell,
        durations=DIST_STEPS_D10,
        cpu_weight=17,
        cpu_max=10000,
        throttle_ratio=0.99,
        extra_slices=(
            # (ts_start_us, ts_end_us, duration_us, cpu, tid, thread_name, pod)
            (6_500_000, 6_500_100, 100, 1, 1001, "stress-ng-cpu", "stress-ng"),
            (7_000_000, 7_000_200, 200, 1, 1001, "stress-ng-cpu", "stress-ng"),
            (7_500_000, 7_500_300, 300, 1, 1001, "stress-ng-cpu", "stress-ng"),
            (8_000_000, 8_000_250, 250, 1, 999, "stress-ng-cpu", "system"),
        ),
    )
    return root


@pytest.fixture
def family_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Six-cell good-family fixture."""
    return build_family(tmp_path / "dist-plot-family")


@pytest.fixture
def degraded_family_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Six-cell fixture with REP_CELL degraded (exclusion case)."""
    return build_family(tmp_path / "dist-plot-degraded", degraded_cells=(REP_CELL,))


@pytest.fixture
def all_degraded_family_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Six-cell fixture where every cell is degraded (no representative)."""
    return build_family(tmp_path / "dist-plot-all-degraded", all_degraded=True)


@pytest.fixture
def missing_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family fixture with the 500m/500m cell missing."""
    return build_missing_cell_data_dir(tmp_path / "dist-plot-missing")


@pytest.fixture
def empty_slices_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Family fixture where the no-limit cell has zero slice rows."""
    return build_empty_slices_data_dir(tmp_path / "dist-plot-empty")


@pytest.fixture
def gantt_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Single representative cell with two CPU lanes + a system slice."""
    return build_gantt_cell_data_dir(tmp_path / "dist-plot-gantt")


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_api(self):
        module = load_dist_plot_module()
        for name in (
            "CELL_HISTOGRAM",
            "FAMILY_IMAGES",
            "load_family_data",
            "good_cells",
            "representative_cell",
            "compute_ecdf",
            "cumulative_runtime_series",
            "histogram_annotations",
            "gantt_annotations",
            "render_cell_histogram",
            "render_slice_comparison",
            "render_ecdf_overlay",
            "render_gantt",
            "render_runtime_trajectory",
            "render_all",
            "main",
        ):
            assert hasattr(module, name), f"missing pinned name: {name}"
        for func in (
            "load_family_data",
            "good_cells",
            "representative_cell",
            "compute_ecdf",
            "cumulative_runtime_series",
            "histogram_annotations",
            "gantt_annotations",
            "render_cell_histogram",
            "render_slice_comparison",
            "render_ecdf_overlay",
            "render_gantt",
            "render_runtime_trajectory",
            "render_all",
            "main",
        ):
            assert callable(getattr(module, func)), f"not callable: {func}"

    def test_cell_histogram_name_pinned(self):
        module = load_dist_plot_module()
        assert module.CELL_HISTOGRAM == "slice-histogram.png"

    def test_family_images_are_the_four_pinned_names(self):
        module = load_dist_plot_module()
        assert tuple(module.FAMILY_IMAGES) == FAMILY_IMAGES
        assert len(module.FAMILY_IMAGES) == 4


# =========================================================================
# load_family_data — dist-analyze OUTPUT ingestion
# =========================================================================


class TestLoadFamilyData:
    """The reader must consume the pinned dist-analyze output files."""

    def test_loads_all_six_cells_with_summary_values(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        assert set(data.keys()) == set(DIST_STEPS_CELLS)
        quota = data["request=500m-limit=500m"]["summary"]
        assert quota["cpu_weight"] == 59
        assert quota["cpu_max"] == 50000
        assert quota["throttle_ratio"] == pytest.approx(0.96, abs=1e-9)
        assert quota["p95_us"] == pytest.approx(D10_P95, abs=1e-9)

    def test_durations_loaded(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        assert data["request=-limit="]["durations_us"] == [
            float(v) for v in DIST_STEPS_D10
        ]

    def test_runtime_rows_loaded(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        rows = data["request=-limit="]["runtime_rows"]
        assert len(rows) == len(DIST_STEPS_D10)
        assert rows[0]["pod"] == "stress-ng"
        assert rows[0]["runtime_ns"] == RUNTIME_STEP_NS
        assert rows[-1]["runtime_ns"] == RUNTIME_STEP_NS

    def test_slices_rows_loaded_with_cpu_and_pod(self, family_data_dir):
        """render_gantt needs per-slice cpu + pod (colored lanes by pod)."""
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        rows = data["request=-limit="]["slices"]
        assert len(rows) == len(DIST_STEPS_D10)
        assert set(rows[0].keys()) >= {
            "ts_start_us",
            "ts_end_us",
            "duration_us",
            "cpu",
            "tid",
            "thread_name",
            "pod",
        }
        assert rows[0]["cpu"] == 0
        assert rows[0]["pod"] == "stress-ng"
        assert rows[0]["duration_us"] == pytest.approx(DIST_STEPS_D10[0], abs=1e-9)

    def test_cell_label_exposed_in_cell_dict(self, family_data_dir):
        """render_gantt receives only the cell dict, so it must carry its label."""
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        for cell in DIST_STEPS_CELLS:
            assert data[cell]["cell"] == cell

    def test_percentiles_loaded(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        table = data["request=-limit="]["percentiles"]
        assert table["p99"] == pytest.approx(D10_P99, abs=1e-9)
        assert table["p51"] == pytest.approx(55.9, abs=1e-9)

    def test_quality_good_for_good_cells(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        for cell in DIST_STEPS_CELLS:
            assert data[cell]["quality"] == "good"

    def test_quality_degraded_detected(self, degraded_family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        assert data[REP_CELL]["quality"] == "degraded"
        for cell in DIST_STEPS_CELLS:
            if cell != REP_CELL:
                assert data[cell]["quality"] == "good"

    def test_any_degraded_row_marks_cell_degraded(self, tmp_path):
        """A cell is degraded if ANY summary row is degraded."""
        cell = DIST_STEPS_CELLS[3]
        cell_dir = tmp_path / "distribution" / FAMILY / cell
        write_cell_outputs(
            cell_dir,
            cell=cell,
            durations=DIST_STEPS_D10,
            cpu_weight=59,
            cpu_max=50000,
            throttle_ratio=0.96,
            qualities=("good", "degraded"),
        )
        module = load_dist_plot_module()
        data = module.load_family_data(tmp_path, FAMILY, [cell])
        assert data[cell]["quality"] == "degraded"

    def test_missing_cell_raises_naming_cell(self, missing_cell_data_dir):
        module = load_dist_plot_module()
        missing = DIST_STEPS_CELLS[3]
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                missing_cell_data_dir, FAMILY, list(DIST_STEPS_CELLS)
            )
        assert missing in str(excinfo.value)

    def test_empty_slices_raises_naming_cell(self, empty_slices_data_dir):
        module = load_dist_plot_module()
        no_limit = DIST_STEPS_CELLS[0]
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                empty_slices_data_dir, FAMILY, list(DIST_STEPS_CELLS)
            )
        assert no_limit in str(excinfo.value)


# =========================================================================
# good_cells — degraded-cell exclusion from family images
# =========================================================================


class TestGoodCells:
    """Family images may only include quality=good cells, in cell order."""

    def test_all_cells_good_returns_all_in_order(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        assert module.good_cells(data) == list(DIST_STEPS_CELLS)

    def test_degraded_cell_excluded(self, degraded_family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        good = module.good_cells(data)
        assert REP_CELL not in good
        assert good == [c for c in DIST_STEPS_CELLS if c != REP_CELL]

    def test_any_degraded_row_excludes_cell(self, tmp_path):
        cell = DIST_STEPS_CELLS[3]
        cell_dir = tmp_path / "distribution" / FAMILY / cell
        write_cell_outputs(
            cell_dir,
            cell=cell,
            durations=DIST_STEPS_D10,
            cpu_weight=59,
            cpu_max=50000,
            throttle_ratio=0.96,
            qualities=("good", "degraded"),
        )
        module = load_dist_plot_module()
        data = module.load_family_data(tmp_path, FAMILY, [cell])
        assert module.good_cells(data) == []

    def test_all_degraded_returns_empty(self, all_degraded_family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, all_degraded_family_data_dir)
        assert module.good_cells(data) == []


# =========================================================================
# representative_cell — the Gantt source (best shows the family mechanism)
# =========================================================================


class TestRepresentativeCell:
    """Among good cells, pick the highest throttle_ratio (ties -> first)."""

    def test_picks_highest_ratio_cell(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        assert module.representative_cell(data) == REP_CELL  # ratio 0.99

    def test_skips_degraded_cell(self, degraded_family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        assert module.representative_cell(data) == REP_CELL_DEGRADED_FALLBACK  # 0.97

    def test_tie_breaks_to_first_in_cell_order(self, tmp_path):
        """Two good cells with equal ratio: the earlier cell in --cells order."""
        cell_a = DIST_STEPS_CELLS[0]
        cell_b = DIST_STEPS_CELLS[1]
        write_cell_outputs(
            tmp_path / "distribution" / FAMILY / cell_a,
            cell=cell_a,
            durations=DIST_STEPS_D10,
            cpu_weight=1,
            cpu_max=100000,
            throttle_ratio=0.5,
        )
        write_cell_outputs(
            tmp_path / "distribution" / FAMILY / cell_b,
            cell=cell_b,
            durations=DIST_STEPS_D10,
            cpu_weight=17,
            cpu_max=10000,
            throttle_ratio=0.5,
        )
        module = load_dist_plot_module()
        data = module.load_family_data(tmp_path, FAMILY, [cell_a, cell_b])
        assert module.representative_cell(data) == cell_a

    def test_no_good_cells_raises_valueerror(self, all_degraded_family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, all_degraded_family_data_dir)
        with pytest.raises(ValueError):
            module.representative_cell(data)

    def test_returns_a_good_cell(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        rep = module.representative_cell(data)
        assert rep in module.good_cells(data)


# =========================================================================
# compute_ecdf — the ECDF overlay source (eevdf-plot.py formula)
# =========================================================================


class TestComputeEcdf:
    """ECDF: x sorted ascending, y = rank/n (eevdf-plot.py convention)."""

    def test_ecdf_of_d10(self):
        module = load_dist_plot_module()
        x, y = module.compute_ecdf([float(v) for v in DIST_STEPS_D10])
        assert x == [float(v) for v in DIST_STEPS_D10]
        assert y == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])

    def test_ecdf_sorted_and_monotonic(self):
        module = load_dist_plot_module()
        x, y = module.compute_ecdf([30.0, 10.0, 20.0])
        assert x == [10.0, 20.0, 30.0]
        assert y == pytest.approx([1 / 3, 2 / 3, 1.0])
        assert all(a <= b for a, b in zip(y, y[1:]))

    def test_ecdf_single_value(self):
        module = load_dist_plot_module()
        x, y = module.compute_ecdf([42.0])
        assert x == [42.0]
        assert y == pytest.approx([1.0])

    def test_ecdf_empty(self):
        module = load_dist_plot_module()
        x, y = module.compute_ecdf([])
        assert x == []
        assert y == []


# =========================================================================
# cumulative_runtime_series — the runtime-trajectory source
# =========================================================================


class TestCumulativeRuntime:
    """Per-pod cumulative runtime_ns over ts, sorted deterministically."""

    def test_cumulative_sums_in_ts_order(self):
        module = load_dist_plot_module()
        rows = [
            {"ts": 4_000_000, "pod": "stress-ng", "runtime_ns": 1_000_000},
            {"ts": 2_000_000, "pod": "stress-ng", "runtime_ns": 500_000},
            {"ts": 3_000_000, "pod": "stress-ng", "runtime_ns": 700_000},
        ]
        df = module.cumulative_runtime_series(rows)
        assert list(df["ts"]) == [2_000_000, 3_000_000, 4_000_000]
        assert list(df["cumulative_ns"]) == [500_000, 1_200_000, 2_200_000]

    def test_groups_by_pod(self):
        module = load_dist_plot_module()
        rows = [
            {"ts": 2_000_000, "pod": "a", "runtime_ns": 100},
            {"ts": 3_000_000, "pod": "b", "runtime_ns": 200},
            {"ts": 4_000_000, "pod": "a", "runtime_ns": 300},
        ]
        df = module.cumulative_runtime_series(rows)
        pods_a = df[df["pod"] == "a"]
        pods_b = df[df["pod"] == "b"]
        assert list(pods_a["cumulative_ns"]) == [100, 400]
        assert list(pods_b["cumulative_ns"]) == [200]

    def test_empty_returns_empty_with_pinned_columns(self):
        module = load_dist_plot_module()
        df = module.cumulative_runtime_series([])
        assert list(df.columns) == ["pod", "ts", "runtime_ns", "cumulative_ns"]
        assert df.empty

    def test_cumulative_never_decreases(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        df = module.cumulative_runtime_series(data[REP_CELL]["runtime_rows"])
        cum = list(df["cumulative_ns"])
        assert cum == pytest.approx(
            [(i + 1) * RUNTIME_STEP_NS for i in range(len(DIST_STEPS_D10))]
        )
        assert all(a <= b for a, b in zip(cum, cum[1:]))


# =========================================================================
# histogram_annotations — the percentile overlay text (no-OCR mechanism)
# =========================================================================


class TestHistogramAnnotations:
    """slice-histogram.png overlays mean/median/p95/p99 from the summary."""

    def test_d10_annotations(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        lines = module.histogram_annotations(data[REP_CELL])
        assert "mean 55 us" in lines
        assert "median 55 us" in lines
        assert "p95 95.5 us" in lines
        assert "p99 99.1 us" in lines

    def test_annotations_track_fixture_data_not_hardcoded(self, tmp_path):
        """Provenance ("rendered from dist-analyze output"): with
        DIFFERENT fixture durations the annotations must carry the new
        measured percentiles — a hardcoded 95.5 would be caught here."""
        cell = REP_CELL
        cell_dir = tmp_path / "distribution" / FAMILY / cell
        write_cell_outputs(
            cell_dir,
            cell=cell,
            durations=DIST_STEPS_ALT,
            cpu_weight=17,
            cpu_max=10000,
            throttle_ratio=0.99,
        )
        module = load_dist_plot_module()
        data = module.load_family_data(tmp_path, FAMILY, [cell])
        lines = module.histogram_annotations(data[cell])
        assert f"mean {ALT_MEAN:g} us" in lines
        assert f"median {ALT_MEDIAN:g} us" in lines
        assert f"p95 {ALT_P95:g} us" in lines
        assert f"p99 {ALT_P99:g} us" in lines

    def test_annotation_values_come_from_summary(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        lines = module.histogram_annotations(data[REP_CELL])
        for value, key in ((D10_MEAN, "mean_us"), (D10_P95, "p95_us")):
            expected = f"{key.split('_')[0]} {value:g} us"
            assert any(line == expected for line in lines)


# =========================================================================
# gantt_annotations — per-CPU / per-pod / throttle-hatch contract
# =========================================================================


class TestGanttAnnotations:
    """The Gantt must name its cell, CPU lanes, pods, and throttle state."""

    def test_cell_label_and_cpu_lanes(self, gantt_cell_data_dir):
        module = load_dist_plot_module()
        data = module.load_family_data(gantt_cell_data_dir, FAMILY, [REP_CELL])
        lines = module.gantt_annotations(data[REP_CELL])
        assert REP_CELL in lines
        assert "CPU 0" in lines
        assert "CPU 1" in lines

    def test_pod_names_present(self, gantt_cell_data_dir):
        module = load_dist_plot_module()
        data = module.load_family_data(gantt_cell_data_dir, FAMILY, [REP_CELL])
        lines = module.gantt_annotations(data[REP_CELL])
        assert "pod stress-ng" in lines
        assert "pod system" in lines

    def test_throttled_quota_cell_has_hatch_annotation(self, gantt_cell_data_dir):
        module = load_dist_plot_module()
        data = module.load_family_data(gantt_cell_data_dir, FAMILY, [REP_CELL])
        lines = module.gantt_annotations(data[REP_CELL])
        assert "throttle gaps hatched" in lines
        assert "throttle_ratio 0.99" in lines

    def test_no_throttle_cell_has_no_hatch_annotation(self, family_data_dir):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        no_throttle = DIST_STEPS_CELLS[0]  # request=-limit=, ratio 0.0
        lines = module.gantt_annotations(data[no_throttle])
        assert "throttle gaps hatched" not in lines
        assert no_throttle in lines
        assert "CPU 0" in lines


# =========================================================================
# In-process rendering — figure text objects carry the labels/annotations
# =========================================================================


class TestRenderInProcess:
    """Render functions write the PNG AND return a Figure whose text objects
    contain every annotation/label string (the no-OCR mechanism)."""

    @pytest.mark.parametrize("cell_index", [0, 1, 3])
    def test_render_cell_histogram_writes_openable_png(
        self, cell_index, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        cell = DIST_STEPS_CELLS[cell_index]
        out = tmp_path / f"hist-{cell_index}.png"
        fig = module.render_cell_histogram(data[cell], out)
        assert fig is not None
        assert_openable_png(out)

    def test_render_cell_histogram_log_x_axis(self, family_data_dir, tmp_path):
        """Log-x histogram of slice durations."""
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "hist.png"
        fig = module.render_cell_histogram(data[REP_CELL], out)
        assert fig.axes[0].get_xscale() == "log"

    def test_render_cell_histogram_figure_contains_annotations(
        self, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "hist.png"
        fig = module.render_cell_histogram(data[REP_CELL], out)
        texts = figure_texts(fig)
        for line in module.histogram_annotations(data[REP_CELL]):
            assert line in texts, f"histogram annotation not in figure: {line!r}"

    def test_render_slice_comparison_writes_openable_png(
        self, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "cmp.png"
        fig = module.render_slice_comparison(data, out)
        assert fig is not None
        assert_openable_png(out)
        assert fig.axes[0].get_xscale() == "log"

    def test_render_slice_comparison_labels_every_good_cell(
        self, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "cmp.png"
        fig = module.render_slice_comparison(data, out)
        texts = figure_texts(fig)
        for cell in DIST_STEPS_CELLS:
            assert cell in texts, f"comparison figure missing cell label {cell!r}"

    def test_render_slice_comparison_excludes_degraded_cell(
        self, degraded_family_data_dir, tmp_path
    ):
        """A degraded cell must never appear in family comparison."""
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        out = tmp_path / "cmp.png"
        fig = module.render_slice_comparison(data, out)
        texts = figure_texts(fig)
        assert REP_CELL not in texts, "degraded cell leaked into comparison image"
        for cell in DIST_STEPS_CELLS:
            if cell != REP_CELL:
                assert cell in texts

    def test_render_ecdf_overlay_writes_openable_png(self, family_data_dir, tmp_path):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "ecdf.png"
        fig = module.render_ecdf_overlay(data, out)
        assert fig is not None
        assert_openable_png(out)
        assert fig.axes[0].get_xscale() == "log"

    def test_render_ecdf_overlay_labels_every_good_cell(
        self, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "ecdf.png"
        fig = module.render_ecdf_overlay(data, out)
        texts = figure_texts(fig)
        for cell in DIST_STEPS_CELLS:
            assert cell in texts, f"ECDF figure missing cell label {cell!r}"

    def test_render_ecdf_overlay_excludes_degraded_cell(
        self, degraded_family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        out = tmp_path / "ecdf.png"
        fig = module.render_ecdf_overlay(data, out)
        texts = figure_texts(fig)
        assert REP_CELL not in texts, "degraded cell leaked into ECDF overlay"
        for cell in DIST_STEPS_CELLS:
            if cell != REP_CELL:
                assert cell in texts

    def test_render_gantt_writes_openable_png_with_annotations(
        self, gantt_cell_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = module.load_family_data(gantt_cell_data_dir, FAMILY, [REP_CELL])
        out = tmp_path / "gantt.png"
        fig = module.render_gantt(data[REP_CELL], out)
        assert fig is not None
        assert_openable_png(out)
        texts = figure_texts(fig)
        for line in module.gantt_annotations(data[REP_CELL]):
            assert line in texts, f"gantt annotation not in figure: {line!r}"

    def test_render_runtime_trajectory_writes_openable_png(
        self, family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "traj.png"
        fig = module.render_runtime_trajectory(data, out)
        assert fig is not None
        assert_openable_png(out)

    def test_render_runtime_trajectory_labels_every_good_cell_pod(
        self, family_data_dir, tmp_path
    ):
        """Legend convention: '{cell} {pod}' per (cell, pod) series."""
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        out = tmp_path / "traj.png"
        fig = module.render_runtime_trajectory(data, out)
        texts = figure_texts(fig)
        for cell in DIST_STEPS_CELLS:
            assert f"{cell} stress-ng" in texts, (
                f"trajectory figure missing legend entry for {cell!r}"
            )

    def test_render_runtime_trajectory_excludes_degraded_cell(
        self, degraded_family_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        out = tmp_path / "traj.png"
        fig = module.render_runtime_trajectory(data, out)
        texts = figure_texts(fig)
        assert f"{REP_CELL} stress-ng" not in texts, (
            "degraded cell leaked into trajectory image"
        )
        for cell in DIST_STEPS_CELLS:
            if cell != REP_CELL:
                assert f"{cell} stress-ng" in texts

    def test_render_all_writes_req017_layout(self, family_data_dir, tmp_path):
        """Per-cell histograms + family images, all openable."""
        module = load_dist_plot_module()
        data = load_family_data(module, family_data_dir)
        figures = module.render_all(data, tmp_path)
        expected_keys = {f"{cell}/slice-histogram.png" for cell in DIST_STEPS_CELLS}
        expected_keys |= set(FAMILY_IMAGES)
        assert set(figures.keys()) == expected_keys
        for cell in DIST_STEPS_CELLS:
            assert_openable_png(tmp_path / cell / "slice-histogram.png")
        for name in FAMILY_IMAGES:
            assert_openable_png(tmp_path / name)

    def test_render_all_still_renders_degraded_cell_histogram(
        self, degraded_family_data_dir, tmp_path
    ):
        """Degraded cells keep their per-cell histogram."""
        module = load_dist_plot_module()
        data = load_family_data(module, degraded_family_data_dir)
        module.render_all(data, tmp_path)
        assert_openable_png(tmp_path / REP_CELL / "slice-histogram.png")


# =========================================================================
# CLI contract
# =========================================================================


class TestCli:
    """--data-dir / --output-dir / --family / --cells contract."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_dist_plot(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        for flag in ("--data-dir", "--output-dir", "--family", "--cells"):
            assert flag in combined

    def test_missing_required_flags_exits_nonzero(self):
        rc, _out, err = run_dist_plot([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero(self, tmp_path):
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(tmp_path / "missing"),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0
        assert "missing" in err or "data-dir" in err

    def test_missing_cell_fails_loudly_naming_cell(
        self, missing_cell_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(missing_cell_data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0, "missing cell must fail loudly"
        assert DIST_STEPS_CELLS[3] in err

    def test_empty_slices_fails_loudly_naming_cell(
        self, empty_slices_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(empty_slices_data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0, "empty slices must fail loudly"
        assert DIST_STEPS_CELLS[0] in err

    def test_all_degraded_family_fails_loudly(
        self, all_degraded_family_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(all_degraded_family_data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0, "no good cells must fail loudly"
        assert "no good cells" in err

    def test_e2e_produces_req017_layout(self, family_data_dir, tmp_path):
        """Per-cell slice-histogram.png + the four family images."""
        out_root = tmp_path / "out"
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(family_data_dir),
                "--output-dir",
                str(out_root),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc == 0, f"stderr: {err}"
        family_out = out_root / "distribution" / FAMILY
        assert family_out.is_dir(), f"missing family output dir: {family_out}"
        for cell in DIST_STEPS_CELLS:
            assert_openable_png(family_out / cell / "slice-histogram.png")
        for name in FAMILY_IMAGES:
            assert_openable_png(family_out / name)

    def test_e2e_degraded_cell_keeps_histogram_and_family_images_exclude_it(
        self, degraded_family_data_dir, tmp_path
    ):
        """End-to-end: degraded cell's histogram still rendered."""
        out_root = tmp_path / "out"
        rc, _out, err = run_dist_plot(
            [
                "--data-dir",
                str(degraded_family_data_dir),
                "--output-dir",
                str(out_root),
                "--family",
                FAMILY,
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc == 0, f"stderr: {err}"
        family_out = out_root / "distribution" / FAMILY
        assert_openable_png(family_out / REP_CELL / "slice-histogram.png")
        for name in FAMILY_IMAGES:
            assert_openable_png(family_out / name)


# =========================================================================
# Determinism
# =========================================================================


class TestDeterminism:
    """Two CLI runs on the same staged data yield byte-identical outputs."""

    def test_two_runs_produce_identical_sha256(self, family_data_dir, tmp_path):
        argv = [
            "--data-dir",
            str(family_data_dir),
            "--family",
            FAMILY,
            "--cells",
            ",".join(DIST_STEPS_CELLS),
        ]
        rc1, _o1, err1 = run_dist_plot(
            [*argv, "--output-dir", str(tmp_path / "out1")], env=agg_env()
        )
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, _o2, err2 = run_dist_plot(
            [*argv, "--output-dir", str(tmp_path / "out2")], env=agg_env()
        )
        assert rc2 == 0, f"second run failed: {err2}"
        m1 = sha256_manifest(tmp_path / "out1" / "distribution")
        m2 = sha256_manifest(tmp_path / "out2" / "distribution")
        assert m1 == m2

    def test_manifest_covers_exactly_req017_layout(self, family_data_dir, tmp_path):
        argv = [
            "--data-dir",
            str(family_data_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--family",
            FAMILY,
            "--cells",
            ",".join(DIST_STEPS_CELLS),
        ]
        rc, _out, err = run_dist_plot(argv, env=agg_env())
        assert rc == 0, f"stderr: {err}"
        manifest = sha256_manifest(tmp_path / "out" / "distribution")
        expected = {f"{FAMILY}/{cell}/slice-histogram.png" for cell in DIST_STEPS_CELLS}
        expected |= {f"{FAMILY}/{name}" for name in FAMILY_IMAGES}
        assert set(manifest.keys()) == expected

    def test_two_runs_degraded_variant_identical_sha256(
        self, degraded_family_data_dir, tmp_path
    ):
        argv = [
            "--data-dir",
            str(degraded_family_data_dir),
            "--family",
            FAMILY,
            "--cells",
            ",".join(DIST_STEPS_CELLS),
        ]
        rc1, _o1, err1 = run_dist_plot(
            [*argv, "--output-dir", str(tmp_path / "out1")], env=agg_env()
        )
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, _o2, err2 = run_dist_plot(
            [*argv, "--output-dir", str(tmp_path / "out2")], env=agg_env()
        )
        assert rc2 == 0, f"second run failed: {err2}"
        m1 = sha256_manifest(tmp_path / "out1" / "distribution")
        m2 = sha256_manifest(tmp_path / "out2" / "distribution")
        assert m1 == m2
