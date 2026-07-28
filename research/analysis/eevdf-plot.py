#!/usr/bin/env python3
"""eevdf-plot.py — Generate EEVDF visualization plots from analysis CSV data.

Usage:
    eevdf-plot.py --csv-dir <dir> --output-dir <dir>
    eevdf-plot.py --csv-dir <dir> --output-dir <dir> --comparison --labels A,B
    eevdf-plot.py --help

Consumes the CSV outputs from eevdf-analyze.py and produces:

  1. vruntime-trajectory.png  — Vruntime over wall-clock time per task
  2. slice-distribution.png   — Histogram of CPU slice durations
  3. deadline-drift.png       — Scatter plot: deadline vs actual schedule time
  4. lag-timeseries.png       — Per-task lag over time (y=0 reference line)
  5. sched-latency-ecdf.png   — ECDF of scheduling wakeup latency

With the ``--comparison`` flag, overlays data from multiple config directories.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
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
    """Apply seaborn-like style if seaborn is available."""
    try:
        import seaborn as sns  # noqa: F811

        sns.set_theme(style="whitegrid")
        log("  Applied seaborn style")
    except ImportError:
        plt.style.use("ggplot")
        log("  seaborn not available; using ggplot style")


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

REQUIRED_CSVS = [
    "eevdf-vruntime.csv",
    "eevdf-slices.csv",
    "eevdf-latency.csv",
    "eevdf-lag.csv",
]


def load_csv_data(csv_dir: str, label: str = "") -> dict[str, pd.DataFrame]:
    """Load EEVDF analysis CSVs from *csv_dir* into a dict of DataFrames.

    Missing files produce an empty DataFrame.
    """
    data: dict[str, pd.DataFrame] = {}
    missing: list[str] = []

    for csv_name in REQUIRED_CSVS:
        path = os.path.join(csv_dir, csv_name)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                data[csv_name.replace(".csv", "")] = df
                log(
                    f"  Loaded {csv_name} ({len(df)} rows{(' [' + label + ']') if label else ''})"
                )
            except Exception as exc:
                log(f"  Failed to load {csv_name}: {exc}", level="warn")
                data[csv_name.replace(".csv", "")] = pd.DataFrame()
        else:
            missing.append(csv_name)
            data[csv_name.replace(".csv", "")] = pd.DataFrame()

    if missing:
        log(f"  Missing CSV files: {', '.join(missing)}", level="warn")

    return data


# ---------------------------------------------------------------------------
# Plot configuration
# ---------------------------------------------------------------------------

FIG_WIDTH = 1200 / 100  # 1200px at 100 dpi → 12 inches
FIG_HEIGHT = 800 / 100  # 800px at 100 dpi → 8 inches
DPI = 150


def _figure() -> tuple[plt.Figure, plt.Axes]:
    """Create a figure with standard dimensions."""
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, FIG_HEIGHT))
    return fig, ax


def _save(fig: plt.Figure, path: str) -> None:
    """Save figure with standard settings."""
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {path}")


# ---------------------------------------------------------------------------
# Plot 1 — Vruntime Trajectory
# ---------------------------------------------------------------------------


def plot_vruntime_trajectory(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    label: str = "",
) -> None:
    """Line plot of vruntime vs wall-clock time per task (colored by PID)."""
    vr = data.get("eevdf-vruntime", pd.DataFrame())
    if vr.empty:
        log("  SKIP: vruntime-trajectory — no vruntime data", level="warn")
        return

    fig, ax = _figure()

    # Require 'timestamp' and 'vruntime' columns
    required_cols = {"timestamp", "vruntime", "pid", "task"}
    if not required_cols.intersection(vr.columns):
        log("  SKIP: vruntime-trajectory — missing required columns", level="warn")
        _save(fig, os.path.join(output_dir, "vruntime-trajectory.png"))
        return

    # Group by pid and plot each trajectory
    pids = vr["pid"].unique()
    colours = plt.cm.tab10(np.linspace(0, 1, min(len(pids), 10)))

    for idx, pid in enumerate(pids[:50]):  # Limit to 50 PIDs for readability
        pid_data = vr[vr["pid"] == pid].sort_values("timestamp")
        if pid_data.empty:
            continue
        colour = colours[idx % len(colours)]
        task_name = (
            str(pid_data["task"].iloc[0]) if "task" in pid_data.columns else str(pid)
        )
        label_str = f"PID {int(pid)} ({task_name})"
        if label:
            label_str = f"[{label}] {label_str}"
        ax.plot(
            pid_data["timestamp"],
            pid_data["vruntime"],
            color=colour,
            linewidth=1,
            alpha=0.7,
            label=label_str,
        )

    ax.set_xlabel("Time (us)")
    ax.set_ylabel("Vruntime (ns)")
    title = "EEVDF Vruntime Trajectory"
    if label:
        title += f" — {label}"
    ax.set_title(title, fontsize=14)
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)

    _save(fig, os.path.join(output_dir, "vruntime-trajectory.png"))


# ---------------------------------------------------------------------------
# Plot 2 — Slice Distribution
# ---------------------------------------------------------------------------


def plot_slice_distribution(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    label: str = "",
) -> None:
    """Histogram of CPU slice durations, with percentile overlays."""
    sl = data.get("eevdf-slices", pd.DataFrame())
    if sl.empty or "duration_us" not in sl.columns:
        log("  SKIP: slice-distribution — no slice data", level="warn")
        return

    durations = sl["duration_us"].values
    durations = durations[~np.isnan(durations) & (durations > 0)]

    if len(durations) == 0:
        log("  SKIP: slice-distribution — no valid durations", level="warn")
        return

    fig, ax = _figure()

    ax.hist(durations, bins=80, alpha=0.7, color="steelblue", edgecolor="black")
    ax.set_xscale("log")
    ax.set_xlabel("Slice Duration (us)")
    ax.set_ylabel("Frequency")
    title = "EEVDF CPU Slice Duration Distribution"
    if label:
        title += f" — {label}"
    ax.set_title(title, fontsize=14)
    ax.grid(True, alpha=0.3)

    # Statistics overlays
    mean_val = np.mean(durations)
    median_val = np.median(durations)
    p95_val = np.percentile(durations, 95)
    p99_val = np.percentile(durations, 99)

    stats = [
        (mean_val, "Mean", "red", "--"),
        (median_val, "Median", "green", "--"),
        (p95_val, "P95", "orange", "--"),
        (p99_val, "P99", "purple", "--"),
    ]
    for val, sname, colour, style in stats:
        ax.axvline(
            val,
            color=colour,
            linestyle=style,
            linewidth=2,
            label=f"{sname}: {val:.1f} us",
        )

    ax.legend(fontsize=10)
    _save(fig, os.path.join(output_dir, "slice-distribution.png"))


# ---------------------------------------------------------------------------
# Plot 3 — Deadline Drift
# ---------------------------------------------------------------------------


def plot_deadline_drift(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    label: str = "",
) -> None:
    """Scatter plot: ideal schedule time vs actual schedule time.

    This uses slice data as an approximation: timestamp_start is the
    scheduled time, and an 'ideal' line at y=x is drawn for reference.
    Points are coloured by CPU.
    """
    sl = data.get("eevdf-slices", pd.DataFrame())
    if sl.empty:
        log("  SKIP: deadline-drift — no slice data", level="warn")
        return

    # Use timestamp_start as 'actual' and derive an expected start
    # from the previous slice on the same CPU.
    if "cpu" not in sl.columns or "timestamp_start" not in sl.columns:
        log("  SKIP: deadline-drift — missing required columns", level="warn")
        return

    # Sort by CPU and timestamp
    sl_sorted = sl.sort_values(["cpu", "timestamp_start"])

    # Approximate: for each slice, the expected start is the end of the
    # previous slice on the same CPU (or its own start if first).
    cpus = sl_sorted["cpu"].unique()
    fig, ax = _figure()

    colours = plt.cm.tab10(np.linspace(0, 1, min(len(cpus), 10)))
    all_actual = []
    all_expected = []

    for idx, cpu in enumerate(cpus):
        cpu_data = sl_sorted[sl_sorted["cpu"] == cpu].copy()
        if len(cpu_data) < 2:
            continue

        actual = cpu_data["timestamp_start"].values
        expected = (
            cpu_data["timestamp_end"]
            .shift(1)
            .fillna(cpu_data["timestamp_start"])
            .values
        )
        colour = colours[idx % len(colours)]

        ax.scatter(
            expected,
            actual,
            color=colour,
            s=10,
            alpha=0.5,
            label=f"CPU {int(cpu)}",
            edgecolors="none",
        )
        all_actual.extend(actual)
        all_expected.extend(expected)

    # y = x reference line
    if all_actual and all_expected:
        min_val = min(min(all_actual), min(all_expected))
        max_val = max(max(all_actual), max(all_expected))
        ax.plot(
            [min_val, max_val],
            [min_val, max_val],
            color="black",
            linestyle="--",
            linewidth=1.5,
            label="Ideal (y=x)",
        )

    ax.set_xlabel("Expected Schedule Time (us)")
    ax.set_ylabel("Actual Schedule Time (us)")
    title = "EEVDF Schedule Deadline Drift"
    if label:
        title += f" — {label}"
    ax.set_title(title, fontsize=14)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="box")

    _save(fig, os.path.join(output_dir, "deadline-drift.png"))


# ---------------------------------------------------------------------------
# Plot 4 — Lag Timeseries
# ---------------------------------------------------------------------------


def plot_lag_timeseries(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    label: str = "",
) -> None:
    """Per-task lag over time with a horizontal line at y=0 for reference."""
    lag = data.get("eevdf-lag", pd.DataFrame())
    if lag.empty or "lag_us" not in lag.columns:
        log("  SKIP: lag-timeseries — no lag data", level="warn")
        return

    fig, ax = _figure()

    # y=0 reference line
    ax.axhline(y=0, color="black", linestyle="-", linewidth=1.5, alpha=0.5)

    pids = lag["pid"].unique()
    colours = plt.cm.tab10(np.linspace(0, 1, min(len(pids), 10)))

    for idx, pid in enumerate(pids[:30]):  # Limit to 30 PIDs
        pid_data = lag[lag["pid"] == pid].sort_values("timestamp")
        if pid_data.empty:
            continue
        colour = colours[idx % len(colours)]
        task_name = (
            str(pid_data["task"].iloc[0]) if "task" in pid_data.columns else str(pid)
        )
        label_str = f"PID {int(pid)} ({task_name})"
        if label:
            label_str = f"[{label}] {label_str}"
        ax.plot(
            pid_data["timestamp"],
            pid_data["lag_us"],
            color=colour,
            linewidth=1,
            alpha=0.7,
            label=label_str,
        )

    ax.set_xlabel("Time (us)")
    ax.set_ylabel("Lag (us)")
    title = "EEVDF Per-Task Lag"
    if label:
        title += f" — {label}"
    ax.set_title(title, fontsize=14)
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)

    _save(fig, os.path.join(output_dir, "lag-timeseries.png"))


# ---------------------------------------------------------------------------
# Plot 5 — Scheduling Latency ECDF
# ---------------------------------------------------------------------------


def plot_sched_latency_ecdf(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    label: str = "",
) -> None:
    """ECDF of scheduling wakeup latency."""
    lt = data.get("eevdf-latency", pd.DataFrame())
    if lt.empty or "wakeup_latency_us" not in lt.columns:
        log("  SKIP: sched-latency-ecdf — no latency data", level="warn")
        return

    latencies = lt["wakeup_latency_us"].values
    latencies = latencies[~np.isnan(latencies) & (latencies >= 0)]

    if len(latencies) == 0:
        log("  SKIP: sched-latency-ecdf — no valid latencies", level="warn")
        return

    # Sort for ECDF
    sorted_lat = np.sort(latencies)
    ecdf_y = np.arange(1, len(sorted_lat) + 1) / len(sorted_lat)

    fig, ax = _figure()

    label_str = "Latency ECDF"
    if label:
        label_str = f"[{label}] {label_str}"

    ax.step(
        sorted_lat,
        ecdf_y,
        where="post",
        color="steelblue",
        linewidth=2,
        label=label_str,
    )
    ax.set_xscale("log")
    ax.set_xlabel("Wakeup Latency (us)")
    ax.set_ylabel("Cumulative Probability")
    title = "EEVDF Scheduling Latency ECDF"
    if label:
        title += f" — {label}"
    ax.set_title(title, fontsize=14)

    # Mark key percentiles
    for pct, colour, style in [
        (50, "green", "--"),
        (95, "orange", "--"),
        (99, "purple", "--"),
    ]:
        val = np.percentile(latencies, pct)
        ax.axvline(
            val,
            color=colour,
            linestyle=style,
            linewidth=1.5,
            label=f"P{pct}: {val:.1f} us",
        )

    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    _save(fig, os.path.join(output_dir, "sched-latency-ecdf.png"))


# ---------------------------------------------------------------------------
# Comparison mode — overlay data from multiple directories
# ---------------------------------------------------------------------------


def plot_comparison(
    csv_dirs: list[str],
    labels: list[str],
    output_dir: str,
) -> None:
    """Generate comparison plots overlaying data from multiple config directories.

    Args:
        csv_dirs: List of directories containing EEVDF analysis CSVs.
        labels: Corresponding labels for each directory.
        output_dir: Output directory for plots.
    """
    log("Generating comparison plots...")
    os.makedirs(output_dir, exist_ok=True)

    all_data: list[tuple[str, dict[str, pd.DataFrame]]] = []
    for csv_dir, lbl in zip(csv_dirs, labels):
        data = load_csv_data(csv_dir, lbl)
        all_data.append((lbl, data))

    if not all_data:
        log("  No data loaded for comparison", level="warn")
        return

    # ---- Comparison: Slice Distribution ECDF ----
    fig, ax = _figure()
    for lbl, data in all_data:
        sl = data.get("eevdf-slices", pd.DataFrame())
        if sl.empty or "duration_us" not in sl.columns:
            continue
        durations = sl["duration_us"].values
        durations = durations[~np.isnan(durations) & (durations > 0)]
        if len(durations) == 0:
            continue
        sorted_dur = np.sort(durations)
        ecdf_y = np.arange(1, len(sorted_dur) + 1) / len(sorted_dur)
        ax.step(sorted_dur, ecdf_y, where="post", linewidth=2, label=lbl)

    ax.set_xscale("log")
    ax.set_xlabel("Slice Duration (us)")
    ax.set_ylabel("Cumulative Probability")
    ax.set_title("Slice Duration ECDF — Config Comparison")
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    _save(fig, os.path.join(output_dir, "slice-dist-cmp.png"))

    # ---- Comparison: Latency ECDF ----
    fig, ax = _figure()
    for lbl, data in all_data:
        lt = data.get("eevdf-latency", pd.DataFrame())
        if lt.empty or "wakeup_latency_us" not in lt.columns:
            continue
        latencies = lt["wakeup_latency_us"].values
        latencies = latencies[~np.isnan(latencies) & (latencies >= 0)]
        if len(latencies) == 0:
            continue
        sorted_lat = np.sort(latencies)
        ecdf_y = np.arange(1, len(sorted_lat) + 1) / len(sorted_lat)
        ax.step(sorted_lat, ecdf_y, where="post", linewidth=2, label=lbl)

    ax.set_xscale("log")
    ax.set_xlabel("Wakeup Latency (us)")
    ax.set_ylabel("Cumulative Probability")
    ax.set_title("Scheduling Latency ECDF — Config Comparison")
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    _save(fig, os.path.join(output_dir, "sched-latency-ecdf-overlay.png"))

    # ---- Comparison: Lag box plot ----
    fig, ax = _figure()
    plot_data: list[pd.Series] = []
    plot_labels: list[str] = []
    for lbl, data in all_data:
        lag = data.get("eevdf-lag", pd.DataFrame())
        if lag.empty or "lag_us" not in lag.columns:
            continue
        vals = lag["lag_us"].dropna().values
        if len(vals) > 0:
            plot_data.append(pd.Series(vals))
            plot_labels.append(lbl)

    if plot_data:
        ax.boxplot(plot_data, showfliers=False)
        ax.set_xticklabels(plot_labels)
        ax.set_ylabel("Lag (us)")
        ax.set_title("EEVDF Lag Distribution — Config Comparison")
        ax.grid(True, alpha=0.3)
        _save(fig, os.path.join(output_dir, "lag-cmp.png"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=("Generate EEVDF visualization plots from analysis CSV data."),
    )
    parser.add_argument(
        "--csv-dir",
        required=True,
        help="Directory containing EEVDF analysis CSV files from eevdf-analyze.py",
    )
    parser.add_argument(
        "--output-dir",
        default="./eevdf-plots",
        help="Output directory for PNG plots (default: ./eevdf-plots)",
    )
    parser.add_argument(
        "--comparison",
        action="store_true",
        help="Enable comparison mode: overlay data from multiple config directories",
    )
    parser.add_argument(
        "--labels",
        default="",
        help="Comma-separated labels for comparison directories (required with --comparison)",
    )
    parser.add_argument(
        "--extra-dirs",
        default="",
        help="Comma-separated list of additional CSV directories for comparison",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Single label for single-config mode (added to plot titles)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and generate plots.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code for the process.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    csv_dir = args.csv_dir
    output_dir = args.output_dir
    label = args.label

    _apply_style()

    # Create output directory early
    os.makedirs(output_dir, exist_ok=True)
    log(f"Output directory: {output_dir}")

    if args.comparison:
        # Comparison mode
        extra_dirs = [d.strip() for d in args.extra_dirs.split(",") if d.strip()]
        labels = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()]

        if not extra_dirs:
            log(
                "--comparison requires --extra-dirs with additional CSV directories",
                level="error",
            )
            return 1

        if len(labels) != len(extra_dirs) + 1:
            log(
                f"Number of labels ({len(labels)}) must equal number of directories "
                f"({1 + len(extra_dirs)})",
                level="error",
            )
            return 1

        all_dirs = [csv_dir] + extra_dirs
        plot_comparison(all_dirs, labels, output_dir)
        return 0

    # Single-config mode
    log("Loading data...")
    data = load_csv_data(csv_dir, label)
    has_data = any(not df.empty for df in data.values())
    if not has_data:
        log("No data loaded, skipping plot generation", level="warn")
        return 0

    log("Generating plots...")
    plot_vruntime_trajectory(data, output_dir, label)
    plot_slice_distribution(data, output_dir, label)
    plot_deadline_drift(data, output_dir, label)
    plot_lag_timeseries(data, output_dir, label)
    plot_sched_latency_ecdf(data, output_dir, label)

    log(f"Plots saved to: {output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
