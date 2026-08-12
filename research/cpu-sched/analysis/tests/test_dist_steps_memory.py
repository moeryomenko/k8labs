"""Memory-structure tests for dist-steps.py — fixed bins + bounded step-4 bars.

Test-first design (RED against the current implementation; the engineer makes
these pass as part of the memory-optimization work).  The names used here are
the NEW API surface the implementation must add WITHOUT breaking the existing
dist-steps contract (``load_family_data`` keeps returning
``slices_us: list[float]`` — pinned by the 40 existing tests — while the
rendering steps change their bin/bar strategy).

What is pinned here (fixed bins + vectorized bars):

    research/cpu-sched/analysis/dist-steps.py  (module: dist_steps)

    NEW constants:
      STEP3_BINS         int == 80  — the fixed log-histogram bin count for
                           step-3 (replaces bins=max(4, len(slices)), the
                           ~2.7M-bin bug on real data).
      STEP4_BAR_BUDGET   int > 0, <= 500  — the max number of bar artists
                           step-4 may draw (vectorized call or fixed
                           downsampled budget; never one artist per slice).

The artist-bound tests use a synthetic 50K-slice family fixture (deterministic
D10-set durations repeated) so the CURRENT implementation renders ~50K
histogram bins (step-3) and ~50K bars (step-4) — far above the bounds — while
the optimized implementation stays under them.

Existing behavior stays green (requirement d): test_dist_steps.py 40 tests
are NOT modified and must keep passing.

Run from research/cpu-sched/analysis:
    python3 -m pytest tests/test_dist_steps_memory.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")  # headless rendering for the in-process figure tests

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from tests.conftest import (  # noqa: E402
    DIST_STEPS_CELLS,
    DIST_STEPS_D10,
    build_dist_steps_family,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_STEPS_SCRIPT = ANALYSIS_DIR / "dist-steps.py"

FAMILY = "dist-stress-ng"
LARGE_N = 50_000  # synthetic slice rows per cell for the artist-bound tests
STEP4_BOUND_SLACK = 1  # the throttle-gap axvspan is one extra patch


# =========================================================================
# Helpers
# =========================================================================


def load_dist_steps_module():
    """Import the script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("dist_steps", DIST_STEPS_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_STEPS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_of(path: pathlib.Path) -> str:
    """SHA-256 hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_family_data(module, data_dir: pathlib.Path) -> dict:
    """Load the six-cell fixture data through the module's own reader."""
    return module.load_family_data(data_dir, FAMILY, list(DIST_STEPS_CELLS))


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def large_steps_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Six-cell family where every cell carries 50K deterministic slices.

    Reuses the conftest builder with the D10 duration set repeated to
    LARGE_N — step-3 reads cells[0] (no-limit), step-4 reads cells[3]
    (500m/500m quota cell), both with 50K rows.
    """
    durations = list(
        np.tile(
            np.asarray([float(v) for v in DIST_STEPS_D10], dtype=float), LARGE_N // 10
        )
    )
    return build_dist_steps_family(tmp_path / "dist-steps-large", durations=durations)


# =========================================================================
# Step-3 — fixed bin count (replaces bins=len(slices))
# =========================================================================


class TestStep3FixedBins:
    """Step-3's slice-duration histogram uses a fixed small bin count."""

    def test_step3_bins_constant_pinned(self):
        module = load_dist_steps_module()
        assert hasattr(module, "STEP3_BINS"), "missing pinned name: STEP3_BINS"
        assert module.STEP3_BINS == 80, (
            f"STEP3_BINS must be the fixed bin count 80, got {module.STEP3_BINS}"
        )

    def test_step3_renders_fixed_bin_count_on_small_family(
        self, dist_steps_family_data_dir, tmp_path
    ):
        """Even a 10-slice cell renders 80 histogram bins (one patch per bin).

        RED today: the current implementation uses bins=max(4, len(slices))
        -> 10 bins on the D10 fixture.
        """
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        out = tmp_path / "step3-small.png"
        fig = module.render_step(3, data, out)
        n_bins = len(fig.axes[0].patches)
        assert n_bins == 80, (
            f"step-3 histogram rendered {n_bins} bins; expected the fixed 80"
        )
        assert n_bins == module.STEP3_BINS

    def test_step3_renders_fixed_bin_count_on_large_fixture(
        self, large_steps_data_dir, tmp_path
    ):
        """50K slices must NOT produce 50K bins (the ~2.7M-bin bug on real data).

        RED today: bins=max(4, len(slices)) -> 50K bins.
        """
        module = load_dist_steps_module()
        data = load_family_data(module, large_steps_data_dir)
        out = tmp_path / "step3-large.png"
        fig = module.render_step(3, data, out)
        n_bins = len(fig.axes[0].patches)
        assert n_bins == 80, (
            f"step-3 histogram rendered {n_bins} bins on a 50K-slice fixture; "
            "bins=len(slices) is not allowed (fixed 80)"
        )
        assert n_bins == module.STEP3_BINS


# =========================================================================
# Step-4 — bounded bar artists (vectorized / fixed budget)
# =========================================================================


class TestStep4VectorizedBars:
    """Step-4's throttle-pattern bars must never be one artist per slice."""

    def test_step4_bar_budget_constant_pinned(self):
        module = load_dist_steps_module()
        assert hasattr(module, "STEP4_BAR_BUDGET"), (
            "missing pinned name: STEP4_BAR_BUDGET"
        )
        budget = module.STEP4_BAR_BUDGET
        assert isinstance(budget, int)
        assert 0 < budget <= 500, (
            f"STEP4_BAR_BUDGET must be a small fixed budget <= 500, got {budget}"
        )

    def test_step4_artist_count_bounded_on_large_fixture(
        self, large_steps_data_dir, tmp_path
    ):
        """50K slices render at most STEP4_BAR_BUDGET bar artists.

        The +1 slack accounts for the throttle-gap axvspan patch (a Rectangle
        on the same axes).  RED today: the current implementation calls
        ax.bar once per slice (~50K patches).
        """
        module = load_dist_steps_module()
        data = load_family_data(module, large_steps_data_dir)
        out = tmp_path / "step4-large.png"
        fig = module.render_step(4, data, out)
        n_patches = len(fig.axes[0].patches)
        assert n_patches <= 500 + STEP4_BOUND_SLACK, (
            f"step-4 drew {n_patches} patches on a 50K-slice fixture; "
            "per-slice bar artists are not allowed (bound 500)"
        )
        assert n_patches <= module.STEP4_BAR_BUDGET + STEP4_BOUND_SLACK, (
            f"step-4 drew {n_patches} patches (budget {module.STEP4_BAR_BUDGET}); "
            "exposed STEP4_BAR_BUDGET must be honored"
        )


# =========================================================================
# Determinism on the large (fixed-bins / budgeted-bars) render path
# =========================================================================


class TestDeterminism:
    """Two renders of the changed steps on the same staged data are identical."""

    def test_step3_and_step4_rerun_identical_on_large_fixture(
        self, large_steps_data_dir, tmp_path
    ):
        module = load_dist_steps_module()
        data = load_family_data(module, large_steps_data_dir)
        for step, name in ((3, "step3"), (4, "step4")):
            out1 = tmp_path / f"{name}-1.png"
            out2 = tmp_path / f"{name}-2.png"
            module.render_step(step, data, out1)
            module.render_step(step, data, out2)
            assert sha256_of(out1) == sha256_of(out2), (
                f"step-{step} render is not deterministic"
            )
