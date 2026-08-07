#!/usr/bin/env python3
"""dist-gif.py — animated visualization of the CPU execution-time distribution.

Renders per-family animated GIFs from dist-analyze output:

    <output-dir>/distribution/<family>/visuals/exec-timeline-replicate-<n>.gif
        Animated per-CPU Gantt per REPLICATE: X = time, Y = CPU lanes,
        colored slice bars per pod, a moving ``window_s``-wide window stepping
        ``step_s`` every frame.  Frame count =
        min(floor(retained_window_s / step_s), TIMELINE_MAX_FRAMES), rendered
        at ``fps_timeline``.  Each frame draws ONLY the slice bars whose start
        falls in the moving window (never all slices).  Quota cells (cpu_max >
        0 in dist-summary.csv) draw a hatched throttle-gap background.
    <output-dir>/distribution/<family>/visuals/exec-timeline.gif
        Canonical alias: byte-identical to replicate 1's GIF.
    <output-dir>/distribution/<family>/visuals/slice-dist-build.gif
        Animated cumulative histogram of slice durations built from the
        representative replicate (replicate 1 when present): one frame per
        second of retained trace (frame t = histogram over [guard_start, t]),
        capped at HIST_MAX_FRAMES; rendered at ``fps_hist``.

The pinned analysis contract covers the GIFs, frame formulas, per-frame
data-derived annotation, deterministic byte-for-byte reruns on staged data,
and the Pillow dependency (a clear error naming Pillow when unavailable — no
silent PNG fallback).

Memory contract: per-replicate slice
DataFrames are loaded + validated + rendered + freed ONE AT A TIME through
the module-level ``load_replicate_slices`` helper (never all held in a
list), and the histogram is O(n + frames) — ``hist_frame_data`` slices the
pre-sorted durations via ``np.searchsorted`` instead of a per-frame
boolean-mask DataFrame copy.

Input contract (dist-analyze output tree):
    <data-dir>/distribution/<family>/<cell>/dist-slices-replicate-<n>.csv
        PER-REPLICATE slice files (required; each replicate is its own
        retained window).  Uses the pinned dist-slices.csv schema
        (SLICES_COLUMNS: ts_start_us,ts_end_us,duration_us,cpu,tid,
        thread_name,pod).
    <data-dir>/distribution/<family>/<cell>/dist-slices.csv
        Merged file (emitted by dist-analyze) — IGNORED by dist-gif.  A cell
        with only the merged file cannot render per-replicate and FAILS
        LOUDLY.
    <data-dir>/distribution/<family>/<cell>/dist-summary.csv
        cpu_max > 0 -> quota cell (hatched throttle gaps).

All per-frame elapsed-time annotations are derived exclusively from slice
timestamps — never wall-clock.  One representative cell drives the
GIFs (default = first cell dir in sorted order, ``--cell`` overrides).

Usage:
    dist-gif.py --data-dir <root> --output-dir <root> --family <name>
                [--cell <name>] [--window-s 2.0] [--step-s 0.5]
                [--fps-timeline 10] [--fps-hist 4]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# ---------------------------------------------------------------------------
# Pinned constants (pinned contract section 4.1)
# ---------------------------------------------------------------------------

FPS_TIMELINE_DEFAULT = 10  # exec-timeline.gif playback fps
FPS_HIST_DEFAULT = 4  # slice-dist-build.gif playback fps
WINDOW_S_DEFAULT = 2.0  # exec-timeline moving window width (seconds)
STEP_S_DEFAULT = 0.5  # exec-timeline window step (seconds) — CHANGED from 0.2
TIMELINE_MAX_FRAMES = 120  # hard cap for exec-timeline.gif (NEW)
HIST_MAX_FRAMES = 120  # hard cap for slice-dist-build.gif (NEW)
TIMELINE_GIF = "exec-timeline.gif"  # canonical replicate-1 alias
HIST_GIF = "slice-dist-build.gif"
REPLICATE_SLICES_PREFIX = "dist-slices-replicate-"  # per-replicate input files
REPLICATE_TIMELINE_TMPL = "exec-timeline-replicate-{n}.gif"

# dist-slices.csv schema (SLICES_COLUMNS)
SLICES_COLUMNS = [
    "ts_start_us",
    "ts_end_us",
    "duration_us",
    "cpu",
    "tid",
    "thread_name",
    "pod",
]


# ---------------------------------------------------------------------------
# Pure core functions (frame formulas + annotation, data-derived)
# ---------------------------------------------------------------------------


def retained_window_s(slices_df: pd.DataFrame) -> float:
    """Return the retained window in seconds: (max - min ts_start_us) / 1e6.

    An empty DataFrame yields 0.0 (the caller must refuse to render).
    """
    if slices_df.empty:
        return 0.0
    ts = slices_df["ts_start_us"].astype(float)
    return float(ts.max() - ts.min()) / 1e6


def timeline_frame_count(
    retained_s: float,
    *,
    step_s: float = STEP_S_DEFAULT,
    max_frames: int = TIMELINE_MAX_FRAMES,
) -> int:
    """Number of exec-timeline frames: min(floor(retained_us / step_us), cap).

    Uses integer microseconds (``int(retained_s * 1e6) //
    int(step_s * 1e6)``) so float step artifacts cannot drop a frame, then
    hard-caps at ``max_frames`` (default 120).  0.0 -> 0; a trailing partial
    step is NOT a frame.  Frame i's window starts at
    ``min_ts_start + i * step_s``.
    """
    return min(int(retained_s * 1_000_000) // int(step_s * 1_000_000), max_frames)


def hist_frame_count(
    retained_s: float,
    *,
    max_frames: int = HIST_MAX_FRAMES,
) -> int:
    """Number of slice-dist-build frames: min(int(retained_s), max_frames).

    One frame per second of retained trace, hard-capped at ``max_frames``
    (default 120).  0.0 -> 0.
    """
    return min(int(retained_s), max_frames)


def elapsed_for_frame(
    slices_df: pd.DataFrame,
    frame_index: int,
    step_s: float = STEP_S_DEFAULT,
) -> float:
    """Per-frame elapsed-time annotation: min(ts_start_us)/1e6 + i * step_s.

    Derived ONLY from slice data — never wall-clock.  An empty
    DataFrame yields 0.0.
    """
    if slices_df.empty:
        return 0.0
    min_ts_s = float(slices_df["ts_start_us"].min()) / 1e6
    return min_ts_s + frame_index * step_s


def annotation_text(family: str, cell: str, elapsed_s: float) -> str:
    """Pinned per-frame annotation: config label | cell | elapsed time."""
    return f"{family} | {cell} | elapsed {elapsed_s:.1f}s"


def representative_cell(family_dir: Path, cell: str | None = None) -> str:
    """Pick the cell that drives both GIFs.

    ``cell`` wins when given; otherwise the first cell dir in sorted order.
    Raises ValueError when no cell dirs exist.
    """
    if cell is not None:
        return cell
    if not family_dir.is_dir():
        raise ValueError(f"no cell directories: {family_dir} is not a directory")
    cell_dirs = sorted(p.name for p in family_dir.iterdir() if p.is_dir())
    if not cell_dirs:
        raise ValueError(f"no cell directories under {family_dir}")
    return cell_dirs[0]


def quota_cells(family_dir: Path) -> set[str]:
    """Cells whose dist-summary.csv has any row with cpu_max > 0 (hatching).

    A cell without a readable summary CSV is not in the set (best effort).
    """
    quota: set[str] = set()
    if not family_dir.is_dir():
        return quota
    for cell_dir in sorted(p for p in family_dir.iterdir() if p.is_dir()):
        summary_path = cell_dir / "dist-summary.csv"
        if not summary_path.is_file():
            continue
        try:
            summary_df = pd.read_csv(summary_path)
        except (OSError, ValueError):
            continue
        if summary_df.empty or "cpu_max" not in summary_df.columns:
            continue
        if (summary_df["cpu_max"].astype(float) > 0).any():
            quota.add(cell_dir.name)
    return quota


def replicate_slice_files(cell_dir: Path) -> list[Path]:
    """Discover per-replicate slice CSVs ``dist-slices-replicate-<n>.csv``.

    Returns the files sorted NUMERICALLY by ``n`` (replicate-10 sorts after
    replicate-2, not before).  A cell without any per-replicate files yields
    an empty list — the merged ``dist-slices.csv`` is never a valid timeline
    input.
    """
    if not cell_dir.is_dir():
        return []
    numbered: list[tuple[int, Path]] = []
    for path in cell_dir.glob(f"{REPLICATE_SLICES_PREFIX}*.csv"):
        stem = path.name[len(REPLICATE_SLICES_PREFIX) : -len(".csv")]
        try:
            numbered.append((int(stem), path))
        except ValueError:
            # A stray non-numeric suffix cannot be a replicate file; skip it
            # deterministically instead of failing the whole render.
            continue
    numbered.sort(key=lambda pair: pair[0])
    return [path for _n, path in numbered]


def replicate_load_plan(rep_files: list[Path]) -> list[tuple[int, Path]]:
    """Return ``[(replicate_number, path), ...]`` ordered NUMERICALLY by n.

    Companion of ``replicate_slice_files`` discovery, kept for the
    per-replicate bounded-loading contract:
    ``generate_family_gifs`` iterates this plan to load + validate + render +
    free one replicate DataFrame at a time.  Non-numeric suffix files are
    skipped deterministically; an empty input yields an empty plan.
    """
    numbered: list[tuple[int, Path]] = []
    for path in rep_files:
        try:
            numbered.append((_replicate_number(path), path))
        except ValueError:
            continue
    numbered.sort(key=lambda pair: pair[0])
    return numbered


def load_replicate_slices(path: Path) -> pd.DataFrame:
    """Load ONE per-replicate slice CSV (pinned dist-slices schema).

    This is the module-level loader ``generate_family_gifs`` MUST call per
    replicate — tests monkeypatch it (weakref tracker) to prove bounded
    loading.  The two string columns (``thread_name``, ``pod``) are read as
    pandas ``category`` so a 10M-row replicate does not materialize ~10M
    Python string objects (memory contract: a single replicate DataFrame
    stays ~0.4 GiB instead of ~1.6 GiB).  Columns, row order and values are
    the pinned dist-slices schema; rendering treats category values exactly
    like object strings.
    """
    return pd.read_csv(
        path,
        dtype={"thread_name": "category", "pod": "category"},
    )


def window_slice_count(
    slices_df: pd.DataFrame,
    frame_index: int,
    *,
    window_s: float = WINDOW_S_DEFAULT,
    step_s: float = STEP_S_DEFAULT,
) -> int:
    """Number of slice bars whose start falls in the moving window.

    Frame ``frame_index``'s window is ``[win_start, win_start + window_s)``
    with ``win_start = min(ts_start_us)/1e6 + frame_index * step_s``; the
    comparison is done in integer microseconds so the drawn bars always match
    the window the frame shows.  An empty DataFrame yields 0; a frame whose
    window lies past the data end naturally counts 0.
    """
    if slices_df.empty:
        return 0
    min_ts_us = int(float(slices_df["ts_start_us"].min()))
    win_start_us = min_ts_us + int(frame_index * step_s * 1_000_000)
    win_end_us = win_start_us + int(window_s * 1_000_000)
    starts_us = slices_df["ts_start_us"].to_numpy()
    return int(((starts_us >= win_start_us) & (starts_us < win_end_us)).sum())


def hist_frame_data(
    ts_sorted_us: np.ndarray,
    durations_sorted_us: np.ndarray,
    end_us: float,
) -> np.ndarray:
    """Return durations of slices with ts_start_us <= end_us.

    Cumulative path: ``ts_sorted_us`` is PRE-SORTED once by
    the caller; the boundary is ``np.searchsorted(ts_sorted_us, end_us,
    side='right')``; the result is a slice VIEW of ``durations_sorted_us`` —
    never a boolean-mask copy (O(log n) per frame, O(1) extra memory per
    frame).  Empty input -> empty.  Boundary is INCLUSIVE at a ts exactly
    equal to ``end_us`` (preserves the old ``<=`` mask semantics).
    """
    idx = np.searchsorted(ts_sorted_us, end_us, side="right")
    return durations_sorted_us[:idx]


# ---------------------------------------------------------------------------
# Pillow guard
# ---------------------------------------------------------------------------


def require_pillow() -> None:
    """Verify Pillow is importable; raise RuntimeError naming it otherwise.

    Lazy import so the failure is a clear runtime error — no silent
    PNG fallback.  matplotlib pulls PIL transitively, so this is the pinned
    guard that produces the user-facing message.
    """
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required to render the distribution GIFs; install it "
            "with 'pip install Pillow>=10.0' (no silent PNG "
            "fallback)"
        ) from exc


# ---------------------------------------------------------------------------
# Render layer (matplotlib + PillowWriter)
# ---------------------------------------------------------------------------


def _render_timeline_gif(
    slices_df: pd.DataFrame,
    path: Path,
    family: str,
    cell: str,
    retained_s: float,
    *,
    window_s: float,
    step_s: float,
    fps: int,
    quota: bool,
    max_frames: int,
) -> None:
    """Render one replicate's exec-timeline GIF (per-CPU Gantt, moving window).

    Frame i shows the ``window_s``-wide viewport starting at
    ``min_ts_s + i * step_s``.  Each frame draws ONLY the slice bars whose
    START falls in the viewport (``window_slice_count`` semantics — never all
    slices), colored by pod, on their CPU lane.  Quota cells overlay a
    hatched throttle-gap background.  Elapsed annotation comes from
    ``elapsed_for_frame`` (data-derived).
    """
    from matplotlib.animation import PillowWriter

    n_frames = timeline_frame_count(retained_s, step_s=step_s, max_frames=max_frames)
    min_ts_us = int(float(slices_df["ts_start_us"].min()))
    min_ts_s = min_ts_us / 1e6
    cpus = sorted(int(c) for c in slices_df["cpu"].unique())
    cpu_y = {cpu: i for i, cpu in enumerate(cpus)}
    pods = sorted(slices_df["pod"].unique())
    colormap = matplotlib.colormaps["tab10"]
    pod_colors = {pod: colormap(i % 10) for i, pod in enumerate(pods)}

    start_us = slices_df["ts_start_us"].to_numpy()
    end_us = slices_df["ts_end_us"].to_numpy()
    cpu_arr = slices_df["cpu"].to_numpy()
    pod_arr = slices_df["pod"].to_numpy()

    fig, ax = plt.subplots(figsize=(12, 3))
    writer = PillowWriter(fps=fps)
    try:
        with writer.saving(fig, str(path), 100):
            for frame in range(n_frames):
                win_start_s = min_ts_s + frame * step_s
                win_end_s = win_start_s + window_s
                win_start_us = min_ts_us + int(frame * step_s * 1_000_000)
                win_end_us = win_start_us + int(window_s * 1_000_000)

                ax.clear()
                ax.set_xlim(win_start_s, win_end_s)
                ax.set_ylim(-0.6, len(cpus) - 0.4)
                ax.set_yticks(list(cpu_y.values()))
                ax.set_yticklabels([f"cpu {c}" for c in cpus])
                ax.set_xlabel("elapsed time (s)")
                ax.set_ylabel("CPU")
                ax.grid(True, axis="x", alpha=0.3)

                if quota:
                    ax.axvspan(
                        win_start_s,
                        win_end_s,
                        facecolor="0.85",
                        hatch="//",
                        edgecolor="0.5",
                        linewidth=0.5,
                        alpha=0.6,
                        zorder=0,
                    )

                # Window-only bar rendering: bars whose START falls in
                # [win_start, win_end) — the per-frame optimization that keeps
                # each frame cheap regardless of the total slice count.
                in_window = (start_us >= win_start_us) & (start_us < win_end_us)
                if in_window.any():
                    sel_start = start_us[in_window].astype(float)
                    sel_end = end_us[in_window].astype(float)
                    ys = [cpu_y[int(c)] for c in cpu_arr[in_window]]
                    lefts = sel_start / 1e6
                    widths = (sel_end - sel_start) / 1e6
                    bar_colors = [pod_colors[p] for p in pod_arr[in_window]]
                    ax.barh(
                        ys,
                        widths,
                        left=lefts,
                        height=0.6,
                        color=bar_colors,
                        edgecolor="none",
                        zorder=2,
                    )

                ax.set_title(
                    annotation_text(
                        family, cell, elapsed_for_frame(slices_df, frame, step_s)
                    )
                )
                writer.grab_frame()
    finally:
        plt.close(fig)


def _render_hist_gif(
    slices_df: pd.DataFrame,
    path: Path,
    family: str,
    cell: str,
    retained_s: float,
    *,
    fps: int,
    max_frames: int,
) -> None:
    """Render slice-dist-build.gif: cumulative histogram converging over time.

    One frame per second of trace, capped at ``max_frames``: frame t (1-based)
    is the histogram of slice durations with ts_start_us <= guard_start + t
    seconds.  Bin edges are fixed from the full retained window so the x-axis
    stays stable and the distribution visibly converges.  Elapsed annotation
    is data-derived.

    Memory contract: the ts-sorted + duration-sorted arrays are
    precomputed ONCE before the frame loop; each frame's window comes from
    ``hist_frame_data`` (``np.searchsorted`` slice view) — O(n + frames), no
    per-frame boolean-mask DataFrame copy.
    """
    from matplotlib.animation import PillowWriter

    n_frames = hist_frame_count(retained_s, max_frames=max_frames)
    min_ts_us = float(slices_df["ts_start_us"].min())
    min_ts_s = min_ts_us / 1e6
    durations = slices_df["duration_us"].astype(float)
    dur_min = float(durations.min())
    dur_max = float(durations.max())
    if dur_max <= dur_min:
        bins = np.linspace(dur_min - 1.0, dur_max + 1.0, 21).tolist()
    else:
        bins = np.linspace(dur_min, dur_max, 21).tolist()

    # Cumulative path inputs: sort the ts column ONCE (stable mergesort keeps
    # rerun determinism); the sorted durations are sliced per frame.
    ts = slices_df["ts_start_us"].to_numpy()
    dur = durations.to_numpy()
    order = np.argsort(ts, kind="mergesort")
    ts_sorted = ts[order].astype(float)
    dur_sorted = dur[order].astype(float)

    fig, ax = plt.subplots(figsize=(12, 4))
    writer = PillowWriter(fps=fps)
    try:
        with writer.saving(fig, str(path), 100):
            for frame in range(n_frames):
                end_us = min_ts_us + (frame + 1) * 1_000_000
                window_durs = hist_frame_data(ts_sorted, dur_sorted, end_us)

                ax.clear()
                ax.hist(
                    window_durs,
                    bins=bins,
                    color="steelblue",
                    edgecolor="black",
                    alpha=0.8,
                )
                ax.set_xlim(dur_min, dur_max)
                ax.set_xlabel("slice duration (us)")
                ax.set_ylabel("slice count")
                ax.grid(True, axis="y", alpha=0.3)
                elapsed_s = min_ts_s + (frame + 1)
                ax.set_title(annotation_text(family, cell, elapsed_s))
                writer.grab_frame()
    finally:
        plt.close(fig)


def _replicate_number(slices_path: Path) -> int:
    """Parse ``n`` from a ``dist-slices-replicate-<n>.csv`` path."""
    name = slices_path.name
    return int(name[len(REPLICATE_SLICES_PREFIX) : -len(".csv")])


def generate_family_gifs(
    data_dir: Path,
    output_dir: Path,
    family: str,
    *,
    cell: str | None = None,
    window_s: float = WINDOW_S_DEFAULT,
    step_s: float = STEP_S_DEFAULT,
    fps_timeline: int = FPS_TIMELINE_DEFAULT,
    fps_hist: int = FPS_HIST_DEFAULT,
) -> dict[str, Path]:
    """Render the family GIFs and return {TIMELINE_GIF: path, HIST_GIF: path}.

    Renders one ``exec-timeline-replicate-<n>.gif`` per per-replicate slice
    file in the representative cell (each replicate its own retained window),
    writes the canonical ``exec-timeline.gif`` as replicate 1's GIF
    (byte-identical; lowest-numbered replicate when replicate 1 is absent),
    and renders ``slice-dist-build.gif`` from the representative replicate's
    slices.

    Replicates are processed ONE AT A TIME (load -> validate -> render
    timeline -> free) so the number of concurrently-live replicate DataFrames
    stays a small constant (< the number of replicates); every replicate
    DataFrame is freed after its own timeline render, and the canonical
    replicate's slices are reloaded for the histogram render (one DataFrame
    live at a time).  Each replicate is loaded through the module-level
    ``load_replicate_slices`` helper (patchable; bounded-loading contract).

    Raises with a clear message when Pillow is unavailable, when the
    cell has NO per-replicate slice files (a merged-only cell cannot render
    per-replicate and is refused loudly before any output), when a replicate
    has no animatable data (retained_window_s == 0 — a zero-frame GIF is
    un-openable and must never be written), or when a tuning flag is invalid.
    """
    require_pillow()

    if fps_timeline <= 0:
        raise ValueError(f"fps_timeline must be > 0, got {fps_timeline}")
    if fps_hist <= 0:
        raise ValueError(f"fps_hist must be > 0, got {fps_hist}")
    if step_s <= 0:
        raise ValueError(f"step_s must be > 0, got {step_s}")
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0, got {window_s}")

    family_dir = Path(data_dir) / "distribution" / family
    rep_cell = representative_cell(family_dir, cell)
    cell_dir = family_dir / rep_cell

    rep_files = replicate_slice_files(cell_dir)
    if not rep_files:
        raise ValueError(
            f"cell {rep_cell} has no per-replicate slice files "
            f"({REPLICATE_SLICES_PREFIX}<n>.csv) under {cell_dir}; the merged "
            "dist-slices.csv is NOT a valid timeline input — per-replicate "
            "files are required"
        )

    # Validate + render each replicate ONE AT A TIME (load -> validate ->
    # render timeline -> free) so the number of concurrently-live replicate
    # DataFrames stays a small constant (< number of replicates).  Every
    # replicate DataFrame is freed after its own timeline render; the
    # canonical replicate's slices are reloaded for the histogram render
    # (one DataFrame live at a time) and freed when done.
    plan = replicate_load_plan(rep_files)
    plan_ns = [n for n, _path in plan]
    canonical_n = 1 if 1 in plan_ns else min(plan_ns)
    canonical_path = next(path for n, path in plan if n == canonical_n)

    visuals = Path(output_dir) / "distribution" / family / "visuals"
    visuals.mkdir(parents=True, exist_ok=True)

    quota = rep_cell in quota_cells(family_dir)

    replicate_paths: dict[int, Path] = {}
    for rep_n, slices_path in plan:
        slices_df = load_replicate_slices(slices_path)
        retained = retained_window_s(slices_df)
        if retained <= 0:
            raise ValueError(
                f"cell {rep_cell} replicate {rep_n} has no animatable data: "
                "retained window is 0 (no slices) — refusing to write a "
                "zero-frame GIF"
            )
        n_timeline = timeline_frame_count(retained, step_s=step_s)
        n_hist = hist_frame_count(retained)
        if n_timeline <= 0 or n_hist <= 0:
            raise ValueError(
                f"cell {rep_cell} replicate {rep_n} has nothing to animate: "
                f"timeline frames={n_timeline}, hist frames={n_hist}"
            )
        timeline_path = visuals / REPLICATE_TIMELINE_TMPL.format(n=rep_n)
        _render_timeline_gif(
            slices_df,
            timeline_path,
            family,
            rep_cell,
            retained,
            window_s=window_s,
            step_s=step_s,
            fps=fps_timeline,
            quota=quota,
            max_frames=TIMELINE_MAX_FRAMES,
        )
        replicate_paths[rep_n] = timeline_path
        del slices_df

    # Canonical exec-timeline.gif is replicate 1's GIF (byte-identical);
    # fall back to the lowest-numbered replicate when replicate 1 is absent.
    timeline_path = visuals / TIMELINE_GIF
    timeline_path.write_bytes(replicate_paths[canonical_n].read_bytes())

    # The histogram is built from the representative replicate's slices,
    # reloaded one-at-a-time (bounded: exactly one DataFrame live).
    hist_df = load_replicate_slices(canonical_path)
    hist_retained = retained_window_s(hist_df)
    hist_path = visuals / HIST_GIF
    _render_hist_gif(
        hist_df,
        hist_path,
        family,
        rep_cell,
        hist_retained,
        fps=fps_hist,
        max_frames=HIST_MAX_FRAMES,
    )
    del hist_df
    return {TIMELINE_GIF: timeline_path, HIST_GIF: hist_path}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Render per-family animated GIFs (exec-timeline GIFs per "
            "replicate + canonical exec-timeline.gif + slice-dist-build.gif) "
            "from dist-analyze output."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        metavar="DIR",
        help="dist-analyze output root (holds distribution/<family>/<cell>)",
    )
    parser.add_argument(
        "--output-dir", required=True, metavar="DIR", help="output root dir"
    )
    parser.add_argument(
        "--family", required=True, metavar="NAME", help="family name (output subdir)"
    )
    parser.add_argument(
        "--cell",
        default=None,
        metavar="NAME",
        help="representative cell (default: first cell dir in sorted order)",
    )
    parser.add_argument(
        "--window-s",
        type=float,
        default=WINDOW_S_DEFAULT,
        metavar="SEC",
        help="exec-timeline moving window width in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--step-s",
        type=float,
        default=STEP_S_DEFAULT,
        metavar="SEC",
        help="exec-timeline window step in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--fps-timeline",
        type=int,
        default=FPS_TIMELINE_DEFAULT,
        metavar="FPS",
        help="exec-timeline.gif playback fps (default: 10)",
    )
    parser.add_argument(
        "--fps-hist",
        type=int,
        default=FPS_HIST_DEFAULT,
        metavar="FPS",
        help="slice-dist-build.gif playback fps (default: 4)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Render the family's GIFs; return 0 on success, non-zero on failure.

    Errors (missing family, no cells, no per-replicate slices, empty slices,
    Pillow unavailable, invalid flags) are printed to stderr.  A zero-frame
    GIF is never written.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.window_s <= 0:
        parser.error(f"--window-s must be > 0, got {args.window_s}")
    if args.step_s <= 0:
        parser.error(f"--step-s must be > 0, got {args.step_s}")
    if args.fps_timeline <= 0:
        parser.error(f"--fps-timeline must be > 0, got {args.fps_timeline}")
    if args.fps_hist <= 0:
        parser.error(f"--fps-hist must be > 0, got {args.fps_hist}")

    data_dir = Path(args.data_dir)
    family_dir = data_dir / "distribution" / args.family
    if not family_dir.is_dir():
        parser.error(
            f"family dir missing: {family_dir} (checked under data-dir={args.data_dir})"
        )

    try:
        generate_family_gifs(
            data_dir=data_dir,
            output_dir=Path(args.output_dir),
            family=args.family,
            cell=args.cell,
            window_s=args.window_s,
            step_s=args.step_s,
            fps_timeline=args.fps_timeline,
            fps_hist=args.fps_hist,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
