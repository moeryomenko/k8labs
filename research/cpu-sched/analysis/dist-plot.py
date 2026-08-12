#!/usr/bin/env python3
"""dist-plot.py — static EEVDF CPU execution-time distribution images.

Renders the static distribution images from dist-analyze OUTPUT (no
traces, no cluster, no network): per-cell slice-duration histograms and the
four per-family comparison images.  The pinned analysis contract covers
the histogram/comparison/ECDF/Gantt/trajectory images, deterministic
byte-identical reruns, degraded cells excluded from family comparison
images, and the output layout.

Input layout (dist-analyze output, per cell):
    <data-dir>/distribution/<family>/<cell>/
        dist-slices.csv        ts_start_us,ts_end_us,duration_us,cpu,tid,
                               thread_name,pod
        dist-runtime.csv       ts,cpu,pid,tid,thread_name,pod,runtime_ns
                               (runtime_ns is the per-switch DELTA; the
                               trajectory cumsums it)
        dist-summary.csv       cell,replicate,pod,slice_count,total_exec_ms,
                               mean_us,median_us,p50_us,p95_us,p99_us,max_us,
                               throttle_ratio,cpu_weight,cpu_max,quality
        dist-percentiles.json  {replicate: {pod: {p<k>: value}}}

Output layout:
    <output-dir>/distribution/<family>/<cell>/slice-histogram.png  (every cell)
    <output-dir>/distribution/<family>/slice-dist-comparison.png
    <output-dir>/distribution/<family>/slice-ecdf-overlay.png
    <output-dir>/distribution/<family>/gantt-timeline.png
    <output-dir>/distribution/<family>/runtime-trajectory.png
    (the four family images use good cells only; the Gantt uses the
    representative cell — the good cell with the highest throttle_ratio)

Usage:
    dist-plot.py --data-dir <analysis root> --output-dir <out root>
                 --family <name> --cells <c1,c2,...>

Errors are loud: a missing data dir, a listed cell with missing dist-analyze
output or zero slice rows, or a family with no good cells exits non-zero
naming the cause — never a silent partial render.  No wall-clock values
appear in any output; two runs on the same staged data are
byte-identical (SHA-256).

Memory contract: the CLI loads one cell at a time through
compact numpy arrays (``load_cells_compact``) instead of materializing every
slice row as a Python dict, and the Gantt renders through ``load_slices_arrays``
with a single vectorized ``ax.barh`` call capped at ``GANTT_BAR_BUDGET`` bars
(deterministic time-bucket downsampling on large cells).  The legacy
``load_family_data`` return shape (``slices: list[dict]``, ``durations_us:
list[float]``, ``runtime_rows: list[dict]``) is preserved for the dist-plot
tests; the array-backed path is additive.  The compact path is extended:
``pod`` string columns are read as pandas ``category`` (shared labels, no
per-row Python strings) and the ECDF / runtime-trajectory family images plot
at bounded resolution (``ECDF_PLOT_POINTS`` / ``TRAJECTORY_PLOT_POINTS``)
so matplotlib never expands markers or vertex buffers for millions of rows.
"""

from __future__ import annotations

import argparse
import json
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

CELL_HISTOGRAM = "slice-histogram.png"
FAMILY_IMAGES = (
    "slice-dist-comparison.png",
    "slice-ecdf-overlay.png",
    "gantt-timeline.png",
    "runtime-trajectory.png",
)

# Fixed cap on the number of Gantt bar artists for ANY slice dataset:
# large cells are downsampled to this budget instead of drawing one artist
# per slice.  The throttle-hatch axhspan is a separate
# +1 patch allowance on the same axes.
GANTT_BAR_BUDGET = 500

# Deterministic plot-resolution caps for the full-resolution family images:
# the ECDF overlay and the runtime trajectory each plot
# a bounded number of points per series so matplotlib never expands markers
# or vertex buffers for millions of data points (api-server cells carry
# ~3.3-3.6M rows).  A series with at most this many points renders EXACTLY
# (the cap is not a target); larger series are sampled at evenly spaced
# ranks, which preserves the curve shape and the sampled ECDF values.
ECDF_PLOT_POINTS = 2000
TRAJECTORY_PLOT_POINTS = 2000

# ---------------------------------------------------------------------------
# Pure core — data ingestion
# ---------------------------------------------------------------------------


def _first_percentile_table(path: Path) -> dict[str, float]:
    """Flatten dist-percentiles.json to the FIRST replicate's FIRST pod table.

    ``dist-percentiles.json`` is ``{replicate: {pod: {p<k>: value}}}``; the
    contract reads the first replicate's first pod table (replicates and pods
    visited in sorted order for determinism).  Percentile overlays use the
    dist-summary stats, so this table is informational for dist-plot.
    """
    raw = json.loads(path.read_text())
    if not raw:
        return {}
    first_rep = sorted(raw)[0]
    pods = raw[first_rep]
    if not pods:
        return {}
    return dict(pods[sorted(pods)[0]])


def load_family_data(
    analysis_root: Path | str, family: str, cells: list[str]
) -> dict[str, dict]:
    """Read dist-analyze OUTPUT for every listed cell.

    Returns ``{cell: {"cell": str, "summary": dict (FIRST dist-summary row),
    "quality": str ("degraded" iff ANY summary row is degraded), "durations_us":
    list[float], "slices": list[dict] (dist-slices.csv rows incl. cpu/pod for
    the Gantt), "runtime_rows": list[dict], "percentiles": dict (first
    replicate, first pod table)}}``.

    A listed cell with any missing file, or a dist-slices.csv with zero rows,
    raises an error whose message names the cell — never a silent partial
    render.
    """
    root = Path(analysis_root)
    data: dict[str, dict] = {}
    for cell in cells:
        cell_dir = root / "distribution" / family / cell
        slices_path = cell_dir / "dist-slices.csv"
        runtime_path = cell_dir / "dist-runtime.csv"
        summary_path = cell_dir / "dist-summary.csv"
        percentiles_path = cell_dir / "dist-percentiles.json"

        missing = [
            path.name
            for path in (slices_path, runtime_path, summary_path, percentiles_path)
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"cell {cell}: missing dist-analyze output file(s): "
                f"{', '.join(missing)}"
            )

        slices_df = pd.read_csv(slices_path)
        if slices_df.empty:
            raise ValueError(f"cell {cell}: dist-slices.csv has zero rows")

        summary_df = pd.read_csv(summary_path)
        if summary_df.empty:
            raise ValueError(f"cell {cell}: dist-summary.csv has zero rows")

        runtime_df = pd.read_csv(runtime_path)

        data[cell] = {
            "cell": cell,
            "summary": summary_df.iloc[0].to_dict(),
            "quality": (
                "degraded"
                if (summary_df["quality"].astype(str) == "degraded").any()
                else "good"
            ),
            "durations_us": slices_df["duration_us"].astype(float).tolist(),
            "slices": slices_df.to_dict(orient="records"),
            "runtime_rows": runtime_df.to_dict(orient="records"),
            "percentiles": _first_percentile_table(percentiles_path),
        }
    return data


def load_slices_arrays(
    analysis_root: Path | str, family: str, cell: str
) -> dict[str, np.ndarray]:
    """Read one cell's dist-slices.csv into compact per-column numpy arrays.

    Returns ``{"ts_start_us": ndarray[int64], "ts_end_us": ndarray[int64],
    "duration_us": ndarray[float64], "cpu": ndarray[int64], "pod":
    ndarray[object]}`` — the Gantt data source, read WITHOUT per-row dicts
    (no-dict contract).  A missing dist-slices.csv raises
    ``FileNotFoundError`` naming the cell; a header-only file raises
    ``ValueError`` naming the cell (same loud-failure convention as
    ``load_family_data``).
    """
    root = Path(analysis_root)
    cell_dir = root / "distribution" / family / cell
    slices_path = cell_dir / "dist-slices.csv"
    if not slices_path.is_file():
        raise FileNotFoundError(f"cell {cell}: missing dist-slices.csv")
    slices_df = pd.read_csv(
        slices_path,
        usecols=["ts_start_us", "ts_end_us", "duration_us", "cpu", "pod"],
        dtype={"pod": "category"},
    )
    if slices_df.empty:
        raise ValueError(f"cell {cell}: dist-slices.csv has zero rows")
    return {
        "ts_start_us": slices_df["ts_start_us"].to_numpy(dtype=np.int64),
        "ts_end_us": slices_df["ts_end_us"].to_numpy(dtype=np.int64),
        "duration_us": slices_df["duration_us"].to_numpy(dtype=np.float64),
        "cpu": slices_df["cpu"].to_numpy(dtype=np.int64),
        "pod": slices_df["pod"].to_numpy(dtype=object),
    }


def load_cells_compact(
    analysis_root: Path | str, family: str, cells: list[str]
) -> dict[str, dict]:
    """Load every listed cell's OUTPUT as compact arrays, one cell at a time.

    This is the memory-bounded data path for the CLI: durations are
    float32 arrays, runtime keeps only the trajectory columns (``ts``,
    ``pod``, ``runtime_ns``), and slice rows are NEVER materialized as dicts —
    the Gantt loads its arrays on demand through ``load_slices_arrays``.  The
    per-cell dict shape mirrors ``load_family_data`` so the render functions
    accept it, except ``durations_us`` is an ndarray, ``runtime_rows`` is a
    DataFrame, and ``slices`` is ``None`` until the render pipeline attaches
    the representative cell's arrays.
    """
    root = Path(analysis_root)
    data: dict[str, dict] = {}
    for cell in cells:
        cell_dir = root / "distribution" / family / cell
        slices_path = cell_dir / "dist-slices.csv"
        runtime_path = cell_dir / "dist-runtime.csv"
        summary_path = cell_dir / "dist-summary.csv"
        percentiles_path = cell_dir / "dist-percentiles.json"

        missing = [
            path.name
            for path in (slices_path, runtime_path, summary_path, percentiles_path)
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"cell {cell}: missing dist-analyze output file(s): "
                f"{', '.join(missing)}"
            )

        slices_df = pd.read_csv(slices_path, usecols=["duration_us"])
        if slices_df.empty:
            raise ValueError(f"cell {cell}: dist-slices.csv has zero rows")

        summary_df = pd.read_csv(summary_path)
        if summary_df.empty:
            raise ValueError(f"cell {cell}: dist-summary.csv has zero rows")

        runtime_df = pd.read_csv(
            runtime_path,
            usecols=["ts", "pod", "runtime_ns"],
            dtype={"pod": "category"},
        )

        data[cell] = {
            "cell": cell,
            "summary": summary_df.iloc[0].to_dict(),
            "quality": (
                "degraded"
                if (summary_df["quality"].astype(str) == "degraded").any()
                else "good"
            ),
            "durations_us": slices_df["duration_us"].to_numpy(dtype=np.float32),
            "slices": None,
            "runtime_rows": runtime_df,
            "percentiles": _first_percentile_table(percentiles_path),
        }
    return data


# ---------------------------------------------------------------------------
# Pure core — selection, ECDF, cumulative runtime, annotations
# ---------------------------------------------------------------------------


def good_cells(data: dict) -> list[str]:
    """Cells in the pinned ``--cells`` order whose quality is "good".

    This is the exclusion rule: family comparison images use only
    good cells, while the per-cell histogram is still rendered for degraded
    cells.
    """
    return [cell for cell in data if data[cell]["quality"] == "good"]


def representative_cell(data: dict) -> str:
    """Good cell with the highest throttle_ratio (ties -> first in cell order).

    This is the pinned reading of "the cell that best shows the
    family's mechanism": the quota-throttled cell is the mechanism.  Raises
    ``ValueError`` when no good cell exists (never a silent empty Gantt).
    """
    good = good_cells(data)
    if not good:
        raise ValueError("family has no good cells")
    return max(good, key=lambda cell: data[cell]["summary"]["throttle_ratio"])


def compute_ecdf(
    durations_us: list[float],
) -> tuple[list[float], list[float]]:
    """ECDF of slice durations: x sorted ascending, y = rank/n.

    Follows the eevdf-plot.py convention; empty input returns ([], []).
    """
    x = sorted(durations_us)
    n = len(x)
    return x, [i / n for i in range(1, n + 1)]


def cumulative_runtime_series(
    runtime_rows: list[dict] | pd.DataFrame,
) -> pd.DataFrame:
    """Per-pod cumulative exec runtime from dist-runtime.csv DELTA samples.

    dist-runtime.csv carries per-switch ``runtime_ns`` deltas (kernel
    ``sched_stat_runtime`` semantics), so the trajectory cumsums them.  Rows
    are grouped by pod (sorted), each group sorted by ts, cumsummed, then
    concatenated.  Returns columns ``["pod","ts","runtime_ns",
    "cumulative_ns"]``; empty input returns an empty df with those columns.
    Accepts the legacy ``list[dict]`` rows or the compact DataFrame (ts/pod/
    runtime_ns columns) produced by ``load_cells_compact``.
    """
    if isinstance(runtime_rows, pd.DataFrame):
        df = runtime_rows
        if df.empty:
            return pd.DataFrame().reindex(
                columns=["pod", "ts", "runtime_ns", "cumulative_ns"]
            )
    else:
        if not runtime_rows:
            return pd.DataFrame().reindex(
                columns=["pod", "ts", "runtime_ns", "cumulative_ns"]
            )
        df = pd.DataFrame(runtime_rows)
    parts = []
    for pod in sorted(df["pod"].unique()):
        group = df[df["pod"] == pod].sort_values("ts").reset_index(drop=True)
        group["cumulative_ns"] = group["runtime_ns"].cumsum()
        parts.append(group)
    return pd.concat(parts, ignore_index=True)[
        ["pod", "ts", "runtime_ns", "cumulative_ns"]
    ]


def histogram_annotations(cell_data: dict) -> list[str]:
    """The mean/median/p95/p99 overlay labels for slice-histogram.png.

    Exactly ``["mean {mean_us:g} us", "median {median_us:g} us",
    "p95 {p95_us:g} us", "p99 {p99_us:g} us"]`` from the summary row.
    """
    s = cell_data["summary"]
    return [
        f"mean {s['mean_us']:g} us",
        f"median {s['median_us']:g} us",
        f"p95 {s['p95_us']:g} us",
        f"p99 {s['p99_us']:g} us",
    ]


def gantt_annotations(cell_data: dict) -> list[str]:
    """Annotation strings for the Gantt: cell label, CPU lanes, pods, throttle.

    Returns the cell label, ``"CPU <n>"`` per unique cpu (sorted), ``"pod
    <name>"`` per unique pod (sorted) and, when ``throttle_ratio > 0``,
    ``"throttle gaps hatched"`` and ``"throttle_ratio {r:g}"``.  Accepts
    either the legacy ``slices: list[dict]`` or the compact arrays dict from
    ``load_slices_arrays``.
    """
    slices = cell_data["slices"]
    if isinstance(slices, dict):
        cpus = np.unique(slices["cpu"]).tolist()
        pods = np.unique(slices["pod"]).tolist()
    else:
        cpus = sorted({row["cpu"] for row in slices})
        pods = sorted({row["pod"] for row in slices})
    lines = [cell_data["cell"]]
    lines.extend(f"CPU {cpu}" for cpu in cpus)
    lines.extend(f"pod {pod}" for pod in pods)
    if cell_data["summary"]["throttle_ratio"] > 0:
        lines.append("throttle gaps hatched")
        lines.append(f"throttle_ratio {cell_data['summary']['throttle_ratio']:g}")
    return lines


# ---------------------------------------------------------------------------
# Rendering — each image is data-driven, writes the PNG AND returns the Figure
# ---------------------------------------------------------------------------


def _add_text_annotations(
    fig, lines: list[str], *, start: float = 0.95, step: float = 0.05
) -> None:
    """Place every annotation line as ONE figure text object.

    Each string is independently assertable via ``fig.texts`` (the no-OCR
    label mechanism the tests use).
    """
    for index, line in enumerate(lines):
        fig.text(0.02, start - index * step, line, fontsize=9, family="monospace")


def render_cell_histogram(cell_data: dict, out_path: Path | str) -> plt.Figure:
    """Render the per-cell log-x slice-duration histogram with percentile overlays.

    Draws ``durations_us`` on a log-x axis with mean/median/p95/p99 vertical
    lines and renders every ``histogram_annotations`` string as a figure text
    object.  Writes the PNG and returns the Figure.
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_xscale("log")
    ax.hist(cell_data["durations_us"], bins=10, color="tab:blue", alpha=0.8)
    s = cell_data["summary"]
    for key, color in (
        ("mean_us", "tab:red"),
        ("median_us", "tab:green"),
        ("p95_us", "tab:orange"),
        ("p99_us", "tab:purple"),
    ):
        ax.axvline(s[key], color=color, linestyle="--", linewidth=1.2)
    ax.set_xlabel("slice duration (us)")
    ax.set_ylabel("slice count")
    ax.set_title(cell_data["cell"], fontsize=10)
    _add_text_annotations(fig, histogram_annotations(cell_data))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="png")
    plt.close(fig)
    return fig


def render_slice_comparison(cells_data: dict, out_path: Path | str) -> plt.Figure:
    """Render the family overlaid log-x histograms (good cells only).

    Every good cell's label appears as a legend entry; a degraded cell's
    label never appears.  Writes the PNG and returns the Figure.
    """
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.set_xscale("log")
    for cell in good_cells(cells_data):
        ax.hist(cells_data[cell]["durations_us"], bins=10, alpha=0.55, label=cell)
    ax.set_xlabel("slice duration (us)")
    ax.set_ylabel("slice count")
    ax.legend(fontsize=6)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="png")
    plt.close(fig)
    return fig


def render_ecdf_overlay(cells_data: dict, out_path: Path | str) -> plt.Figure:
    """Render the family slice-duration ECDF overlay (good cells only).

    Overlays ``compute_ecdf`` per good cell with the cell label as a legend
    entry; a degraded cell's label never appears.  Each series is
    plotted at bounded resolution (``ECDF_PLOT_POINTS`` evenly spaced ranks)
    so matplotlib never expands markers/vertex buffers for millions of ECDF
    points; small fixtures plot every point exactly.  Writes the PNG
    and returns the Figure.
    """
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.set_xscale("log")
    for cell in good_cells(cells_data):
        x, y = compute_ecdf(cells_data[cell]["durations_us"])
        if len(x) > ECDF_PLOT_POINTS:
            idx = _bounded_indices(len(x), ECDF_PLOT_POINTS)
            x = [x[i] for i in idx]
            y = [y[i] for i in idx]
        ax.plot(x, y, marker="o", markersize=3, linewidth=1.0, label=cell)
    ax.set_xlabel("slice duration (us)")
    ax.set_ylabel("ECDF")
    ax.legend(fontsize=6)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="png")
    plt.close(fig)
    return fig


def _bounded_indices(n: int, max_points: int) -> np.ndarray:
    """Deterministic even-spaced index set of size ``min(n, max_points)``.

    ``n <= max_points`` returns ``np.arange(n)`` so small series render
    EXACTLY (the caps are not a target); larger series keep the first and
    last index plus evenly spaced interior ranks.  No RNG and fixed spacing
    keep reruns byte-identical.
    """
    if n <= max_points:
        return np.arange(n, dtype=np.int64)
    return np.linspace(0, n - 1, max_points).round().astype(np.int64)


def _slices_to_arrays(
    slices: dict[str, np.ndarray] | list[dict],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Normalize the Gantt's ``slices`` to numpy arrays.

    Accepts either the legacy ``list[dict]`` rows or the compact arrays dict
    from ``load_slices_arrays`` and returns ``(ts_start_us, ts_end_us, cpu,
    pod)`` so the Gantt renders with a single vectorized ``ax.barh`` call
    instead of per-slice artists.
    """
    if isinstance(slices, dict):
        return (
            slices["ts_start_us"],
            slices["ts_end_us"],
            slices["cpu"],
            slices["pod"],
        )
    ts_start = np.asarray([row["ts_start_us"] for row in slices], dtype=np.int64)
    ts_end = np.asarray([row["ts_end_us"] for row in slices], dtype=np.int64)
    cpu = np.asarray([row["cpu"] for row in slices], dtype=np.int64)
    pod = np.asarray([row["pod"] for row in slices], dtype=object)
    return ts_start, ts_end, cpu, pod


def _cap_slices_bars(
    ts_start: np.ndarray,
    ts_end: np.ndarray,
    cpu: np.ndarray,
    pod: np.ndarray,
    budget: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Deterministically downsample slice arrays to at most *budget* bars.

    When the dataset exceeds the budget, each CPU lane's slices are aggregated
    into per-lane time buckets (``budget // lanes`` buckets each) and one
    representative slice — the LAST in array order within the bucket — is kept
    per non-empty bucket, preserving coverage across the whole window without
    per-slice artists.  No RNG and fixed bucket edges keep reruns byte-
    identical; a final uniform sample guarantees the total never
    exceeds the budget even with many lanes.
    """
    n = len(ts_start)
    if n <= budget:
        return ts_start, ts_end, cpu, pod
    lanes = np.unique(cpu)
    per_lane = max(1, budget // len(lanes))
    keep_s: list[np.ndarray] = []
    keep_e: list[np.ndarray] = []
    keep_c: list[np.ndarray] = []
    keep_p: list[np.ndarray] = []
    for lane in lanes:
        mask = cpu == lane
        s = ts_start[mask]
        e = ts_end[mask]
        p = pod[mask]
        m = len(s)
        if m <= per_lane:
            keep_s.append(s)
            keep_e.append(e)
            keep_c.append(np.full(m, lane, dtype=cpu.dtype))
            keep_p.append(p)
            continue
        edges = np.linspace(int(s.min()), int(e.max()), per_lane + 1)
        bin_idx = np.clip(np.digitize(s, edges) - 1, 0, per_lane - 1)
        for b in range(per_lane):
            sel = np.where(bin_idx == b)[0]
            if len(sel) == 0:
                continue
            j = sel[-1]
            keep_s.append(s[j : j + 1])
            keep_e.append(e[j : j + 1])
            keep_c.append(np.asarray([lane], dtype=cpu.dtype))
            keep_p.append(p[j : j + 1])
    if not keep_s:
        return ts_start[:0], ts_end[:0], cpu[:0], pod[:0]
    ts_start = np.concatenate(keep_s)
    ts_end = np.concatenate(keep_e)
    cpu = np.concatenate(keep_c)
    pod = np.concatenate(keep_p)
    if len(ts_start) > budget:
        idx = np.linspace(0, len(ts_start) - 1, budget).round().astype(np.int64)
        ts_start = ts_start[idx]
        ts_end = ts_end[idx]
        cpu = cpu[idx]
        pod = pod[idx]
    return ts_start, ts_end, cpu, pod


def render_gantt(cell_data: dict, out_path: Path | str) -> plt.Figure:
    """Render the per-CPU Gantt for the representative cell.

    One lane per unique cpu, slice bars colored by ``pod``; when the cell's
    ``throttle_ratio > 0`` a hatched background marks the throttle gaps.
    Bars are drawn with a single vectorized ``ax.barh`` call capped at
    ``GANTT_BAR_BUDGET`` (deterministic time-bucket downsampling on large
    cells — never one artist per slice).  Every ``gantt_annotations`` string
    is rendered as a figure text object.  Writes the PNG and returns the
    Figure.
    """
    fig, ax = plt.subplots(figsize=(12, 5))
    ts_start, ts_end, cpu, pod = _slices_to_arrays(cell_data["slices"])
    ts_start, ts_end, cpu, pod = _cap_slices_bars(
        ts_start, ts_end, cpu, pod, GANTT_BAR_BUDGET
    )
    cpus = np.unique(cpu)
    pods = sorted(np.unique(pod))
    pod_colors = {
        pod_name: plt.cm.tab10(index % 10) for index, pod_name in enumerate(pods)
    }
    lane_of = {cpu_id: lane for lane, cpu_id in enumerate(cpus)}
    ys = np.asarray([lane_of[c] for c in cpu], dtype=float)
    widths = (ts_end - ts_start).astype(float)
    lefts = ts_start.astype(float)
    colors = [pod_colors[p] for p in pod]

    ax.barh(
        ys,
        widths,
        left=lefts,
        height=0.6,
        color=colors,
        edgecolor="black",
        linewidth=0.3,
    )
    ax.set_yticks(range(len(cpus)))
    ax.set_yticklabels([f"CPU {cpu}" for cpu in cpus])
    ax.set_xlabel("time (us)")
    if cell_data["summary"]["throttle_ratio"] > 0:
        ax.axhspan(
            -0.5,
            len(cpus) - 0.5,
            xmin=0,
            xmax=1,
            facecolor="tab:red",
            alpha=0.08,
            hatch="//",
        )
    _add_text_annotations(fig, gantt_annotations(cell_data), start=0.95, step=0.04)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="png")
    plt.close(fig)
    return fig


def render_runtime_trajectory(cells_data: dict, out_path: Path | str) -> plt.Figure:
    """Render cumulative exec runtime per pod for every good cell.

    Each (cell, pod) series from ``cumulative_runtime_series`` is plotted with
    the legend entry formatted ``"{cell} {pod}"``.  Each series is plotted at
    bounded resolution (``TRAJECTORY_PLOT_POINTS`` evenly spaced samples) so
    matplotlib never expands markers/vertex buffers for millions of runtime
    rows; the per-cell cumulative DataFrame is dropped after its
    series are drawn.  Writes the PNG and returns the Figure.
    """
    fig, ax = plt.subplots(figsize=(11, 6))
    for cell in good_cells(cells_data):
        df = cumulative_runtime_series(cells_data[cell]["runtime_rows"])
        for pod in sorted(df["pod"].unique()):
            pod_df = df[df["pod"] == pod]
            if len(pod_df) > TRAJECTORY_PLOT_POINTS:
                pod_df = pod_df.iloc[
                    _bounded_indices(len(pod_df), TRAJECTORY_PLOT_POINTS)
                ]
            ax.plot(
                pod_df["ts"],
                pod_df["cumulative_ns"],
                marker="o",
                markersize=2,
                linewidth=1.0,
                label=f"{cell} {pod}",
            )
        del df
    ax.set_xlabel("time (us)")
    ax.set_ylabel("cumulative runtime (ns)")
    ax.legend(fontsize=5)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, format="png")
    plt.close(fig)
    return fig


def render_all(cells_data: dict, output_root: Path | str) -> dict[str, plt.Figure]:
    """Render the per-cell histograms + the four family images.

    The per-cell ``slice-histogram.png`` is written for EVERY cell (degraded
    included); the four family images use good cells only, with the
    Gantt drawn from ``representative_cell``.  Returns
    ``{relative_path: Figure}`` with keys ``f"{cell}/slice-histogram.png"`` +
    the four ``FAMILY_IMAGES`` names.  Raises ``ValueError`` when the family
    has no good cells.
    """
    root = Path(output_root)
    figures: dict[str, plt.Figure] = {}
    for cell in cells_data:
        cell_out = root / cell / CELL_HISTOGRAM
        figures[f"{cell}/{CELL_HISTOGRAM}"] = render_cell_histogram(
            cells_data[cell], cell_out
        )

    if not good_cells(cells_data):
        raise ValueError("family has no good cells")

    figures[FAMILY_IMAGES[0]] = render_slice_comparison(
        cells_data, root / FAMILY_IMAGES[0]
    )
    figures[FAMILY_IMAGES[1]] = render_ecdf_overlay(cells_data, root / FAMILY_IMAGES[1])
    figures[FAMILY_IMAGES[2]] = render_gantt(
        cells_data[representative_cell(cells_data)], root / FAMILY_IMAGES[2]
    )
    figures[FAMILY_IMAGES[3]] = render_runtime_trajectory(
        cells_data, root / FAMILY_IMAGES[3]
    )
    return figures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Render static EEVDF distribution images from dist-analyze output."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        metavar="DIR",
        help="Analysis root containing distribution/<family>/<cell>/ outputs",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        metavar="DIR",
        help="Output root; images land under distribution/<family>/",
    )
    parser.add_argument(
        "--family", required=True, metavar="NAME", help="Family name (output subdir)"
    )
    parser.add_argument(
        "--cells",
        required=True,
        metavar="C1,C2,...",
        help="Comma-separated cell labels in pinned order (defines comparison "
        "ordering and representative tie-breaking)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Render per-cell histograms + family images; non-zero on any loud failure."""
    parser = build_parser()
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        parser.error(f"data dir missing: {data_dir}")

    cells = [cell.strip() for cell in args.cells.split(",") if cell.strip()]
    if not cells:
        parser.error("--cells must list at least one cell")

    try:
        data = load_cells_compact(data_dir, args.family, cells)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_root = Path(args.output_dir) / "distribution" / args.family
    try:
        representative = representative_cell(data)
        # The Gantt's cell data comes from the array-backed loader (no dicts);
        # every other cell keeps only compact durations/runtime arrays.
        data[representative]["slices"] = load_slices_arrays(
            data_dir, args.family, representative
        )
        render_all(data, out_root)
    except ValueError as exc:
        if "no good cells" in str(exc):
            print(f"error: family {args.family} has no good cells", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
