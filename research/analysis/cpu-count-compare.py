#!/usr/bin/env python3
"""cpu-count-compare.py — 2-CPU vs 4-CPU weight-share comparison.

For the Family A weight-share experiments, compare each cell's per-pod
``ratio_error`` between a 2-vCPU run and a 4-vCPU run and emit:

    cpu-count-compare.csv       per-cell mean |ratio_error| + delta
    cpu-count-detail.csv        per-pod signed ratio_error + delta
    cpu-count-4v-scaled.csv     scaled 4-vCPU block (only with
                                ``--csv-4v-scaled``)
    cpu-count-verdict.txt       verdict line(s), one per line
    cpu-count-compare.png       optional lazy matplotlib plot (non-fatal)

Usage:
    cpu-count-compare.py --csv-2cpu <file> --csv-4cpu <file>
        [--csv-4v-scaled <file>] --output-dir <dir>

Input files are ``weight-share-summary.csv`` outputs from
``weight-share-analyze.py`` (columns ``cell,pod,achieved_share,
weight_share,ratio_error``). ``--csv-4v-scaled`` is OPTIONAL: when provided
the scaled-4v block and its verdict line are emitted; when omitted the block
is skipped with a stderr warning and the exit code stays 0.

Math (pinned by the TASK-V07 contract, TEST-DESIGN.md section 4):

    error_2cpu / error_4cpu = per-cell mean |ratio_error| of the pod rows
    delta                   = error_4cpu - error_2cpu (negative is
                              improvement); NaN when either side is missing
    missing_in              = "both" | "2cpu" | "4cpu" naming the run the
                              cell is present in (one-sided cells keep the
                              missing side NaN, never a crash)

Verdict means are computed over cells present in BOTH runs. Empty (header-only)
input is the repo convention: header-only outputs + a stderr warning, exit 0.
The module is also importable — callers use ``load_summary_csv``,
``build_comparison``, ``build_detail``, ``build_scaled_block``,
``verdict_line`` and ``scaled_verdict_line`` directly.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import numpy as np
import pandas as pd

OUTPUT_COMPARISON_CSV = "cpu-count-compare.csv"
OUTPUT_DETAIL_CSV = "cpu-count-detail.csv"
OUTPUT_SCALED_CSV = "cpu-count-4v-scaled.csv"
OUTPUT_VERDICT_CSV = "cpu-count-verdict.txt"
OUTPUT_PNG = "cpu-count-compare.png"
VERDICT_FORMAT = "mean |ratio_error| {:.3f} -> {:.3f}"
SCALED_VERDICT_FORMAT = "scaled-4v mean |ratio_error| {:.3f}"

COMPARISON_COLUMNS = [
    "cell",
    "ratio_label",
    "error_2cpu",
    "error_4cpu",
    "delta",
    "missing_in",
]
DETAIL_COLUMNS = ["cell", "pod", "ratio_error_2cpu", "ratio_error_4cpu", "delta"]
SCALED_COLUMNS = ["cell", "ratio_label", "error_scaled"]
SUMMARY_COLUMNS = ["cell", "pod", "achieved_share", "weight_share", "ratio_error"]

_MILLICORE_RE = re.compile(r"(\d+)m")


def load_summary_csv(path: pathlib.Path) -> pd.DataFrame:
    """Read a weight-share-summary.csv input file.

    Args:
        path: Path to a ``weight-share-summary.csv`` with the pinned
            5-column schema (``cell,pod,achieved_share,weight_share,
            ratio_error``).

    Returns:
        The summary DataFrame with the pinned column order.

    Raises:
        FileNotFoundError: If *path* does not exist, naming the missing path.
    """
    summary_path = pathlib.Path(path)
    if not summary_path.is_file():
        raise FileNotFoundError(f"weight-share summary not found: {summary_path}")
    return pd.read_csv(summary_path)


def ratio_label(cell: str) -> str:
    """Short human-readable ratio label for a cell.

    The first two millicore tokens (``NNNm``) in *cell* joined with ``/``
    (e.g. ``a=500m;b=500m`` -> ``500/500``). Family A limits are empty, so the
    first two millicore tokens are a_request and b_request. Cells with fewer
    than two tokens fall back to the cell string.

    Args:
        cell: The matrix cell string naming the experiment directory.

    Returns:
        The ``"<a>/<b>"`` ratio label, or *cell* unchanged.
    """
    tokens = _MILLICORE_RE.findall(cell)
    if len(tokens) >= 2:
        return f"{tokens[0]}/{tokens[1]}"
    return cell


def _per_cell_mean_abs_error(df: pd.DataFrame) -> dict[str, float]:
    """Per-cell mean |ratio_error| keyed by cell, for one summary."""
    return {
        str(cell): float(group["ratio_error"].abs().mean())
        for cell, group in df.groupby("cell")
    }


def build_comparison(df_2cpu: pd.DataFrame, df_4cpu: pd.DataFrame) -> pd.DataFrame:
    """Per-cell comparison table between the two runs.

    One row per cell (the union of both runs), sorted by cell, with columns
    ``cell, ratio_label, error_2cpu, error_4cpu, delta, missing_in`` in that
    order. ``error_2cpu``/``error_4cpu`` are the per-cell mean |ratio_error|
    over the pod rows; ``delta`` is ``error_4cpu - error_2cpu``; the missing
    side is NaN. ``missing_in`` is ``"both"`` when the cell is in both runs,
    ``"2cpu"`` when the cell is only in the 2-CPU file and ``"4cpu"`` when it
    is only in the 4-CPU file. An empty input on either side yields a
    header-only DataFrame (repo convention for empty input).

    Args:
        df_2cpu: Summary rows from the 2-CPU run.
        df_4cpu: Summary rows from the 4-CPU run.

    Returns:
        The comparison DataFrame.
    """
    if df_2cpu.empty or df_4cpu.empty:
        return pd.DataFrame(columns=pd.Index(COMPARISON_COLUMNS))

    errors_2cpu = _per_cell_mean_abs_error(df_2cpu)
    errors_4cpu = _per_cell_mean_abs_error(df_4cpu)

    rows: list[dict[str, object]] = []
    for cell in sorted(set(errors_2cpu) | set(errors_4cpu)):
        in_2cpu = cell in errors_2cpu
        in_4cpu = cell in errors_4cpu
        e2 = errors_2cpu.get(cell, np.nan)
        e4 = errors_4cpu.get(cell, np.nan)
        if in_2cpu and in_4cpu:
            missing_in = "both"
            delta = e4 - e2
        elif in_2cpu:
            missing_in = "2cpu"
            delta = np.nan
        else:
            missing_in = "4cpu"
            delta = np.nan
        rows.append(
            {
                "cell": cell,
                "ratio_label": ratio_label(cell),
                "error_2cpu": e2,
                "error_4cpu": e4,
                "delta": delta,
                "missing_in": missing_in,
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(COMPARISON_COLUMNS))


def build_detail(df_2cpu: pd.DataFrame, df_4cpu: pd.DataFrame) -> pd.DataFrame:
    """Per-pod signed ratio_error detail between the two runs.

    One row per (cell, pod) present in either run, sorted by (cell, pod),
    with columns ``cell, pod, ratio_error_2cpu, ratio_error_4cpu, delta`` in
    that order. The signed per-pod ``ratio_error`` from each run and the delta
    between them; the missing side is NaN. An empty input on either side
    yields a header-only DataFrame.

    Args:
        df_2cpu: Summary rows from the 2-CPU run.
        df_4cpu: Summary rows from the 4-CPU run.

    Returns:
        The detail DataFrame.
    """
    if df_2cpu.empty or df_4cpu.empty:
        return pd.DataFrame(columns=pd.Index(DETAIL_COLUMNS))

    err_2cpu = df_2cpu.set_index(["cell", "pod"])["ratio_error"]
    err_4cpu = df_4cpu.set_index(["cell", "pod"])["ratio_error"]

    rows: list[dict[str, object]] = []
    for key in sorted(set(err_2cpu.index) | set(err_4cpu.index)):
        in_2cpu = key in err_2cpu.index
        in_4cpu = key in err_4cpu.index
        v2 = err_2cpu.get(key, np.nan)
        v4 = err_4cpu.get(key, np.nan)
        rows.append(
            {
                "cell": key[0],
                "pod": key[1],
                "ratio_error_2cpu": v2,
                "ratio_error_4cpu": v4,
                "delta": (v4 - v2) if (in_2cpu and in_4cpu) else np.nan,
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(DETAIL_COLUMNS))


def build_scaled_block(df_scaled: pd.DataFrame) -> pd.DataFrame:
    """Scaled 4-vCPU block: per-cell mean |ratio_error| from the scaled run.

    One row per scaled cell, sorted by cell, with columns
    ``cell, ratio_label, error_scaled`` in that order. An empty input yields
    a header-only DataFrame.

    Args:
        df_scaled: Summary rows from the scaled 4-vCPU run.

    Returns:
        The scaled block DataFrame.
    """
    if df_scaled.empty:
        return pd.DataFrame(columns=pd.Index(SCALED_COLUMNS))
    rows = [
        {
            "cell": str(cell),
            "ratio_label": ratio_label(str(cell)),
            "error_scaled": float(group["ratio_error"].abs().mean()),
        }
        for cell, group in df_scaled.groupby("cell")
    ]
    result = pd.DataFrame(rows, columns=pd.Index(SCALED_COLUMNS))
    return result.sort_values("cell").reset_index(drop=True)


def verdict_line(comparison_df: pd.DataFrame) -> str:
    """Format the 2-CPU -> 4-CPU verdict line.

    The means are over cells present in BOTH runs only.

    Args:
        comparison_df: Output of :func:`build_comparison`.

    Returns:
        A line in the pinned ``VERDICT_FORMAT``.
    """
    both = comparison_df[comparison_df["missing_in"] == "both"]
    return VERDICT_FORMAT.format(both["error_2cpu"].mean(), both["error_4cpu"].mean())


def scaled_verdict_line(scaled_df: pd.DataFrame) -> str:
    """Format the scaled-4v verdict line over all scaled cells.

    Args:
        scaled_df: Output of :func:`build_scaled_block`.

    Returns:
        A line in the pinned ``SCALED_VERDICT_FORMAT``.
    """
    return SCALED_VERDICT_FORMAT.format(scaled_df["error_scaled"].mean())


def _render_plot(comparison_df: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Render the optional comparison PNG (lazy matplotlib, never fatal).

    matplotlib is imported lazily inside this function so a broken or missing
    install never blocks the CSV/verdict outputs. Uses the Agg backend (the
    caller's ``MPLBACKEND=Agg`` is respected as well). Plot failures are
    warned about on stderr, not fatal.

    Args:
        comparison_df: Output of :func:`build_comparison`.
        output_path: Destination PNG path.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001 - a broken matplotlib is non-fatal
        print(f"warning: matplotlib unavailable, skipping plot: {exc}", file=sys.stderr)
        return

    plot_df = comparison_df[comparison_df["missing_in"] == "both"].dropna(
        subset=["error_2cpu", "error_4cpu"]
    )
    if plot_df.empty:
        print("warning: no matched cells to plot; skipping PNG", file=sys.stderr)
        return

    try:
        fig, ax = plt.subplots(figsize=(8, 5))
        x = np.arange(len(plot_df))
        width = 0.35
        ax.bar(x - width / 2, plot_df["error_2cpu"], width, label="2-CPU")
        ax.bar(x + width / 2, plot_df["error_4cpu"], width, label="4-CPU")
        ax.set_xticks(x)
        ax.set_xticklabels(plot_df["ratio_label"], rotation=45, ha="right")
        ax.set_ylabel("mean |ratio_error|")
        ax.set_title("Weight-share ratio error: 2-CPU vs 4-CPU")
        ax.legend()
        fig.tight_layout()
        fig.savefig(output_path)
        plt.close(fig)
    except Exception as exc:  # noqa: BLE001 - a plot failure is non-fatal
        print(f"warning: plot failed, skipping PNG: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the comparison outputs.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including empty inputs and a skipped scaled block),
        1 on a missing required input file or an uncreatable output dir,
        2 from argparse for invalid flags.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compare per-cell mean |ratio_error| between a 2-CPU and a 4-CPU "
            "weight-share run, plus an optional scaled 4-vCPU block."
        ),
    )
    parser.add_argument(
        "--csv-2cpu",
        required=True,
        help="weight-share-summary.csv from the 2-CPU run",
    )
    parser.add_argument(
        "--csv-4cpu",
        required=True,
        help="weight-share-summary.csv from the 4-CPU run",
    )
    parser.add_argument(
        "--csv-4v-scaled",
        help="optional weight-share-summary.csv from the scaled 4-vCPU run",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="directory for cpu-count-compare.csv and the other outputs",
    )
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"error: cannot create output directory {output_dir}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        df_2cpu = load_summary_csv(args.csv_2cpu)
        df_4cpu = load_summary_csv(args.csv_4cpu)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if df_2cpu.empty:
        print(
            f"warning: empty 2-CPU input, writing header-only outputs: {args.csv_2cpu}",
            file=sys.stderr,
        )
    if df_4cpu.empty:
        print(
            f"warning: empty 4-CPU input, writing header-only outputs: {args.csv_4cpu}",
            file=sys.stderr,
        )

    comparison = build_comparison(df_2cpu, df_4cpu)
    detail = build_detail(df_2cpu, df_4cpu)

    comparison.to_csv(output_dir / OUTPUT_COMPARISON_CSV, index=False)
    detail.to_csv(output_dir / OUTPUT_DETAIL_CSV, index=False)

    verdict_lines = [verdict_line(comparison)]
    print(verdict_lines[0])

    if args.csv_4v_scaled:
        try:
            df_scaled = load_summary_csv(args.csv_4v_scaled)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if df_scaled.empty:
            print(
                f"warning: empty scaled-4v input, writing header-only scaled "
                f"output: {args.csv_4v_scaled}",
                file=sys.stderr,
            )
        scaled = build_scaled_block(df_scaled)
        scaled.to_csv(output_dir / OUTPUT_SCALED_CSV, index=False)
        scaled_line = scaled_verdict_line(scaled)
        verdict_lines.append(scaled_line)
        print(scaled_line)
    else:
        print(
            "warning: --csv-4v-scaled not provided; skipping scaled-4v block",
            file=sys.stderr,
        )

    (output_dir / OUTPUT_VERDICT_CSV).write_text("\n".join(verdict_lines) + "\n")

    _render_plot(comparison, output_dir / OUTPUT_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
