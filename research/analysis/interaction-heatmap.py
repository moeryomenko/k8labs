#!/usr/bin/env python3
"""interaction-heatmap.py — request x limit interaction heatmap.

For Family B experiments (request-limit-matrix.yaml), build a 2D pivot table
of per-cell throttling ratio or CPU usage: rows are request milliCPU, columns
are limit milliCPU. Emits ``heatmap-<value>.csv`` plus a matplotlib heatmap
``heatmap-<value>.png``.

Usage:
    interaction-heatmap.py --data-dir <dir> --output-dir <dir>
                           [--value {throttling_ratio,usage}]

Cell value semantics (pinned by the TASK-014 contract, TEST-DESIGN.md
section 5):

    throttling_ratio  = mean across replicates of nr_throttled / nr_periods
                        (NaN when nr_periods == 0)
    usage             = mean of usage_usec across replicates

Missing (request, limit) combos are NaN; rows with unparseable cell labels
(Family A prefixes, empty requests, garbage) are skipped with a warning.
PNG rendering is lazy and non-fatal: matplotlib is imported only inside the
render function, and any failure warns to stderr while the CSV is already
written and the exit code stays 0.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import pandas as pd

_LABEL_PATTERN = re.compile(r"request=(\d+)m-limit=(\d+)m")


def parse_cell_label(label: str) -> tuple[int, int] | None:
    """Parse a request-limit cell label into ``(request_m, limit_m)``.

    Args:
        label: A runner cell_label, e.g. ``request=100m-limit=200m``.

    Returns:
        The ``(request, limit)`` tuple in milliCPU, or ``None`` for any label
        that is not exactly ``request=<digits>m-limit=<digits>m`` — Family A
        prefixed labels, empty requests like ``request=-limit=100m``, and
        garbage all yield ``None``.
    """
    match = _LABEL_PATTERN.fullmatch(label)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def build_heatmap(
    summary_df: pd.DataFrame, value: str = "throttling_ratio"
) -> pd.DataFrame:
    """Build the request x limit pivot table.

    Args:
        summary_df: Rows read from summary.csv.
        value: Metric to pivot — ``throttling_ratio`` (default) or ``usage``.

    Returns:
        A DataFrame with a ``request`` column (int, ascending) and one column
        per unique limit (int, ascending). Missing (request, limit) combos are
        NaN. Empty input yields a DataFrame with only the ``request`` column
        and zero rows.

    Raises:
        ValueError: If *value* is not a supported metric name.
    """
    if value not in ("throttling_ratio", "usage"):
        raise ValueError(f"unknown heatmap value: {value!r}")

    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(["request"]))

    parsed = summary_df["cell_label"].map(parse_cell_label)
    parseable = parsed.notna()
    for label in summary_df.loc[~parseable, "cell_label"]:
        print(f"warn: skipping unparseable cell label {label!r}", file=sys.stderr)

    work = summary_df.loc[parseable].copy()
    if work.empty:
        return pd.DataFrame(columns=pd.Index(["request"]))

    work["request"] = parsed.loc[parseable].map(lambda pair: pair[0])
    work["limit"] = parsed.loc[parseable].map(lambda pair: pair[1])

    if value == "throttling_ratio":
        periods = work["nr_periods"]
        valid = periods > 0
        ratios = pd.Series(float("nan"), index=work.index)
        ratios.loc[valid] = work.loc[valid, "nr_throttled"] / periods.loc[valid]
        work["metric"] = ratios
    else:
        work["metric"] = work["usage_usec"]

    pivot = work.pivot_table(
        index="request",
        columns="limit",
        values="metric",
        aggfunc="mean",
        dropna=False,
    )
    return pivot.reset_index()


def _render_heatmap_png(pivot: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Render the pivot table as a PNG heatmap (lazy import, non-fatal).

    Args:
        pivot: Pivot table from :func:`build_heatmap`.
        output_path: Destination PNG path.
    """
    if pivot.shape[0] == 0 or pivot.shape[1] < 2:
        print("warn: no heatmap data; skipping PNG", file=sys.stderr)
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"warn: matplotlib unavailable ({exc}); skipping PNG", file=sys.stderr)
        return

    try:
        matrix = pivot.iloc[:, 1:].to_numpy(dtype=float)
        fig, ax = plt.subplots(figsize=(8, 6))
        image = ax.imshow(matrix, cmap="viridis", aspect="auto")
        ax.set_xticks(range(matrix.shape[1]))
        ax.set_xticklabels(pivot.columns[1:])
        ax.set_yticks(range(matrix.shape[0]))
        ax.set_yticklabels(pivot["request"])
        ax.set_xlabel("limit (millicores)")
        ax.set_ylabel("request (millicores)")
        ax.set_title("request x limit interaction")
        fig.colorbar(image, ax=ax)
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
    except Exception as exc:
        print(f"warn: heatmap PNG failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the heatmap CSV (plus optional PNG).

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success, 1 on a missing data dir, 2 from argparse for invalid
        flags. PNG rendering failures never change the exit code.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Build a request x limit interaction heatmap from Family B summary data."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory containing summary.csv"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for heatmap-<value>.csv and heatmap-<value>.png",
    )
    parser.add_argument(
        "--value",
        choices=("throttling_ratio", "usage"),
        default="throttling_ratio",
        help="Metric to pivot (default: throttling_ratio)",
    )
    args = parser.parse_args(argv)

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    summary_path = data_dir / "summary.csv"
    if not summary_path.is_file():
        print(f"error: summary.csv not found: {summary_path}", file=sys.stderr)
        return 1
    summary = pd.read_csv(summary_path)

    pivot = build_heatmap(summary, value=args.value)

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pivot.to_csv(output_dir / f"heatmap-{args.value}.csv", index=False)
    _render_heatmap_png(pivot, output_dir / f"heatmap-{args.value}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
