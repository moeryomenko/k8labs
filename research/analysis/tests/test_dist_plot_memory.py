"""Memory-structure tests for dist-plot.py — compact arrays + bounded Gantt.

Test-first design (RED against the current implementation; the engineer makes
these pass as part of the memory-optimization work).  The module/function/
constant names used here are the NEW API surface the implementation must add
WITHOUT breaking the existing dist-plot contract (the existing
``load_family_data`` return shape — ``slices: list[dict]``, ``durations_us:
list[float]``, ``runtime_rows: list[dict]`` — is pinned by the 66 existing
tests and MUST stay intact).

What is pinned here (no-dict-explosion + bounded artists):

    research/analysis/dist-plot.py  (module: dist_plot)

    NEW constants:
      GANTT_BAR_BUDGET   int > 0, <= 500  — the max number of bar artists the
                           Gantt may draw on any slice dataset (the fixed bar
                           budget / downsampling cap).

    NEW pure-core helper:
      load_slices_arrays(analysis_root: Path | str, family: str, cell: str)
          -> dict[str, np.ndarray]
          # {ts_start_us, ts_end_us, duration_us, cpu, pod} — the Gantt data
          # source, read from dist-slices.csv WITHOUT per-row dicts (no
          # to_dict(orient="records")).  Every value is a numpy array with
          # one element per slice row; duration_us is floating kind, cpu is
          # integer kind.  A cell with a missing dist-slices.csv raises
          # FileNotFoundError naming the cell; a header-only dist-slices.csv
          # raises ValueError naming the cell (same loud-failure convention
          # as load_family_data).

Existing behavior stays green (requirement d): test_dist_plot.py 66 tests
are NOT modified and must keep passing — the existing loader contract
(``slices: list[dict]``) is preserved for the legacy path while the Gantt
render path uses the array-backed helper.

The artist-bound tests use a synthetic 50K-slice fixture (fast to build,
deterministic) so the CURRENT implementation (one ax.barh per slice) draws
~50K patches — far above the bound — while the optimized implementation
(vectorized bars + fixed budget) stays under it.

Run from research/analysis:
    python3 -m pytest tests/test_dist_plot_memory.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")  # headless rendering for the in-process figure tests

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from tests.conftest import (  # noqa: E402
    DIST_PERCENTILE_STEPS,
    DIST_RUNTIME_COLUMNS,
    DIST_SLICES_COLUMNS,
    DIST_STEPS_CELLS,
    DIST_STEPS_D10,
    DIST_SUMMARY_COLUMNS,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_PLOT_SCRIPT = ANALYSIS_DIR / "dist-plot.py"

FAMILY = "dist-stress-ng"
# The Gantt renders the representative cell (highest throttle_ratio) — the
# throttled 100m/100m cell is the natural large-fixture cell.
LARGE_CELL = DIST_STEPS_CELLS[1]  # request=100m-limit=100m, throttle_ratio 0.99
# The no-limit cell (ratio 0.0) has no hatch patch, so its Gantt artist count
# is exactly the bar count — used by the exact-small-count test.
SMALL_CELL = DIST_STEPS_CELLS[0]  # request=-limit=
LARGE_N = 50_000  # synthetic slice rows for the artist-bound tests
GANTT_BOUND_SLACK = 1  # the throttle-hatch axhspan is one extra patch

# Pinned helper contract: the exact array keys load_slices_arrays returns.
SLICES_ARRAY_KEYS = ("ts_start_us", "ts_end_us", "duration_us", "cpu", "pod")

# Fixture geometry: deterministic 1s-spaced slice starts from 2.5s (same
# convention as the dist-plot/dist-steps fixtures).
LARGE_TS_START = 2_500_000
LARGE_TS_STEP = 10_000


# =========================================================================
# Helpers
# =========================================================================


def load_dist_plot_module():
    """Import the script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("dist_plot", DIST_PLOT_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_PLOT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_of(path: pathlib.Path) -> str:
    """SHA-256 hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_family_data(module, data_dir: pathlib.Path, cells: list[str]) -> dict:
    """Load cells through the module's own (legacy-contract) reader."""
    return module.load_family_data(data_dir, FAMILY, cells)


def _fixture_durations(n: int) -> np.ndarray:
    """Deterministic duration_us values: the D10 set repeated to length *n*."""
    base = np.asarray([float(v) for v in DIST_STEPS_D10], dtype=float)
    return np.tile(base, n // len(base) + 1)[:n]


# =========================================================================
# Fixture writers — dist-analyze OUTPUT trees (no traces, no cluster)
# =========================================================================


def write_large_cell_outputs(
    cell_dir: pathlib.Path,
    *,
    cell: str,
    n_slices: int,
    throttle_ratio: float,
    cpu_weight: int = 17,
    cpu_max: int = 10000,
) -> pathlib.Path:
    """Write one cell's dist-analyze OUTPUT with *n_slices* deterministic rows.

    dist-slices.csv holds n_slices rows: durations cycle through the D10 set,
    cpu alternates 0/1, every 100th slice is a ``system`` pod row.  All
    timestamps are derived from the fixture start/step constants (no
    wall-clock — determinism).  dist-runtime.csv holds a small constant set of
    per-switch deltas (the trajectory is not exercised here).  The summary
    row carries the pandas-method stats for the generated durations.
    """
    cell_dir.mkdir(parents=True, exist_ok=True)
    dur = _fixture_durations(n_slices)
    ts_start = LARGE_TS_START + np.arange(n_slices, dtype=np.int64) * LARGE_TS_STEP
    ts_end = ts_start + dur.astype(np.int64)
    cpu = np.arange(n_slices, dtype=np.int64) % 2
    pod = np.where((np.arange(1, n_slices + 1) % 100) == 0, "system", "stress-ng")

    pd.DataFrame(
        {
            "ts_start_us": ts_start,
            "ts_end_us": ts_end,
            "duration_us": dur,
            "cpu": cpu,
            "tid": np.full(n_slices, 1001),
            "thread_name": np.full(n_slices, "stress-ng-cpu"),
            "pod": pod,
        },
        columns=DIST_SLICES_COLUMNS,
    ).to_csv(cell_dir / "dist-slices.csv", index=False)

    runtime_n = min(n_slices, 200)
    pd.DataFrame(
        {
            "ts": ts_start[:runtime_n],
            "cpu": cpu[:runtime_n],
            "pid": np.full(runtime_n, 1001),
            "tid": np.full(runtime_n, 1001),
            "thread_name": np.full(runtime_n, "stress-ng-cpu"),
            "pod": np.full(runtime_n, "stress-ng"),
            "runtime_ns": np.full(runtime_n, 1_400_000),
        },
        columns=DIST_RUNTIME_COLUMNS,
    ).to_csv(cell_dir / "dist-runtime.csv", index=False)

    series = pd.Series(dur, dtype="float64")
    summary_row = [
        cell,
        1,
        "stress-ng",
        n_slices,
        float(series.sum()) / 1000.0,
        float(series.mean()),
        float(series.median()),
        float(series.quantile(0.50)),
        float(series.quantile(0.95)),
        float(series.quantile(0.99)),
        float(series.max()),
        throttle_ratio,
        cpu_weight,
        cpu_max,
        "good",
    ]
    pd.DataFrame([summary_row], columns=DIST_SUMMARY_COLUMNS).to_csv(
        cell_dir / "dist-summary.csv", index=False
    )

    table = {f"p{k}": float(series.quantile(k / 100.0)) for k in DIST_PERCENTILE_STEPS}
    (cell_dir / "dist-percentiles.json").write_text(
        json.dumps({"1": {"stress-ng": table}}, indent=2, sort_keys=True) + "\n"
    )
    return cell_dir


@pytest.fixture
def large_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis root with one 50K-slice cell: the Gantt artist-bound stress case.

    Returns the ANALYSIS ROOT (like ``family_data_dir`` in test_dist_plot.py)
    so ``load_family_data(root, family, cells)`` resolves the cell under
    ``root/distribution/<family>/<cell>/``.
    """
    write_large_cell_outputs(
        tmp_path / "distribution" / FAMILY / LARGE_CELL,
        cell=LARGE_CELL,
        n_slices=LARGE_N,
        throttle_ratio=0.99,
    )
    return tmp_path


@pytest.fixture
def small_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Analysis root with one 10-slice non-throttled cell (exact-count case)."""
    write_large_cell_outputs(
        tmp_path / "distribution" / FAMILY / SMALL_CELL,
        cell=SMALL_CELL,
        n_slices=10,
        throttle_ratio=0.0,
        cpu_weight=1,
        cpu_max=100000,
    )
    return tmp_path


@pytest.fixture
def missing_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Cell dir with NO dist-slices.csv (load_slices_arrays must fail loudly)."""
    cell = "missing-cell"
    cell_dir = tmp_path / "distribution" / FAMILY / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    (cell_dir / "dist-summary.csv").write_text(",".join(DIST_SUMMARY_COLUMNS) + "\n")
    return tmp_path


@pytest.fixture
def empty_slices_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Cell dir whose dist-slices.csv is header-only (zero rows)."""
    cell = "empty-cell"
    cell_dir = tmp_path / "distribution" / FAMILY / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=DIST_SLICES_COLUMNS).to_csv(
        cell_dir / "dist-slices.csv", index=False
    )
    return tmp_path


# =========================================================================
# load_slices_arrays — compact array-backed Gantt data (no dicts per row)
# =========================================================================


class TestLoadSlicesArrays:
    """The Gantt data source must be numpy arrays, never per-row dicts.

    This is the no-dict-explosion structural pin: the NEW helper replaces
    ``to_dict(orient="records")`` on the Gantt path while the existing
    ``load_family_data`` contract (``slices: list[dict]``) stays intact for
    the existing tests.
    """

    def test_helper_is_exposed_and_callable(self):
        module = load_dist_plot_module()
        assert hasattr(module, "load_slices_arrays"), (
            "missing pinned name: load_slices_arrays"
        )
        assert callable(module.load_slices_arrays)

    def test_small_cell_returns_arrays_matching_fixture(self, small_cell_data_dir):
        """On 10 slices the arrays must carry exactly the CSV values."""
        module = load_dist_plot_module()
        arrays = module.load_slices_arrays(small_cell_data_dir, FAMILY, SMALL_CELL)
        assert set(arrays.keys()) == set(SLICES_ARRAY_KEYS)
        for key in SLICES_ARRAY_KEYS:
            assert isinstance(arrays[key], np.ndarray), f"{key} is not an ndarray"
            assert not isinstance(arrays[key], list), f"{key} is a list, not an array"
        assert len(arrays["ts_start_us"]) == 10
        assert arrays["ts_start_us"][0] == LARGE_TS_START
        assert arrays["ts_start_us"][1] == LARGE_TS_START + LARGE_TS_STEP
        np.testing.assert_allclose(
            arrays["duration_us"], [float(v) for v in DIST_STEPS_D10]
        )
        assert int(arrays["cpu"][0]) == 0
        assert int(arrays["cpu"][1]) == 1
        assert np.asarray(arrays["pod"])[0] == "stress-ng"

    def test_large_cell_returns_compact_arrays_no_dicts(self, large_cell_data_dir):
        """50K rows come back as one array per column — no list[dict]."""
        module = load_dist_plot_module()
        arrays = module.load_slices_arrays(large_cell_data_dir, FAMILY, LARGE_CELL)
        assert set(arrays.keys()) == set(SLICES_ARRAY_KEYS)
        for key in SLICES_ARRAY_KEYS:
            value = arrays[key]
            assert isinstance(value, np.ndarray), f"{key} is not an ndarray"
            assert len(value) == LARGE_N, f"{key} length mismatch"
        # compact dtypes: durations are floating, cpu is integer
        assert arrays["duration_us"].dtype.kind in "if"
        assert arrays["cpu"].dtype.kind in "iu"
        # the array still carries the fixture's deterministic first values
        assert arrays["ts_start_us"][0] == LARGE_TS_START
        assert arrays["ts_end_us"][0] == LARGE_TS_START + int(arrays["duration_us"][0])

    def test_missing_cell_raises_naming_cell(self, missing_cell_data_dir):
        module = load_dist_plot_module()
        with pytest.raises(FileNotFoundError) as excinfo:
            module.load_slices_arrays(missing_cell_data_dir, FAMILY, "missing-cell")
        assert "missing-cell" in str(excinfo.value)

    def test_empty_slices_raises_naming_cell(self, empty_slices_data_dir):
        module = load_dist_plot_module()
        with pytest.raises(ValueError) as excinfo:
            module.load_slices_arrays(empty_slices_data_dir, FAMILY, "empty-cell")
        assert "empty-cell" in str(excinfo.value)


# =========================================================================
# Gantt artist bound — a fixed bar budget replaces per-slice ax.barh
# =========================================================================


class TestGanttBarBudget:
    """The rendered Gantt must never draw one artist per slice."""

    def test_gantt_bar_budget_constant_pinned(self):
        module = load_dist_plot_module()
        assert hasattr(module, "GANTT_BAR_BUDGET"), (
            "missing pinned name: GANTT_BAR_BUDGET"
        )
        budget = module.GANTT_BAR_BUDGET
        assert isinstance(budget, int)
        assert 0 < budget <= 500, (
            f"GANTT_BAR_BUDGET must be a small fixed budget <= 500, got {budget}"
        )

    def test_gantt_artist_count_bounded_on_large_fixture(
        self, large_cell_data_dir, tmp_path
    ):
        """50K slices must render at most GANTT_BAR_BUDGET bar artists.

        The +1 slack accounts for the throttle-hatch axhspan patch (the cell
        is throttled, ratio 0.99).  RED today: the current implementation
        draws one ax.barh per slice (~50K patches).
        """
        module = load_dist_plot_module()
        data = load_family_data(module, large_cell_data_dir, [LARGE_CELL])
        out = tmp_path / "gantt-large.png"
        fig = module.render_gantt(data[LARGE_CELL], out)
        n_patches = len(fig.axes[0].patches)
        assert n_patches <= 500 + GANTT_BOUND_SLACK, (
            f"Gantt drew {n_patches} patches on a 50K-slice fixture; "
            "per-slice artists are not allowed (bound 500)"
        )
        assert n_patches <= module.GANTT_BAR_BUDGET + GANTT_BOUND_SLACK, (
            f"Gantt drew {n_patches} patches (budget {module.GANTT_BAR_BUDGET}); "
            "exposed GANTT_BAR_BUDGET must be honored"
        )

    def test_gantt_small_fixture_exact_bar_count(self, small_cell_data_dir, tmp_path):
        """Small datasets keep EXACTLY their slice count — no over-downsample.

        The fixed budget is a cap, not a target: 10 slices render 10 bars.
        The cell is non-throttled (ratio 0.0), so there is no hatch patch.
        """
        module = load_dist_plot_module()
        data = load_family_data(module, small_cell_data_dir, [SMALL_CELL])
        out = tmp_path / "gantt-small.png"
        fig = module.render_gantt(data[SMALL_CELL], out)
        assert len(fig.axes[0].patches) == 10, (
            "small Gantt must draw exactly one bar per slice"
        )


# =========================================================================
# Determinism on the large (budgeted) render path
# =========================================================================


class TestDeterminism:
    """Two renders of the budgeted Gantt on the same staged data are identical."""

    def test_render_gantt_rerun_identical_on_large_fixture(
        self, large_cell_data_dir, tmp_path
    ):
        module = load_dist_plot_module()
        data = load_family_data(module, large_cell_data_dir, [LARGE_CELL])
        out1 = tmp_path / "gantt-1.png"
        out2 = tmp_path / "gantt-2.png"
        module.render_gantt(data[LARGE_CELL], out1)
        module.render_gantt(data[LARGE_CELL], out2)
        assert sha256_of(out1) == sha256_of(out2)
