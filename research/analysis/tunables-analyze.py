#!/usr/bin/env python3
"""tunables-analyze.py — tunable sweep comparison under contention.

For Family F experiments (tunables-contention.yaml, one cell per tunable set),
group per-replicate p99 latency and mean slice duration by tunable and compare
each tunable against the ``default`` set; emit ``tunables-comparison.csv`` and
``tunables-significance.csv``.

Usage:
    tunables-analyze.py --data-dir <dir> --output-dir <dir>

Math (pinned by the TASK-016 contract, TEST-DESIGN.md section 6, plus the
FIX-4 slice-optional hybrid rule, TEST-DESIGN.md section 3.4):

    mean_p99 / std_p99          = mean / sample std (ddof=1) of per-replicate
                                  p99 latency (latency_stats reuse); every
                                  replicate whose latency.csv parses counts
    mean_slice_us / std_slice_us = mean / sample std of per-replicate mean
                                  ``duration_us`` from eevdf-slices.csv; NaN
                                  when no replicate has a parseable slice file
    n                           = latency-parseable replicates when NO slice
                                  file exists for the tunable, otherwise the
                                  number of replicates with BOTH files parse
                                  (legacy rule, backward compatible)
    diff_p99                    = mean_p99 - default_mean_p99   (signed)
    noise_threshold             = max(std_p99_tunable, std_p99_default)
    significant                 = abs(diff_p99) > noise_threshold (strict)

Slice data is optional: the p99-only significance verdict is still emitted
when eevdf-slices.csv is absent everywhere (runs without ``--eevdf``). Rows
are ordered with ``default`` first, then the remaining tunables
alphabetically. No ``default`` group -> the significance table is header-only
and main warns. PNG rendering is lazy and non-fatal.

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


def _discover_cell_dirs(data_dir: pathlib.Path) -> set[str]:
    """Return the cell directory names present under *data_dir*.

    The runner nests per-replicate files under
    ``<data-dir>/<timestamp>/<cell>/replicate-<N>/`` (real layout) or
    ``<data-dir>/<cell>/replicate-<N>/`` (flat fixtures). Every
    ``replicate-*`` directory's parent is a cell directory, so scanning for
    them at any depth yields the cell dir names — never a label split.

    Args:
        data_dir: Experiment data root (summary.csv lives here).

    Returns:
        The set of cell directory names discovered from the filesystem.
    """
    return {p.parent.name for p in data_dir.rglob("replicate-*") if p.is_dir()}


def _tunable_name(cell_dir: str) -> str:
    """Extract the tunable set name from a cell directory name.

    The real layout names cell dirs ``...-tunables=<name>`` while the flat
    layout names them exactly after the tunable (``default``,
    ``base-slice-low``). The trailing ``-tunables=<name>`` token wins when
    present, else the directory name itself — under both layouts the
    ``default`` comparison group is the tunable named exactly ``default``.

    Args:
        cell_dir: A cell directory name.

    Returns:
        The tunable set name.
    """
    marker = "-tunables="
    if marker in cell_dir:
        return cell_dir.rsplit(marker, 1)[1]
    return cell_dir


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

    Cells are discovered from the filesystem (cell dir names under any run
    timestamp) and mapped to tunable names via the trailing ``-tunables=<name>``
    token (or the dir name itself for flat fixtures). Replicate directories
    are the ``replicate-*`` children of each cell dir, found at any nesting
    depth so both ``<data-dir>/<tunable>/replicate-*`` (flat) and
    ``<data-dir>/<timestamp>/<cell>/replicate-*`` (real) layouts work.

    Args:
        data_dir: Experiment data root (summary.csv lives here).
        summary_df: Rows read from summary.csv.

    Returns:
        Tunable name -> sorted replicate directory paths.
    """
    found: dict[str, list[pathlib.Path]] = {}
    for cell_dir in sorted(_discover_cell_dirs(data_dir)):
        tunable = _tunable_name(cell_dir)
        rep_dirs = sorted(
            p
            for p in data_dir.rglob("replicate-*")
            if p.is_dir() and p.parent.name == cell_dir
        )
        found.setdefault(tunable, []).extend(rep_dirs)
    for tunable in found:
        found[tunable].sort()
    return found


def _replicate_p99(replicate_dir: pathlib.Path) -> float | None:
    """p99 latency from ``latency.csv`` in one replicate, or None.

    Args:
        replicate_dir: A ``replicate-<N>`` directory.

    Returns:
        The p99 percentile, or ``None`` when latency.csv is missing or
        unparseable.
    """
    latency_path = replicate_dir / "latency.csv"
    if not latency_path.is_file():
        return None
    try:
        stats = percentiles_from_csv(latency_path)
    except Exception:
        return None
    return float(stats[99.0])


def _replicate_slice_mean(replicate_dir: pathlib.Path) -> float | None:
    """Mean ``duration_us`` from ``eevdf-slices.csv`` in one replicate.

    Args:
        replicate_dir: A ``replicate-<N>`` directory.

    Returns:
        The mean slice duration, or ``None`` when eevdf-slices.csv is missing
        or unparseable.
    """
    slices_path = replicate_dir / "eevdf-slices.csv"
    if not slices_path.is_file():
        return None
    try:
        slice_frame = pd.read_csv(slices_path)
        return float(slice_frame["duration_us"].mean())
    except Exception:
        return None


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
    sample std (``ddof=1``, pandas default). The slice-optional hybrid rule
    (FIX-4, TEST-DESIGN section 3.4) applies per tunable:

    - A replicate always counts toward ``n`` / ``mean_p99`` / ``std_p99``
      when latency.csv parses.
    - ``mean_slice_us`` / ``std_slice_us`` come only from replicates that
      ALSO have a parseable eevdf-slices.csv; when NO replicate has slice data
      they are NaN.
    - When a tunable HAS some eevdf-slices.csv files, the legacy rule
      ``n = replicates with both files parse`` is preserved (backward
      compatible with the pre-FIX-4 degraded-replicate tests).

    Rows are ordered with ``default`` first, then the remaining tunables
    alphabetically. Empty summary -> an empty DataFrame with the pinned
    columns.

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
        p99_by_rep: list[tuple[pathlib.Path, float]] = []
        slice_by_rep: list[tuple[pathlib.Path, float]] = []
        for rep_dir in replicate_dirs_by_cell[tunable]:
            p99 = _replicate_p99(rep_dir)
            if p99 is not None:
                p99_by_rep.append((rep_dir, p99))
            slice_mean = _replicate_slice_mean(rep_dir)
            if slice_mean is not None:
                slice_by_rep.append((rep_dir, slice_mean))
        if not p99_by_rep:
            continue
        if slice_by_rep:
            # legacy rule: n = replicates with BOTH files parse
            p99_dirs = {rep_dir for rep_dir, _ in p99_by_rep}
            slice_dirs = {rep_dir for rep_dir, _ in slice_by_rep}
            both_dirs = p99_dirs & slice_dirs
            p99_values = [p for rep_dir, p in p99_by_rep if rep_dir in both_dirs]
            slice_values = [m for rep_dir, m in slice_by_rep if rep_dir in both_dirs]
        else:
            p99_values = [p for _, p in p99_by_rep]
            slice_values = []
        rows.append(
            {
                "tunable": tunable,
                "mean_p99": pd.Series(p99_values).mean(),
                "std_p99": pd.Series(p99_values).std(ddof=1),
                "mean_slice_us": pd.Series(slice_values).mean(),
                "std_slice_us": pd.Series(slice_values).std(ddof=1),
                "n": len(p99_values),
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
