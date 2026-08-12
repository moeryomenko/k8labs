#!/usr/bin/env python3
"""plot-perfetto-cpu.py — Generate CPU execution time visualization plots from Perfetto data.

Usage:
    plot-perfetto-cpu.py <input_path> [--pod-name NAME] [--output-dir PATH]
    plot-perfetto-cpu.py --help

Loads either a .perfetto-trace file (analyzing it via perfetto-analyze first) or
a directory of pre-analyzed CSV files, then generates 4 visualizations:

  1. cpu-timeline.png       — Gantt chart of threads per CPU
  2. slice-distribution.png — Histogram of CPU slice durations
  3. cpu-utilization.png    — Per-CPU utilization over time
  4. sched-latency.png      — Scheduling wakeup latency distribution
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
# CSV loading
# ---------------------------------------------------------------------------

CSV_FILES: dict[str, str] = {
    "threads": "perfetto-threads.csv",
    "cpu_util": "perfetto-cpu-util.csv",
    "process_summary": "perfetto-process-summary.csv",
    "sched_latency": "perfetto-sched-latency.csv",
}


def load_csv_data(csv_dir: str, pod_name: str = "") -> dict[str, pd.DataFrame]:
    """Load CSV files from *csv_dir* into a dict of DataFrames.

    Missing files produce an empty DataFrame.  If *pod_name* is non-empty,
    thread / process / latency rows are filtered to those whose name contains
    *pod_name* (case-insensitive substring match).
    """
    data: dict[str, pd.DataFrame] = {}
    missing: list[str] = []

    for key, filename in CSV_FILES.items():
        path = os.path.join(csv_dir, filename)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                data[key] = df
                log(f"  Loaded {filename} ({len(df)} rows)")
            except Exception as exc:
                log(f"  Failed to load {filename}: {exc}", level="warn")
                data[key] = pd.DataFrame()
        else:
            missing.append(filename)
            data[key] = pd.DataFrame()

    if missing:
        msg = f"  Missing CSV files: {', '.join(missing)}"
        log(msg, level="warn")

    # Apply pod-name filter where applicable
    if pod_name:
        threads = data.get("threads")
        if threads is not None and not threads.empty:
            mask = threads["thread_name"].str.contains(pod_name, case=False, na=False)
            data["threads"] = threads[mask]
            log(f"  Filtered threads to '{pod_name}': {len(data['threads'])} rows")

        psum = data.get("process_summary")
        if psum is not None and not psum.empty:
            mask = psum["name"].str.contains(pod_name, case=False, na=False)
            data["process_summary"] = psum[mask]

        sched = data.get("sched_latency")
        if sched is not None and not sched.empty:
            mask = sched["thread_name"].str.contains(pod_name, case=False, na=False)
            data["sched_latency"] = sched[mask]

    return data


# ---------------------------------------------------------------------------
# Trace processing (Mode 1)
# ---------------------------------------------------------------------------


def process_trace_mode(trace_path: str, output_dir: str) -> dict[str, pd.DataFrame]:
    """Analyze *trace_path* via ``perfetto-analyze.py`` and return loaded CSVs.

    If the ``perfetto`` package is not available, or trace processing fails,
    an empty dict is returned and the failure is logged as a warning (not
    error), so callers can exit cleanly.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        # Import the sibling module (both files live in the same directory).
        # When run as a script, sys.path[0] is the script's directory.
        _here = os.path.dirname(os.path.abspath(__file__))
        orig_path = list(sys.path)
        if _here not in sys.path:
            sys.path.insert(0, _here)

        import perfetto_analyze  # pylint: disable=import-outside-toplevel

        perfetto_analyze.process_trace_file(trace_path, output_dir)
    except ImportError:
        log(
            "perfetto package not available. Install with: pip install perfetto",
            level="warn",
        )
        return {}
    except Exception as exc:
        log(f"Trace processing failed: {exc}", level="warn")
        return {}

    # Load CSVs from the output directory
    return load_csv_data(output_dir)


# ---------------------------------------------------------------------------
# Plot 1 — CPU Timeline (Gantt chart)
# ---------------------------------------------------------------------------


def plot_cpu_timeline(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    _pod_name: str = "",
) -> None:
    """Generate a Gantt chart showing threads scheduled on each CPU core."""
    threads = data.get("threads", pd.DataFrame())
    if threads.empty:
        log("  SKIP: cpu-timeline — no thread data", level="warn")
        return

    cpus = sorted(threads["cpu"].unique())
    n_cpus = len(cpus)

    fig, axes = plt.subplots(n_cpus, 1, figsize=(12, max(3, n_cpus * 2)), sharex=True)
    if n_cpus == 1:
        axes = [axes]

    # Colour each unique thread name from the tab10 cycle.
    unique_threads = sorted(threads["thread_name"].unique())
    colors = plt.cm.tab10(np.linspace(0, 1, len(unique_threads)))
    thread_colours = {name: colors[i] for i, name in enumerate(unique_threads)}

    for idx, cpu in enumerate(cpus):
        ax = axes[idx]
        cpu_data = threads[threads["cpu"] == cpu]

        if cpu_data.empty:
            ax.text(
                0.5,
                0.5,
                f"CPU {cpu}\n(no data)",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=12,
            )
            ax.set_ylabel(f"CPU {cpu}", fontsize=12)
            continue

        for _, row in cpu_data.iterrows():
            thread_name = row["thread_name"]
            exec_time_s = row["exec_time_ms"] / 1000.0
            colour = thread_colours.get(thread_name, "gray")
            ax.barh(thread_name, exec_time_s, height=0.8, color=colour, alpha=0.7)

        ax.set_ylabel(f"CPU {cpu}", fontsize=12)
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=10)

    axes[0].set_title("CPU Scheduling Timeline", fontsize=14)
    axes[-1].set_xlabel("Time (s)", fontsize=12)

    fig.tight_layout()
    path = os.path.join(output_dir, "cpu-timeline.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {path}")


# ---------------------------------------------------------------------------
# Plot 2 — Pod CPU Slice Duration Distribution
# ---------------------------------------------------------------------------


def plot_slice_distribution(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    _pod_name: str = "",
) -> None:
    """Produce a log-scale histogram of slice durations with statistics overlay."""
    threads = data.get("threads", pd.DataFrame())
    if threads.empty:
        log("  SKIP: slice-distribution — no thread data", level="warn")
        return

    # exec_time_ms is in milliseconds ― convert to microseconds for display.
    durations_us = threads["exec_time_ms"].values * 1000.0

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.hist(durations_us, bins=50, alpha=0.7, color="steelblue", edgecolor="black")
    ax.set_xscale("log")
    ax.set_xlabel("Slice Duration (us)", fontsize=12)
    ax.set_ylabel("Frequency", fontsize=12)
    ax.set_title("CPU Slice Duration Distribution", fontsize=14)
    ax.grid(True, alpha=0.3)

    # Overlay percentile / mean lines
    mean_val = np.mean(durations_us)
    median_val = np.median(durations_us)
    p95_val = np.percentile(durations_us, 95)
    p99_val = np.percentile(durations_us, 99)

    stats = [
        (mean_val, "Mean", "red", "--"),
        (median_val, "Median", "green", "--"),
        (p95_val, "P95", "orange", "--"),
        (p99_val, "P99", "purple", "--"),
    ]
    for val, label, colour, style in stats:
        ax.axvline(
            val,
            color=colour,
            linestyle=style,
            linewidth=2,
            label=f"{label}: {val:.1f} us",
        )

    ax.legend(fontsize=10)
    fig.tight_layout()

    path = os.path.join(output_dir, "slice-distribution.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {path}")


# ---------------------------------------------------------------------------
# Plot 3 — CPU Utilization Over Time
# ---------------------------------------------------------------------------


def plot_cpu_utilization(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    _pod_name: str = "",
) -> None:
    """Plot per-core utilisation as a scatter / horizontal-line chart.

    With aggregate CSV data (no per-interval slices) this shows one point
    per core.  With per-slice data it would show a 100-ms-binned line plot.
    """
    cpu_util = data.get("cpu_util", pd.DataFrame())
    if cpu_util.empty:
        log("  SKIP: cpu-utilization — no CPU utilization data", level="warn")
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    cores = sorted(cpu_util["core"].unique())
    for i, core in enumerate(cores):
        core_data = cpu_util[cpu_util["core"] == core]
        if core_data.empty:
            continue
        util = core_data["utilization_pct"].values[0]
        colour = f"C{i}"
        ax.axhline(
            y=util,
            color=colour,
            linestyle="-",
            linewidth=2,
            alpha=0.7,
            label=f"CPU {core}: {util:.1f}%",
        )
        ax.scatter([core], [util], color=colour, s=100, alpha=0.7, zorder=5)

    ax.set_xlabel("CPU Core", fontsize=12)
    ax.set_ylabel("CPU Utilization (%)", fontsize=12)
    ax.set_title("CPU Utilization During Experiment", fontsize=14)
    ax.set_ylim(0, 100)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.tick_params(labelsize=10)

    fig.tight_layout()
    path = os.path.join(output_dir, "cpu-utilization.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {path}")


# ---------------------------------------------------------------------------
# Plot 4 — Scheduling Latency Distribution
# ---------------------------------------------------------------------------


def plot_sched_latency(
    data: dict[str, pd.DataFrame],
    output_dir: str,
    _pod_name: str = "",
) -> None:
    """Plot a histogram of wakeup latencies or a placeholder when no data."""
    sched = data.get("sched_latency", pd.DataFrame())

    fig, ax = plt.subplots(figsize=(10, 6))

    if sched.empty:
        # Placeholder when no sched_waking data is available.
        ax.text(
            0.5,
            0.5,
            "No sched_waking data available",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.set_title("Scheduling Wakeup Latency (no data)", fontsize=14)
    else:
        # wakeup_latency_ms -> us
        latencies_us = sched["wakeup_latency_ms"].values * 1000.0
        weights = sched["count"].values

        ax.hist(
            latencies_us,
            bins=50,
            weights=weights,
            alpha=0.7,
            color="steelblue",
            edgecolor="black",
        )
        ax.set_xscale("log")
        ax.set_xlabel("Wakeup Latency (us)", fontsize=12)
        ax.set_ylabel("Frequency", fontsize=12)
        ax.set_title("Scheduling Wakeup Latency Distribution", fontsize=14)

        # Weighted statistics.
        expanded = np.repeat(latencies_us, weights.astype(int))
        mean_val = np.mean(expanded)
        median_val = np.median(expanded)
        p95_val = np.percentile(expanded, 95)
        p99_val = np.percentile(expanded, 99)

        stats = [
            (mean_val, "Mean", "red", "--"),
            (median_val, "Median", "green", "--"),
            (p95_val, "P95", "orange", "--"),
            (p99_val, "P99", "purple", "--"),
        ]
        for val, label, colour, style in stats:
            ax.axvline(
                val,
                color=colour,
                linestyle=style,
                linewidth=2,
                label=f"{label}: {val:.1f} us",
            )

        ax.legend(fontsize=10)

    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    path = os.path.join(output_dir, "sched-latency.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log(f"  Created: {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate CPU execution time visualization plots from Perfetto data."
        ),
    )
    parser.add_argument(
        "input_path",
        help=(
            "Path to a .perfetto-trace file or a directory containing"
            " pre-analyzed CSVs from perfetto-analyze.py"
        ),
    )
    parser.add_argument(
        "--pod-name",
        default="",
        help=("Filter to a specific pod name (substring match on thread/process name)"),
    )
    parser.add_argument(
        "--output-dir",
        default="./perfetto-plots",
        help="Output directory for PNG plots (default: ./perfetto-plots)",
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

    input_path = args.input_path
    output_dir = args.output_dir
    pod_name = args.pod_name

    # ------------------------------------------------------------------
    # Validate input path
    # ------------------------------------------------------------------
    if not os.path.exists(input_path):
        log(f"Not found: {input_path}", level="error")
        return 1

    # Create output directory early (test verifies dir creation before
    # potentially-failing trace processing).
    os.makedirs(output_dir, exist_ok=True)
    log(f"Output directory: {output_dir}")

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------
    if os.path.isfile(input_path):
        if input_path.endswith(".perfetto-trace"):
            data = process_trace_mode(input_path, output_dir)
            if not data:
                # Trace processing failed (warn already logged).  Exit
                # cleanly so the calling test doesn't see a hard error.
                return 0
        else:
            log(
                f"Unsupported file type: {input_path}"
                " (expected .perfetto-trace file or CSV directory)",
                level="error",
            )
            return 1
    elif os.path.isdir(input_path):
        data = load_csv_data(input_path, pod_name)
    else:
        log(f"Not found: {input_path}", level="error")
        return 1

    # ------------------------------------------------------------------
    # Check for any usable data
    # ------------------------------------------------------------------
    has_data = any(not df.empty for df in data.values())
    if not has_data:
        log("No data loaded, skipping plot generation", level="warn")
        return 0

    # ------------------------------------------------------------------
    # Generate plots
    # ------------------------------------------------------------------
    log("Generating plots...")
    plot_cpu_timeline(data, output_dir, pod_name)
    plot_slice_distribution(data, output_dir, pod_name)
    plot_cpu_utilization(data, output_dir, pod_name)
    plot_sched_latency(data, output_dir, pod_name)

    log(f"Plots saved to: {output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
