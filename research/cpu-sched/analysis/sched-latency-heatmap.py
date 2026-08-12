#!/usr/bin/env python3
"""sched-latency-heatmap.py — Generate 2D scheduling latency heatmaps from EEVDF analysis CSVs.

Usage:
    sched-latency-heatmap.py --csv-dir <dir> --output-dir <dir>
    sched-latency-heatmap.py --help

Produces:

  1. sched-latency-heatmap.png — 2D heatmap: time on x-axis, CPU core on y-axis,
     scheduling latency as colour intensity.

  2. slice-heatmap.png — 2D heatmap: time on x-axis, task PID on y-axis,
     slice duration as colour intensity.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message: str, level: str = "info") -> None:
    """Print a timestamped log message to stderr."""
    print(f"[{level}] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def _apply_style() -> None:
    """Apply reasonable defaults for heatmap plots."""
    try:
        import seaborn as sns  # noqa: F811

        sns.set_theme(style="white")
        log("  Applied seaborn style")
    except ImportError:
        plt.style.use("ggplot")
        log("  seaborn not available; using ggplot style")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIG_WIDTH = 14  # inches
FIG_HEIGHT = 8  # inches
DPI = 150


# ---------------------------------------------------------------------------
# Heatmap 1 — Scheduling Latency by CPU over Time
# ---------------------------------------------------------------------------


def plot_sched_latency_heatmap(
    data_dir: str,
    output_dir: str,
) -> None:
    """Generate a 2D heatmap of scheduling latency over time per CPU core.

    Reads ``eevdf-latency.csv`` from *data_dir*.  Bins timestamps and CPUs
    into a 2D grid, with colour intensity representing mean wakeup latency.

    Args:
        data_dir: Directory containing EEVDF analysis CSV files.
        output_dir: Directory for output PNG plots.
    """
    csv_path = os.path.join(data_dir, "eevdf-latency.csv")
    if not os.path.exists(csv_path):
        log(f"  SKIP: sched-latency-heatmap — missing eevdf-latency.csv", level="warn")
        return

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        log(f"  Failed to read {csv_path}: {exc}", level="warn")
        return

    if df.empty:
        log("  SKIP: sched-latency-heatmap — no latency data", level="warn")
        return

    required = {"timestamp", "cpu", "wakeup_latency_us"}
    if not required.intersection(df.columns):
        log(
            f"  SKIP: sched-latency-heatmap — missing columns {required - set(df.columns)}",
            level="warn",
        )
        return

    # Filter valid latencies
    df = df[df["wakeup_latency_us"] >= 0].copy()
    if df.empty:
        log(
            "  SKIP: sched-latency-heatmap — no valid latencies after filtering",
            level="warn",
        )
        return

    # Bin time into 100ms intervals
    time_min = df["timestamp"].min()
    time_max = df["timestamp"].max()
    if time_max <= time_min:
        log("  SKIP: sched-latency-heatmap — constant timestamp", level="warn")
        return

    # Create time bins (100 ms = 100000 us)
    bin_width_us = 100000.0
    time_bins = np.arange(time_min, time_max + bin_width_us, bin_width_us)
    time_labels = (time_bins[:-1] + time_bins[1:]) / 2.0

    # Get unique CPUs
    cpus = sorted(df["cpu"].unique())

    # Build 2D grid: bins x CPUs
    n_time = len(time_labels)
    n_cpu = len(cpus)
    grid = np.full((n_cpu, n_time), np.nan)

    df["time_bin"] = np.digitize(df["timestamp"], time_bins) - 1

    for i, cpu in enumerate(cpus):
        for j in range(n_time):
            mask = (df["cpu"] == cpu) & (df["time_bin"] == j)
            subset = df.loc[mask, "wakeup_latency_us"]
            if not subset.empty:
                grid[i, j] = subset.mean()

    _render_heatmap(
        grid=grid,
        x_labels=time_labels,
        y_labels=[f"CPU {int(c)}" for c in cpus],
        xlabel="Time (us)",
        ylabel="CPU Core",
        title="Scheduling Latency by CPU over Time",
        cbar_label="Mean Wakeup Latency (us)",
        output_path=os.path.join(output_dir, "sched-latency-heatmap.png"),
        log_scale=True,
    )


# ---------------------------------------------------------------------------
# Heatmap 2 — Slice Duration by Task PID over Time
# ---------------------------------------------------------------------------


def plot_slice_heatmap(
    data_dir: str,
    output_dir: str,
) -> None:
    """Generate a 2D heatmap of slice duration over time per task PID.

    Reads ``eevdf-slices.csv`` from *data_dir*.  Bins timestamps and PIDs
    into a 2D grid, with colour intensity representing mean slice duration.

    Args:
        data_dir: Directory containing EEVDF analysis CSV files.
        output_dir: Directory for output PNG plots.
    """
    csv_path = os.path.join(data_dir, "eevdf-slices.csv")
    if not os.path.exists(csv_path):
        log(f"  SKIP: slice-heatmap — missing eevdf-slices.csv", level="warn")
        return

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        log(f"  Failed to read {csv_path}: {exc}", level="warn")
        return

    if df.empty:
        log("  SKIP: slice-heatmap — no slice data", level="warn")
        return

    required = {"timestamp_start", "pid", "duration_us"}
    if not required.intersection(df.columns):
        log(
            f"  SKIP: slice-heatmap — missing columns {required - set(df.columns)}",
            level="warn",
        )
        return

    # Filter valid durations
    df = df[df["duration_us"] > 0].copy()
    if df.empty:
        log("  SKIP: slice-heatmap — no valid durations", level="warn")
        return

    # Bin time into 100ms intervals
    time_min = df["timestamp_start"].min()
    time_max = df["timestamp_start"].max()
    if time_max <= time_min:
        log("  SKIP: slice-heatmap — constant timestamp", level="warn")
        return

    bin_width_us = 100000.0
    time_bins = np.arange(time_min, time_max + bin_width_us, bin_width_us)
    time_labels = (time_bins[:-1] + time_bins[1:]) / 2.0

    # Get unique PIDs (limit to top 50 by count for readability)
    pid_counts = df["pid"].value_counts()
    top_pids = pid_counts.head(50).index.tolist()
    df = df[df["pid"].isin(top_pids)]
    pids = sorted(top_pids)

    # Build 2D grid: PIDs x time bins
    n_time = len(time_labels)
    n_pid = len(pids)
    grid = np.full((n_pid, n_time), np.nan)

    df["time_bin"] = np.digitize(df["timestamp_start"], time_bins) - 1

    for i, pid in enumerate(pids):
        for j in range(n_time):
            mask = (df["pid"] == pid) & (df["time_bin"] == j)
            subset = df.loc[mask, "duration_us"]
            if not subset.empty:
                grid[i, j] = subset.mean()

    _render_heatmap(
        grid=grid,
        x_labels=time_labels,
        y_labels=[f"PID {int(p)}" for p in pids],
        xlabel="Time (us)",
        ylabel="Task PID",
        title="Slice Duration by Task over Time",
        cbar_label="Mean Slice Duration (us)",
        output_path=os.path.join(output_dir, "slice-heatmap.png"),
        log_scale=True,
    )


# ---------------------------------------------------------------------------
# Heatmap rendering helper
# ---------------------------------------------------------------------------


def _render_heatmap(
    grid: np.ndarray,
    x_labels: np.ndarray,
    y_labels: list[str],
    xlabel: str,
    ylabel: str,
    title: str,
    cbar_label: str,
    output_path: str,
    log_scale: bool = False,
) -> None:
    """Render and save a 2D heatmap.

    Args:
        grid: 2D numpy array (n_y x n_x) with numeric values or NaN.
        x_labels: Tick labels for the x-axis.
        y_labels: Tick labels for the y-axis.
        xlabel: Label for the x-axis.
        ylabel: Label for the y-axis.
        title: Plot title.
        cbar_label: Colour bar label.
        output_path: Output PNG file path.
        log_scale: If True, apply logarithmic colour scaling.
    """
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, FIG_HEIGHT))

    # Mask NaN values
    masked = np.ma.masked_invalid(grid)

    if log_scale:
        # Use LogNorm for colour mapping
        vmin = np.nanmin(grid)
        vmax = np.nanmax(grid)
        if vmin <= 0 or np.isnan(vmin):
            vmin = grid[~np.isnan(grid)].min()
        if vmin <= 0:
            vmin = 0.1  # Minimum positive value for log scale
        norm = mcolors.LogNorm(vmin=vmin, vmax=vmax)
        cmap = "viridis"
    else:
        norm = None
        cmap = "viridis"

    im = ax.pcolormesh(
        x_labels,
        np.arange(len(y_labels)),
        masked,
        shading="auto",
        cmap=cmap,
        norm=norm,
    )

    cbar = fig.colorbar(im, ax=ax, label=cbar_label)
    cbar.ax.tick_params(labelsize=10)

    ax.set_xlabel(xlabel, fontsize=12)
    ax.set_ylabel(ylabel, fontsize=12)
    ax.set_title(title, fontsize=14)

    # Set y-ticks: show a subset if there are many
    n_y = len(y_labels)
    if n_y > 30:
        step = max(1, n_y // 20)
        ax.set_yticks(np.arange(0, n_y, step))
        ax.set_yticklabels(y_labels[::step], fontsize=8)
    else:
        ax.set_yticks(np.arange(n_y))
        ax.set_yticklabels(y_labels, fontsize=8)

    # Rotate x labels if many bins
    if len(x_labels) > 20:
        ax.tick_params(axis="x", rotation=45, labelsize=8)
    else:
        ax.tick_params(labelsize=10)

    fig.tight_layout()
    fig.savefig(output_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate 2D scheduling latency heatmaps from EEVDF analysis CSVs."
        ),
    )
    parser.add_argument(
        "--csv-dir",
        required=True,
        help="Directory containing EEVDF analysis CSV files from eevdf-analyze.py",
    )
    parser.add_argument(
        "--output-dir",
        default="./eevdf-heatmaps",
        help="Output directory for PNG heatmaps (default: ./eevdf-heatmaps)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and generate heatmaps.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code for the process.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    csv_dir = args.csv_dir
    output_dir = args.output_dir

    if not os.path.isdir(csv_dir):
        log(f"Not found: {csv_dir}", level="error")
        return 1

    _apply_style()
    os.makedirs(output_dir, exist_ok=True)
    log(f"Output directory: {output_dir}")

    log("Loading data and generating heatmaps...")
    plot_sched_latency_heatmap(csv_dir, output_dir)
    plot_slice_heatmap(csv_dir, output_dir)

    log(f"Heatmaps saved to: {output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
