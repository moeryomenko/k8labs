"""Tests for dist-gif.py — per-replicate bounded loading + O(n) histogram.

Test-first contract for the bounded-loading rewrite.
The current `research/analysis/dist-gif.py` has two memory
hotspots this contract pins:

    1. `generate_family_gifs` loads ALL per-replicate slice DataFrames into a
       `reps` list at once (dist-gif.py:491-509) — holding 3 replicates ~ 3 x
       1M rows simultaneously.  The rewrite must load + validate + render + FREE
       each replicate one at a time.
    2. `_render_hist_gif` builds `window_df = slices_df[slices_df["ts_start_us"]
       <= end_us]` per frame (dist-gif.py:411) — an O(n) boolean-mask copy per
       frame, O(n^2) total.  The rewrite must precompute ts-sorted durations once
       and use a cumulative (searchsorted) path per frame.

Pinned NEW API surface (the rewrite must implement exactly this):

    load_replicate_slices(path: Path) -> pd.DataFrame
        Load ONE per-replicate slice CSV (pinned dist-slices schema).  This is
        the module-level loader `generate_family_gifs` MUST call per replicate
        (patchable: tests spy on it).

    replicate_load_plan(rep_files: list[Path]) -> list[tuple[int, Path]]
        [(replicate_number, path), ...] ordered numerically by n.

    hist_frame_data(ts_sorted_us, durations_sorted_us, end_us) -> np.ndarray
        Durations of slices with ts_start_us <= end_us via
        np.searchsorted(..., side='right') on a PRE-SORTED ts array; returns a
        slice VIEW of durations_sorted_us — never a full-array boolean-mask
        copy.  `_render_hist_gif` MUST call it for the per-frame window.

Behavior preservation (1c) is pinned here too: frame formulas
(min(floor(retained_us/500000), 120) / min(int(retained_s), 120)), canonical
exec-timeline.gif == replicate-1 bytes, determinism reruns identical, Pillow
guard, window-only bars.  The existing 46 tests in tests/test_dist_gif.py stay
green and are NOT modified.

Run from research/analysis:
    python3 -m pytest tests/test_dist_gif_memory.py -q
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import inspect
import pathlib
import sys
import weakref

import numpy as np
import pandas as pd
import pytest

from tests.conftest import (
    DIST_SLICES_COLUMNS,
    DIST_SUMMARY_COLUMNS,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
GIF_SCRIPT = ANALYSIS_DIR / "dist-gif.py"

TIMELINE_GIF = "exec-timeline.gif"
HIST_GIF = "slice-dist-build.gif"
REPLICATE_TIMELINE_TMPL = "exec-timeline-replicate-{n}.gif"
REPLICATE_SLICES_PREFIX = "dist-slices-replicate-"
FAMILY = "dist-stress-ng"
REP_CELL = "request=100m-limit=100m"
NO_LIMIT_CELL = "request=none-limit=none"

# Large fixture geometry: each replicate spans exactly 0..30.0s (30M us) with
# rows_per_rep rows.  The O(n^2) path is measurable at 20K rows per replicate
# (used by the pure structural tests; the render-path tests use small rows so
# the RED run stays fast).
LARGE_ROWS = 20_000
LARGE_SPAN_US = 30_000_000
LARGE_TIMELINE_FRAMES = 60  # min(30_000_000 // 500_000, 120)
LARGE_WINDOW_EXPECTED = 20_000 * 2 // 30  # density x 2s window ~= 1333

# Small fixture geometry (behavior E2E): replicate 1 spans 4.0s..10.0s
# (retained 6.0s -> 12 timeline frames / 6 hist frames), replicate 2 spans
# 1.0s..5.0s (retained 4.0s -> 8 timeline frames).
SMALL_REP1_FRAMES = 12
SMALL_REP2_FRAMES = 8
SMALL_HIST_FRAMES = 6


# =========================================================================
# Helpers
# =========================================================================


def load_dist_gif_module():
    """Import the script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("dist_gif", GIF_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {GIF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_slices_csv(path: pathlib.Path, rows: list[tuple]) -> pathlib.Path:
    """Write a pinned dist-slices.csv (SLICES_COLUMNS schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(DIST_SLICES_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_summary_csv(path: pathlib.Path, rows: list[tuple]) -> pathlib.Path:
    """Write a pinned dist-summary.csv (SUMMARY_COLUMNS schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(DIST_SUMMARY_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def slice_row(
    ts_start_us: int,
    duration_us: int,
    cpu: int = 0,
    tid: int = 1001,
    thread_name: str = "stress-ng-cpu",
    pod: str = "stress-ng",
) -> tuple:
    """A single dist-slices.csv row (ts_start_us, ts_end_us, duration_us, ...)."""
    return (
        ts_start_us,
        ts_start_us + duration_us,
        duration_us,
        cpu,
        tid,
        thread_name,
        pod,
    )


def _summary_rows() -> list[tuple]:
    """REP_CELL dist-summary rows; cpu_max = 10000 -> REP_CELL is a quota cell."""
    return [
        (
            REP_CELL,
            1,
            "stress-ng",
            7,
            2.8,
            400.0,
            400.0,
            400.0,
            670.0,
            694.0,
            700.0,
            0.99,
            17,
            10000,
            "good",
        ),
        (
            REP_CELL,
            1,
            "system",
            1,
            0.25,
            250.0,
            250.0,
            250.0,
            250.0,
            250.0,
            250.0,
            0.0,
            0,
            0,
            "good",
        ),
    ]


def _large_rep_df(
    rows_per_rep: int = LARGE_ROWS, span_us: int = LARGE_SPAN_US
) -> pd.DataFrame:
    """One synthetic replicate DataFrame spanning exactly 0..span_us.

    Deterministic: ts = i * span_us / (rows_per_rep - 1) so the retained
    window is exactly 30.0s (60 timeline frames at 0.5s step).  Durations vary
    100..400 us; ~10% of rows are `system` slices on alternating CPUs.
    """
    rows = []
    for i in range(rows_per_rep):
        ts = round(i * span_us / (rows_per_rep - 1)) if rows_per_rep > 1 else 0
        dur = 100 + (i % 7) * 50
        rows.append(
            slice_row(
                ts,
                dur,
                cpu=i % 2,
                tid=1000 + i % 5,
                pod="stress-ng" if i % 10 else "system",
            )
        )
    return pd.DataFrame(rows, columns=DIST_SLICES_COLUMNS)


def _small_rep1_rows() -> list[tuple]:
    """Replicate 1: 7 stress-ng slices 4.0s..10.0s + 1 system slice at 6.5s."""
    rows = [
        slice_row(4_000_000 + i * 1_000_000, dur)
        for i, dur in enumerate((100, 200, 300, 400, 500, 600, 700))
    ]
    rows.append(slice_row(6_500_000, 250, cpu=1, tid=999, pod="system"))
    return rows


def _small_rep2_rows() -> list[tuple]:
    """Replicate 2: 5 stress-ng slices 1.0s..5.0s (retained 4.0s)."""
    return [
        slice_row(1_000_000 + i * 1_000_000, dur)
        for i, dur in enumerate((150, 250, 350, 450, 550))
    ]


def build_large_family_fixture(
    root: pathlib.Path,
    n_reps: int = 3,
    rows_per_rep: int = LARGE_ROWS,
) -> pathlib.Path:
    """Write a family whose REP_CELL carries n_reps per-replicate slice CSVs,
    each spanning 0..30.0s with rows_per_rep rows (dist-slices-replicate-<n>.csv,
    the canonical per-replicate input).  Dist-slices.csv merged file is NOT written:
    dist-gif must refuse merged-only cells (covered by the existing suite).
    """
    rep_dir = root / "distribution" / FAMILY / REP_CELL
    df = _large_rep_df(rows_per_rep=rows_per_rep)
    rows: list[tuple] = list(df.itertuples(index=False, name=None))
    for n in range(1, n_reps + 1):
        _write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}{n}.csv", rows)
    _write_summary_csv(rep_dir / "dist-summary.csv", _summary_rows())
    return root


def build_small_family_fixture(root: pathlib.Path) -> pathlib.Path:
    """Write the small behavior fixture: REP_CELL with per-replicate-1 (6.0s)
    and per-replicate-2 (4.0s) slice files + a quota summary (fast render)."""
    rep_dir = root / "distribution" / FAMILY / REP_CELL
    _write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}1.csv", _small_rep1_rows())
    _write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}2.csv", _small_rep2_rows())
    _write_summary_csv(rep_dir / "dist-summary.csv", _summary_rows())
    return root


def _sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root*."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root))
            manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _sorted_hist_arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(ts_sorted_us, durations_sorted_us) — the pre-sorted inputs the
    cumulative histogram path consumes (sorted ONCE by the caller)."""
    ts = df["ts_start_us"].to_numpy()
    dur = df["duration_us"].to_numpy()
    order = np.argsort(ts, kind="mergesort")
    return ts[order].astype(float), dur[order].astype(float)


def _fake_timeline_render(slices_df, path, *args, **kwargs) -> None:
    """Render stand-in: writes a minimal GIF89a so the canonical copy works."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GIF89a")


def _fake_hist_render(slices_df, path, *args, **kwargs) -> None:
    """Render stand-in for the histogram path."""
    _fake_timeline_render(slices_df, path, *args, **kwargs)


# =========================================================================
# Module contract (pinned NEW API surface)
# =========================================================================


class TestMemoryModuleContract:
    """The memory helpers exist and the pinned constants are unchanged."""

    def test_memory_helpers_exposed(self):
        """load_replicate_slices / replicate_load_plan / hist_frame_data must
        be callable on the module (RED against the current implementation)."""
        module = load_dist_gif_module()
        for name in (
            "load_replicate_slices",
            "replicate_load_plan",
            "hist_frame_data",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned memory helper: {name}"
            )

    def test_pinned_settings_constants_unchanged(self):
        """The pinned settings (frame formulas) are preserved by the rewrite."""
        module = load_dist_gif_module()
        assert module.STEP_S_DEFAULT == 0.5
        assert module.WINDOW_S_DEFAULT == 2.0
        assert module.TIMELINE_MAX_FRAMES == 120
        assert module.HIST_MAX_FRAMES == 120


# =========================================================================
# Per-replicate load plan (bounded loading)
# =========================================================================


class TestReplicateLoadPlan:
    """replicate_load_plan pairs each per-replicate file with its number in
    NUMERIC order; load_replicate_slices reads one pinned-schema CSV."""

    def test_replicate_load_plan_returns_numeric_ordered_pairs(
        self, tmp_path: pathlib.Path
    ):
        """dist-slices-replicate-<n>.csv pairs ordered numerically by n:
        replicate-10 after replicate-2, not before."""
        module = load_dist_gif_module()
        assert callable(getattr(module, "replicate_load_plan", None))
        cell_dir = tmp_path / "cell"
        for n in (2, 10, 1):
            _write_slices_csv(
                cell_dir / f"{REPLICATE_SLICES_PREFIX}{n}.csv",
                [slice_row(4_000_000, 100)],
            )
        files = sorted(cell_dir.glob(f"{REPLICATE_SLICES_PREFIX}*.csv"))
        plan = module.replicate_load_plan(files)
        assert [n for n, _path in plan] == [1, 2, 10]
        assert [pathlib.Path(p).name for _n, p in plan] == [
            f"{REPLICATE_SLICES_PREFIX}1.csv",
            f"{REPLICATE_SLICES_PREFIX}2.csv",
            f"{REPLICATE_SLICES_PREFIX}10.csv",
        ]

    def test_replicate_load_plan_empty_input(self):
        """An empty file list yields an empty plan (no replicates to load)."""
        module = load_dist_gif_module()
        assert callable(getattr(module, "replicate_load_plan", None))
        assert module.replicate_load_plan([]) == []

    def test_load_replicate_slices_reads_pinned_columns(self, tmp_path: pathlib.Path):
        """load_replicate_slices returns a DataFrame with the pinned
        dist-slices schema for ONE per-replicate file."""
        module = load_dist_gif_module()
        assert callable(getattr(module, "load_replicate_slices", None))
        path = _write_slices_csv(
            tmp_path / f"{REPLICATE_SLICES_PREFIX}1.csv",
            [slice_row(4_000_000, 100), slice_row(5_000_000, 200)],
        )
        df = module.load_replicate_slices(path)
        assert list(df.columns) == DIST_SLICES_COLUMNS
        assert len(df) == 2


# =========================================================================
# Bounded loading — generate_family_gifs never holds all replicate DataFrames
# =========================================================================


class TestBoundedLoading:
    """The core invariant: per-replicate load + validate + render +
    free, so the number of concurrently-live replicate DataFrames is a small
    constant (never n_reps).  Proven with a weakref tracker on the patched
    module-level loader (structural — no RSS, budgets are covered elsewhere)."""

    def test_generate_family_gifs_loads_each_replicate_through_helper(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ):
        """generate_family_gifs must load EVERY replicate through the patched
        module-level loader (one load per per-replicate file) — not
        pd.read_csv directly."""
        module = load_dist_gif_module()
        assert hasattr(module, "load_replicate_slices"), (
            "The rewrite must expose load_replicate_slices and generate_family_gifs "
            "must load each replicate through it"
        )
        n_reps = 3
        root = build_large_family_fixture(
            tmp_path / "fixture", n_reps=n_reps, rows_per_rep=2000
        )

        orig = module.load_replicate_slices
        refs: list[weakref.ReferenceType] = []

        def tracked_load(path):
            df = orig(path)
            refs.append(weakref.ref(df))
            return df

        monkeypatch.setattr(module, "load_replicate_slices", tracked_load)
        # Neutralize rendering: this test pins the LOADING pattern only.
        monkeypatch.setattr(module, "_render_timeline_gif", _fake_timeline_render)
        monkeypatch.setattr(module, "_render_hist_gif", _fake_hist_render)

        module.generate_family_gifs(
            data_dir=root,
            output_dir=tmp_path / "out",
            family=FAMILY,
        )
        assert len(refs) >= n_reps, (
            "generate_family_gifs must load each replicate through "
            f"load_replicate_slices (expected >= {n_reps} loads, got {len(refs)})"
        )

    def test_generate_family_gifs_never_holds_all_replicate_dfs(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ):
        """While 3 replicates exist, the number of concurrently-live replicate
        DataFrames must stay below 3 (per-replicate load+render+free).  The
        current implementation holds all 3 in `reps` -> RED."""
        module = load_dist_gif_module()
        assert hasattr(module, "load_replicate_slices"), (
            "The rewrite must expose load_replicate_slices so the concurrent-live "
            "invariant can be checked"
        )
        n_reps = 3
        root = build_large_family_fixture(
            tmp_path / "fixture", n_reps=n_reps, rows_per_rep=2000
        )

        orig = module.load_replicate_slices
        refs: list[weakref.ReferenceType] = []
        alive_before_each: list[int] = []

        def tracked_load(path):
            df = orig(path)
            alive_before_each.append(sum(1 for r in refs if r() is not None))
            refs.append(weakref.ref(df))
            return df

        monkeypatch.setattr(module, "load_replicate_slices", tracked_load)
        monkeypatch.setattr(module, "_render_timeline_gif", _fake_timeline_render)
        monkeypatch.setattr(module, "_render_hist_gif", _fake_hist_render)

        module.generate_family_gifs(
            data_dir=root,
            output_dir=tmp_path / "out",
            family=FAMILY,
        )

        max_concurrent = max(alive_before_each, default=0) + 1  # + the loaded df
        assert max_concurrent < n_reps, (
            f"all {n_reps} replicate DataFrames were held simultaneously "
            "(max concurrent {max_concurrent}); the rewrite must load + validate + "
            "render + free each replicate one at a time"
        )

        gc.collect()
        alive_at_end = sum(1 for r in refs if r() is not None)
        assert alive_at_end < n_reps, (
            f"{alive_at_end} of {n_reps} replicate DataFrames still referenced "
            "after generate_family_gifs returns"
        )


# =========================================================================
# O(n) cumulative histogram — hist_frame_data (searchsorted slice view)
# =========================================================================


class TestHistogramCumulativePath:
    """hist_frame_data is the per-frame cumulative path: np.searchsorted on a
    PRE-SORTED ts array returns a slice VIEW of the sorted durations — no
    full-array boolean-mask copy per frame (O(log n) time, O(1) memory)."""

    def _module_with_helper(self):
        module = load_dist_gif_module()
        assert hasattr(module, "hist_frame_data"), (
            "The rewrite must expose hist_frame_data(ts_sorted_us, durations_us, end_us)"
        )
        return module

    def test_hist_frame_data_matches_reference_mask(self):
        """Output parity with the old `durations[ts <= end_us]` semantics on a
        20K-row replicate — behavior preservation of the mask semantics."""
        module = self._module_with_helper()
        df = _large_rep_df(rows_per_rep=LARGE_ROWS)
        ts_sorted, dur_sorted = _sorted_hist_arrays(df)
        for end_us in (
            0.0,
            1_000_000.0,
            5_000_000.0,
            15_000_000.0,
            LARGE_SPAN_US - 1.0,
            LARGE_SPAN_US * 1.0,
            99_000_000.0,
        ):
            expected = dur_sorted[ts_sorted <= end_us]
            got = module.hist_frame_data(ts_sorted, dur_sorted, end_us)
            np.testing.assert_array_equal(got, expected)

    def test_hist_frame_data_returns_slice_view_not_copy(self):
        """The cumulative path returns a VIEW of the sorted durations (no
        full-array copy).  A boolean-mask fancy-index always copies; a
        searchsorted slice shares memory with the input."""
        module = self._module_with_helper()
        ts_sorted = np.array([1_000_000.0, 2_000_000.0, 3_000_000.0, 4_000_000.0])
        dur_sorted = np.array([100.0, 200.0, 300.0, 400.0])
        got = module.hist_frame_data(ts_sorted, dur_sorted, 3_500_000.0)
        assert np.shares_memory(got, dur_sorted), (
            "hist_frame_data must return a slice VIEW of durations_sorted_us; "
            "a boolean-mask full-array copy is O(n) per frame (O(n^2) total)"
        )
        np.testing.assert_array_equal(got, np.array([100.0, 200.0, 300.0]))

    def test_hist_frame_data_uses_searchsorted_source(self):
        """The helper body must use np.searchsorted (the cumulative mechanism)."""
        module = self._module_with_helper()
        src = inspect.getsource(module.hist_frame_data)
        assert "searchsorted" in src, (
            "hist_frame_data must use np.searchsorted on the pre-sorted ts "
            "array (cumulative path)"
        )

    def test_hist_frame_data_boundaries_inclusive_at_ts(self):
        """End boundary semantics preserve the old `<=` mask: end exactly AT a
        slice ts INCLUDES it (side='right'); before the first -> empty; past
        the last -> all."""
        module = self._module_with_helper()
        ts_sorted = np.array([1_000_000.0, 2_000_000.0, 3_000_000.0])
        dur_sorted = np.array([10.0, 20.0, 30.0])

        assert module.hist_frame_data(ts_sorted, dur_sorted, 500_000.0).size == 0

        got = module.hist_frame_data(ts_sorted, dur_sorted, 2_000_000.0)
        np.testing.assert_array_equal(got, np.array([10.0, 20.0]))

        got = module.hist_frame_data(ts_sorted, dur_sorted, 9_000_000.0)
        np.testing.assert_array_equal(got, dur_sorted)

    def test_hist_frame_data_empty_arrays(self):
        """Empty pre-sorted arrays -> an empty result (no crash, no frame)."""
        module = self._module_with_helper()
        empty = np.array([], dtype=float)
        got = module.hist_frame_data(empty, empty, 1_000_000.0)
        assert got.size == 0

    def test_hist_frame_data_counts_monotonic_with_end(self):
        """Cumulative counts grow monotonically with end_us — the property
        that makes the histogram converge over the trace window."""
        module = self._module_with_helper()
        df = _large_rep_df(rows_per_rep=5000)
        ts_sorted, dur_sorted = _sorted_hist_arrays(df)
        # Grid starts BEFORE the first ts (0.0) so the empty extreme is exact
        # and ends AT the last ts (30.0s, inclusive via side='right').
        ends = np.arange(-1, 31, dtype=float) * 1_000_000.0
        sizes = [
            module.hist_frame_data(ts_sorted, dur_sorted, end).size for end in ends
        ]
        assert sizes == sorted(sizes)
        assert sizes[0] == 0
        assert sizes[-1] == len(df)


# =========================================================================
# Render path — _render_hist_gif uses the cumulative helper, never a mask
# =========================================================================


class TestRenderCumulativePath:
    """_render_hist_gif's per-frame work must NOT re-mask the full DataFrame
    (source regression guard) and must go through hist_frame_data (spy)."""

    def test_render_hist_gif_source_has_no_per_frame_mask(self):
        """The per-frame boolean-mask DataFrame copy
        (slices_df[slices_df["ts_start_us"] <= end_us]) must be gone; the
        frame window comes from the cumulative path instead."""
        module = load_dist_gif_module()
        src = inspect.getsource(module._render_hist_gif)
        assert "slices_df[slices_df[" not in src, (
            "per-frame boolean-mask DataFrame copy remains in _render_hist_gif "
            "(window_df = slices_df[slices_df[...] <= end_us]) — cumulative "
            "path required"
        )
        assert "<= end_us" not in src, (
            "a full-array mask comparison on ts remains in _render_hist_gif"
        )

    def test_render_hist_gif_calls_cumulative_helper(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The render path invokes hist_frame_data at least once (the per-frame
        cumulative window); it never falls back to masking inside the loop."""
        module = load_dist_gif_module()
        assert hasattr(module, "hist_frame_data"), (
            "The rewrite must expose hist_frame_data so _render_hist_gif can use it"
        )
        df = pd.DataFrame(
            [
                {
                    "ts_start_us": 1_000_000,
                    "ts_end_us": 1_000_100,
                    "duration_us": 100,
                    "cpu": 0,
                    "tid": 1,
                    "thread_name": "t",
                    "pod": "p",
                },
                {
                    "ts_start_us": 2_000_000,
                    "ts_end_us": 2_000_200,
                    "duration_us": 200,
                    "cpu": 0,
                    "tid": 1,
                    "thread_name": "t",
                    "pod": "p",
                },
                {
                    "ts_start_us": 3_000_000,
                    "ts_end_us": 3_000_300,
                    "duration_us": 300,
                    "cpu": 0,
                    "tid": 1,
                    "thread_name": "t",
                    "pod": "p",
                },
                {
                    "ts_start_us": 4_000_000,
                    "ts_end_us": 4_000_400,
                    "duration_us": 400,
                    "cpu": 1,
                    "tid": 2,
                    "thread_name": "system",
                    "pod": "system",
                },
            ]
        )
        retained_s = 3.0  # 4.0s - 1.0s -> 3 hist frames (one per second)
        calls: list[float] = []
        orig = module.hist_frame_data

        def spy(ts_sorted, dur_sorted, end_us):
            calls.append(end_us)
            return orig(ts_sorted, dur_sorted, end_us)

        monkeypatch.setattr(module, "hist_frame_data", spy)
        module._render_hist_gif(
            df,
            tmp_path / "hist.gif",
            FAMILY,
            REP_CELL,
            retained_s,
            fps=4,
            max_frames=120,
        )
        assert len(calls) >= 1, (
            "_render_hist_gif must build each frame window through "
            "hist_frame_data (cumulative path), not a boolean-mask copy"
        )


# =========================================================================
# Behavior preservation (the rewrite must not regress the pinned contract)
# =========================================================================


class TestBehaviorPreserved:
    """Frame formulas, window-only bars, canonical alias, determinism, Pillow
    guard and annotation format are pinned here — they stay green in the RED
    phase (current implementation already satisfies them) and prove the rewrite
    rewrite preserved behavior."""

    def test_frame_formulas_unchanged_pinned(self):
        """min(floor(retained_us / 500000), 120) and min(int(retained_s), 120)."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(82.0) == 120  # spec: capped
        assert module.timeline_frame_count(6.0) == 12  # small rep1
        assert module.timeline_frame_count(59.5) == 119  # cap boundary
        assert module.hist_frame_count(82.0) == 82  # spec: one per second
        assert module.hist_frame_count(6.0) == 6  # small rep1
        assert module.hist_frame_count(0.0) == 0

    def test_window_slice_count_preserved_on_large_fixture(self):
        """Window-only bars: per-frame bar counts scale with the 2s window
        density, never with the full 20K-row slice count."""
        module = load_dist_gif_module()
        assert callable(getattr(module, "window_slice_count", None))
        df = _large_rep_df(rows_per_rep=LARGE_ROWS)
        n_frames = module.timeline_frame_count(module.retained_window_s(df))
        assert n_frames == LARGE_TIMELINE_FRAMES
        per_frame = [module.window_slice_count(df, i) for i in range(n_frames)]
        assert max(per_frame) > 0
        assert max(per_frame) < len(df), (
            "a frame must never draw the full slice set (window-only bars)"
        )
        assert max(per_frame) <= LARGE_WINDOW_EXPECTED + 1  # density x 2s

    def test_canonical_exec_timeline_identical_to_replicate_1(
        self, tmp_path: pathlib.Path
    ):
        """exec-timeline.gif is replicate 1's GIF bytes (canonical name)."""
        module = load_dist_gif_module()
        root = build_small_family_fixture(tmp_path / "fixture")
        out = tmp_path / "out"
        result = module.generate_family_gifs(
            data_dir=root, output_dir=out, family=FAMILY
        )
        canonical = pathlib.Path(result[TIMELINE_GIF])
        visuals = canonical.parent
        rep1 = visuals / REPLICATE_TIMELINE_TMPL.format(n=1)
        assert canonical.is_file()
        assert rep1.is_file()
        assert canonical.read_bytes() == rep1.read_bytes()

    def test_two_runs_deterministic_manifest(self, tmp_path: pathlib.Path):
        """Determinism: two runs on the same staged data
        produce byte-identical output trees (no wall-clock values)."""
        module = load_dist_gif_module()
        root = build_small_family_fixture(tmp_path / "fixture")
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        module.generate_family_gifs(data_dir=root, output_dir=out1, family=FAMILY)
        module.generate_family_gifs(data_dir=root, output_dir=out2, family=FAMILY)
        visuals1 = out1 / "distribution" / FAMILY / "visuals"
        visuals2 = out2 / "distribution" / FAMILY / "visuals"
        m1 = _sha256_manifest(visuals1)
        m2 = _sha256_manifest(visuals2)
        assert m1 == m2
        assert set(m1.keys()) == {
            TIMELINE_GIF,
            REPLICATE_TIMELINE_TMPL.format(n=1),
            REPLICATE_TIMELINE_TMPL.format(n=2),
            HIST_GIF,
        }

    def test_pillow_guard_preserved(self, tmp_path: pathlib.Path, monkeypatch):
        """Without Pillow the render fails loudly naming Pillow."""
        module = load_dist_gif_module()
        root = build_small_family_fixture(tmp_path / "fixture")
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        with pytest.raises(Exception) as exc:
            module.generate_family_gifs(
                data_dir=root,
                output_dir=tmp_path / "out",
                family=FAMILY,
            )
        assert "pillow" in str(exc.value).lower()

    def test_annotation_format_pinned(self):
        """Per-frame annotation stays `family | cell | elapsed N.Ns`."""
        module = load_dist_gif_module()
        assert module.annotation_text(FAMILY, REP_CELL, 4.0) == (
            "dist-stress-ng | request=100m-limit=100m | elapsed 4.0s"
        )
