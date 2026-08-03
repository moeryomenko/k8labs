#!/usr/bin/env python3
"""latency-analyze.py — latency interference analysis.

For Family D experiments (latency-interference.yaml, single-workload cells),
compute per-cell p50/p95/p99 latency percentiles (reusing latency_stats) and
join them with the summary throttling stats; emit ``latency-summary.csv`` and
``latency-correlation.csv``.

Usage:
    latency-analyze.py --data-dir <dir> --output-dir <dir>

Math (pinned by the TASK-016 contract, TEST-DESIGN.md section 5):

    p50/p95/p99      = mean across replicates of the per-file percentiles
                       computed by latency_stats.percentiles_from_csv
    throttled_usec   = sum(throttled_usec across replicates)
    usage_usec       = sum(usage_usec across replicates)
    throttling_ratio = sum(nr_throttled) / sum(nr_periods)  (aggregate-then-
                       divide, never the mean of per-replicate ratios)

Percentiles are computed by research/analysis/latency_stats.py — this module
imports it and never reimplements percentile math. A cell is skipped (with a
warning, never a crash) when it has NO usable latency samples: no latency.csv
files, or every file header-only/empty. A present-but-empty latency.csv among
non-empty files contributes (0.0, 0.0, 0.0) to the cell mean (TASK-007
semantics).

The correlation summary is a pandas-native Pearson correlation of each
percentile column with ``throttled_usec``; zero-variance input yields NaN.
PNG rendering is lazy and non-fatal (matplotlib imported only inside the
render function; failures warn to stderr while the CSVs are already written
and the exit code stays 0).

The module is also importable — callers use ``load_summary``,
``discover_latency_csvs``, ``compute_cell_latencies``, ``build_cell_table``
and ``correlation_summary`` directly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

from latency_stats import percentiles_from_csv

OUTPUT_CSV = "latency-summary.csv"
CORRELATION_CSV = "latency-correlation.csv"
OUTPUT_PNG = "latency-vs-throttling.png"
OUTPUT_COLUMNS = [
    "cell",
    "p50",
    "p95",
    "p99",
    "throttled_usec",
    "usage_usec",
    "throttling_ratio",
]
CORRELATION_COLUMNS = ["metric", "correlation"]
CORRELATION_METRICS = (
    ("p50_vs_throttled_usec", "p50"),
    ("p95_vs_throttled_usec", "p95"),
    ("p99_vs_throttled_usec", "p99"),
)


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


def _resolve_latency_cell(label: str, known_cells: set[str]) -> str:
    """Resolve a cell_label to its cell directory name.

    The runner writes ``cell_label = "<pod>-<cell>"`` where ``<pod>`` may
    contain dashes (``ls-api``, ``batch-stress``). The cell is the longest
    known cell directory name the label ends with; when no known cell matches
    (single-pod flat fixtures) the label IS the cell name. First-dash
    splitting is forbidden: ``batch-stress-<cell>`` would split into pod
    ``batch``.

    Args:
        label: A cell_label column value from summary.csv.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        The cell directory name the label belongs to.
    """
    for cell in sorted(known_cells, key=len, reverse=True):
        if label.endswith("-" + cell):
            return cell
    return label


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


def discover_latency_csvs(
    data_dir: pathlib.Path, summary_df: pd.DataFrame
) -> dict[str, list[pathlib.Path]]:
    """Map each summary cell to its latency.csv files, sorted by path.

    Discovery keys are the resolved cell DIR names (from the filesystem), so
    both ``ls-api-<cell>`` and ``batch-stress-<cell>`` summary rows map to the
    same cell. Every ``**/latency.csv`` under ``<data-dir>/**/<cell>/`` is
    collected recursively (covering both ``replicate-<N>/`` nesting and flat
    direct-child layouts) and sorted; a cell without any latency file maps to
    an empty list (REQ-4).

    Args:
        data_dir: Experiment data root (summary.csv lives here).
        summary_df: Rows read from summary.csv.

    Returns:
        Cell dir name -> sorted latency.csv paths (possibly empty).
    """
    cells = _discover_cell_dirs(data_dir)
    label_to_cell = {
        str(label): _resolve_latency_cell(str(label), cells)
        for label in summary_df["cell_label"].unique()
    }
    found: dict[str, list[pathlib.Path]] = {}
    for cell in sorted(set(label_to_cell.values())):
        found[cell] = sorted(data_dir.glob(f"**/{cell}/**/latency.csv"))
    return found


def compute_cell_latencies(
    latency_paths: list[pathlib.Path],
) -> tuple[float, float, float] | None:
    """Compute the per-cell (p50, p95, p99) latency triple.

    Per-file percentiles come from ``latency_stats.percentiles_from_csv``;
    the cell value is the mean across files. An empty file list, or a list
    where every file is header-only/empty (all percentiles 0.0), returns
    ``None`` (the cell is skipped). An empty file among non-empty ones
    contributes ``(0.0, 0.0, 0.0)`` to the mean (TASK-007 semantics).

    Args:
        latency_paths: latency.csv paths for one cell, sorted.

    Returns:
        The ``(p50, p95, p99)`` tuple, or ``None`` when there are no usable
        samples.
    """
    if not latency_paths:
        return None

    triples: list[tuple[float, float, float]] = []
    for path in latency_paths:
        stats = percentiles_from_csv(path)
        triples.append((float(stats[50.0]), float(stats[95.0]), float(stats[99.0])))

    if all(triple == (0.0, 0.0, 0.0) for triple in triples):
        return None

    count = len(triples)
    return (
        sum(triple[0] for triple in triples) / count,
        sum(triple[1] for triple in triples) / count,
        sum(triple[2] for triple in triples) / count,
    )


def build_cell_table(
    summary_df: pd.DataFrame,
    cell_latencies: dict[str, tuple[float, float, float] | None],
) -> pd.DataFrame:
    """Join per-cell latency percentiles with summary throttling stats.

    One row per cell with columns ``cell, p50, p95, p99, throttled_usec,
    usage_usec, throttling_ratio`` in that order, sorted by cell. Cells whose
    latencies are ``None`` are skipped. Summary rows resolve to the cell DIR
    name (suffix match against the *cell_latencies* keys, falling back to the
    label itself), so pod-prefixed labels (``ls-api-<cell>``,
    ``batch-stress-<cell>``) aggregate into one row per cell. Per cell:
    ``throttled_usec`` and ``usage_usec`` are sums across replicates,
    ``throttling_ratio`` is ``sum(nr_throttled) / sum(nr_periods)``
    (aggregate-then-divide; zero periods degrade to NaN).

    Args:
        summary_df: Rows read from summary.csv.
        cell_latencies: Cell dir name -> percentile triple (``None`` values
            skip the cell).

    Returns:
        The cell table; an empty DataFrame with the pinned columns when
        *summary_df* is empty.
    """
    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(OUTPUT_COLUMNS))

    known_cells = set(cell_latencies)
    label_to_cell = {
        str(label): _resolve_latency_cell(str(label), known_cells)
        for label in summary_df["cell_label"]
    }
    df = summary_df.copy()
    df["_cell"] = df["cell_label"].map(lambda label: label_to_cell[str(label)])

    rows: list[dict[str, object]] = []
    for cell, group in df.groupby("_cell"):
        latencies = cell_latencies.get(cell)
        if latencies is None:
            continue
        periods = group["nr_periods"].sum()
        ratio = group["nr_throttled"].sum() / periods if periods > 0 else float("nan")
        rows.append(
            {
                "cell": cell,
                "p50": latencies[0],
                "p95": latencies[1],
                "p99": latencies[2],
                "throttled_usec": group["throttled_usec"].sum(),
                "usage_usec": group["usage_usec"].sum(),
                "throttling_ratio": ratio,
            }
        )
    rows.sort(key=lambda row: str(row["cell"]))
    return pd.DataFrame(rows, columns=pd.Index(OUTPUT_COLUMNS))


def _pearson(series: pd.Series, other: pd.Series) -> float:
    """Pearson correlation with a NaN guard for zero-variance input.

    Args:
        series: First sample series.
        other: Second sample series.

    Returns:
        The correlation coefficient, or NaN when either input has fewer than
        two points or zero variance.
    """
    if len(series) < 2 or series.std() == 0 or other.std() == 0:
        return float("nan")
    return float(series.corr(other))


def correlation_summary(cell_table: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation of each latency metric with throttled_usec.

    Args:
        cell_table: Table from :func:`build_cell_table`.

    Returns:
        One row per metric with columns ``metric, correlation`` in the order
        ``p50_vs_throttled_usec``, ``p95_vs_throttled_usec``,
        ``p99_vs_throttled_usec``. Zero-variance input yields NaN
        correlations (never a crash); an empty *cell_table* yields an empty
        DataFrame with the pinned columns.
    """
    if cell_table.empty:
        return pd.DataFrame(columns=pd.Index(CORRELATION_COLUMNS))

    rows: list[dict[str, object]] = []
    for metric, column in CORRELATION_METRICS:
        rows.append(
            {
                "metric": metric,
                "correlation": _pearson(
                    cell_table[column], cell_table["throttled_usec"]
                ),
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(CORRELATION_COLUMNS))


def _render_latency_png(table: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Render p99 vs throttled_usec scatter as a PNG (lazy, non-fatal).

    Args:
        table: Cell table from :func:`build_cell_table`.
        output_path: Destination PNG path.
    """
    if table.empty:
        print("warn: no latency table data; skipping PNG", file=sys.stderr)
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
        ax.scatter(table["throttled_usec"], table["p99"], color="steelblue")
        ax.set_xlabel("throttled_usec")
        ax.set_ylabel("p99 latency (ms)")
        ax.set_title("p99 latency vs throttled time")
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
    except Exception as exc:
        print(f"warn: latency PNG failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the latency summary + correlation CSVs.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including cells skipped for missing latency data), 1 on
        a missing data dir or summary.csv, 2 from argparse for invalid flags.
        PNG rendering failures never change the exit code.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Build the latency interference summary and throttling correlation "
            "from Family D summary data and latency.csv files."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory containing summary.csv"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for latency-summary.csv and latency-correlation.csv",
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

    latency_files = discover_latency_csvs(data_dir, summary)
    latencies: dict[str, tuple[float, float, float] | None] = {}
    for cell, paths in latency_files.items():
        latencies[cell] = compute_cell_latencies(paths)
        if latencies[cell] is None:
            if paths:
                print(
                    f"warn: skipping cell {cell!r}: no usable latency.csv samples",
                    file=sys.stderr,
                )
            else:
                print(
                    f"warn: skipping cell {cell!r}: no latency.csv files",
                    file=sys.stderr,
                )

    table = build_cell_table(summary, latencies)
    correlation = correlation_summary(table)

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(output_dir / OUTPUT_CSV, index=False)
    correlation.to_csv(output_dir / CORRELATION_CSV, index=False)
    _render_latency_png(table, output_dir / OUTPUT_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
