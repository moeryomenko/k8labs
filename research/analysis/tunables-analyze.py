#!/usr/bin/env python3
"""tunables-analyze.py — tunable sweep comparison under contention.

For Family F experiments (tunables-contention.yaml, one cell per tunable set),
group per-replicate p99 latency and mean slice duration by tunable and compare
each tunable against the ``default`` set; emit ``tunables-comparison.csv`` and
``tunables-significance.csv``.

Usage:
    tunables-analyze.py --data-dir <dir> --output-dir <dir>

Math (pinned by the TASK-016 contract, TEST-DESIGN.md section 6):

    mean_p99 / std_p99          = mean / sample std (ddof=1) of per-replicate
                                  p99 latency (latency_stats reuse)
    mean_slice_us / std_slice_us = mean / sample std of per-replicate mean
                                  ``duration_us`` from eevdf-slices.csv
    n                           = number of complete replicates (both files
                                  parse)
    diff_p99                    = mean_p99 - default_mean_p99   (signed)
    noise_threshold             = max(std_p99_tunable, std_p99_default)
    significant                 = abs(diff_p99) > noise_threshold (strict)

A replicate counts only when BOTH latency.csv and eevdf-slices.csv parse;
missing files shrink ``n``. Rows are ordered with ``default`` first, then the
remaining tunables alphabetically. No ``default`` group -> the significance
table is header-only and main warns. PNG rendering is lazy and non-fatal.

The module is also importable — callers use ``load_summary``,
``discover_replicates``, ``build_comparison`` and ``build_significance``
directly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

from latency_stats import percentiles_from_csv

OUTPUT_CSV = "tunables-comparison.csv"
SIGNIFICANCE_CSV = "tunables-significance.csv"
OUTPUT_PNG = "tunables-p99.png"
OUTPUT_COLUMNS = [
    "tunable",
    "mean_p99",
    "std_p99",
    "mean_slice_us",
    "std_slice_us",
    "n",
]
SIGNIFICANCE_COLUMNS = [
    "tunable",
    "mean_p99",
    "default_mean_p99",
    "diff_p99",
    "noise_threshold",
    "significant",
]
DEFAULT_TUNABLE = "default"


def load_summary(data_dir: pathlib.Path) -> pd.DataFrame:
    """Read ``<data_dir>/summary.csv`` with the runner's 8-column schema.

    Args:
        data_dir: Directory containing the experiment's summary.csv.

    Returns:
        A DataFrame of summary rows: ``cell_label, replicate, nr_periods,
        nr_throttled, throttled_usec, usage_usec, cpu_weight, cpu_max``.

    Raises:
        FileNotFoundError: If ``<data_dir>/summary.csv`` does not exist, with
            a message naming the missing path.
    """
    summary_path = data_dir / "summary.csv"
    if not summary_path.is_file():
        raise FileNotFoundError(f"summary.csv not found: {summary_path}")
    return pd.read_csv(summary_path)


def discover_replicates(
    data_dir: pathlib.Path, summary_df: pd.DataFrame
) -> dict[str, list[pathlib.Path]]:
    """Map each tunable to its replicate directories, sorted by path.

    For Family F the cell_label IS the tunable set name. Replicate directories
    are the direct ``replicate-*`` children of ``<data-dir>/<tunable>/``.

    Args:
        data_dir: Experiment data root (summary.csv lives here).
        summary_df: Rows read from summary.csv.

    Returns:
        Tunable name -> sorted replicate directory paths.
    """
    found: dict[str, list[pathlib.Path]] = {}
    for tunable in summary_df["cell_label"].unique():
        rep_dirs = sorted(
            p for p in (data_dir / str(tunable)).glob("replicate-*") if p.is_dir()
        )
        found[str(tunable)] = rep_dirs
    return found


def _replicate_metrics(replicate_dir: pathlib.Path) -> tuple[float, float] | None:
    """Return ``(p99, mean_slice_us)`` for one replicate, or None.

    A replicate counts only when BOTH latency.csv and eevdf-slices.csv parse:
    the p99 percentile comes from latency.csv (latency_stats reuse) and the
    mean slice duration from the mean of ``duration_us`` in eevdf-slices.csv.

    Args:
        replicate_dir: A ``replicate-<N>`` directory.

    Returns:
        The ``(p99, mean_slice_us)`` pair, or ``None`` when either file is
        missing or unparseable.
    """
    latency_path = replicate_dir / "latency.csv"
    slices_path = replicate_dir / "eevdf-slices.csv"
    if not (latency_path.is_file() and slices_path.is_file()):
        return None
    try:
        stats = percentiles_from_csv(latency_path)
        slice_frame = pd.read_csv(slices_path)
        mean_slice = float(slice_frame["duration_us"].mean())
    except Exception:
        return None
    return float(stats[99.0]), mean_slice


def _tunable_sort_key(tunable: str) -> tuple[int, str]:
    """Ordering key for comparison rows: ``default`` first, then alphabetical.

    Args:
        tunable: Tunable set name.

    Returns:
        A sort key where ``default`` sorts before every other tunable.
    """
    return (0, "") if tunable == DEFAULT_TUNABLE else (1, tunable)


def build_comparison(
    summary_df: pd.DataFrame,
    replicate_dirs_by_cell: dict[str, list[pathlib.Path]],
) -> pd.DataFrame:
    """Group per-replicate stats by tunable set.

    One row per tunable with columns ``tunable, mean_p99, std_p99,
    mean_slice_us, std_slice_us, n`` in that order. Group stats are mean /
    sample std (``ddof=1``, pandas default) across complete replicates; ``n``
    is the count of complete replicates. Rows are ordered with ``default``
    first, then the remaining tunables alphabetically. Empty summary -> an
    empty DataFrame with the pinned columns.

    Args:
        summary_df: Rows read from summary.csv.
        replicate_dirs_by_cell: Tunable -> replicate directories.

    Returns:
        The comparison table.
    """
    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(OUTPUT_COLUMNS))

    rows: list[dict[str, object]] = []
    for tunable in sorted(replicate_dirs_by_cell, key=_tunable_sort_key):
        p99s: list[float] = []
        slice_means: list[float] = []
        for rep_dir in replicate_dirs_by_cell[tunable]:
            metrics = _replicate_metrics(rep_dir)
            if metrics is not None:
                p99, mean_slice = metrics
                p99s.append(p99)
                slice_means.append(mean_slice)
        if not p99s:
            continue
        rows.append(
            {
                "tunable": tunable,
                "mean_p99": pd.Series(p99s).mean(),
                "std_p99": pd.Series(p99s).std(ddof=1),
                "mean_slice_us": pd.Series(slice_means).mean(),
                "std_slice_us": pd.Series(slice_means).std(ddof=1),
                "n": len(p99s),
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(OUTPUT_COLUMNS))


def build_significance(comparison_df: pd.DataFrame) -> pd.DataFrame:
    """Compare each tunable's mean p99 against the ``default`` group.

    Columns ``tunable, mean_p99, default_mean_p99, diff_p99,
    noise_threshold, significant``. ``diff_p99`` is the signed difference
    ``mean_p99 - default_mean_p99``; ``noise_threshold`` is the larger of the
    two ``std_p99`` values; ``significant`` is ``abs(diff_p99) >
    noise_threshold`` (strict, so an exactly-at-threshold difference is NOT
    significant). No ``default`` row -> an empty DataFrame with the pinned
    columns.

    Args:
        comparison_df: Table from :func:`build_comparison`.

    Returns:
        The significance table.
    """
    default_rows = comparison_df[comparison_df["tunable"] == DEFAULT_TUNABLE]
    if default_rows.empty:
        return pd.DataFrame(columns=pd.Index(SIGNIFICANCE_COLUMNS))

    default_mean = float(default_rows.iloc[0]["mean_p99"])
    default_std = float(default_rows.iloc[0]["std_p99"])

    rows: list[dict[str, object]] = []
    for _, row in comparison_df.iterrows():
        if row["tunable"] == DEFAULT_TUNABLE:
            continue
        mean = float(row["mean_p99"])
        diff = mean - default_mean
        threshold = max(float(row["std_p99"]), default_std)
        rows.append(
            {
                "tunable": str(row["tunable"]),
                "mean_p99": mean,
                "default_mean_p99": default_mean,
                "diff_p99": diff,
                "noise_threshold": threshold,
                "significant": abs(diff) > threshold,
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(SIGNIFICANCE_COLUMNS))


def _render_tunables_png(comparison: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Render mean p99 per tunable as a PNG (lazy, non-fatal).

    Args:
        comparison: Table from :func:`build_comparison`.
        output_path: Destination PNG path.
    """
    if comparison.empty:
        print("warn: no comparison data; skipping PNG", file=sys.stderr)
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"warn: matplotlib unavailable ({exc}); skipping PNG", file=sys.stderr)
        return

    try:
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.bar(comparison["tunable"], comparison["mean_p99"], color="steelblue")
        ax.set_ylabel("mean p99 latency (ms)")
        ax.set_title("mean p99 latency by tunable")
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
    except Exception as exc:
        print(f"warn: tunables PNG failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the comparison + significance CSVs.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including a missing ``default`` group), 1 on a missing
        data dir or summary.csv, 2 from argparse for invalid flags. PNG
        rendering failures never change the exit code.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compare tunable sweep results against the default set from "
            "Family F summary data, latency.csv and eevdf-slices.csv."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory containing summary.csv"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for tunables-comparison.csv and tunables-significance.csv",
    )
    args = parser.parse_args(argv)

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    try:
        summary = load_summary(data_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    replicate_dirs = discover_replicates(data_dir, summary)
    comparison = build_comparison(summary, replicate_dirs)
    significance = build_significance(comparison)

    if significance.empty and not comparison.empty:
        print(
            f"warn: no {DEFAULT_TUNABLE!r} tunable found; significance table is empty",
            file=sys.stderr,
        )

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    comparison.to_csv(output_dir / OUTPUT_CSV, index=False)
    significance.to_csv(output_dir / SIGNIFICANCE_CSV, index=False)
    _render_tunables_png(comparison, output_dir / OUTPUT_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
