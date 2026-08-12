#!/usr/bin/env python3
"""weight-share-analyze.py — achieved vs theoretical CPU share per pod.

For Family A experiments (weight-share.yaml, co-located 2/3-pod cells),
compare each pod's achieved CPU share (usage-based) with its theoretical
share (weight-based) and emit ``weight-share-summary.csv``.

Usage:
    weight-share-analyze.py --data-dir <dir> --output-dir <dir>

Math (pinned by the analysis contract):

    pod             = cell_label up to the first '-'
    cell            = remainder of the cell_label
    achieved_share  = sum(usage_usec for pod) / sum(usage_usec for cell)
    weight_share    = sum(cpu_weight for pod) / sum(cpu_weight for cell)
    ratio_error     = achieved_share - weight_share   (signed)

Aggregation is sum-then-divide across replicates (never the mean of
per-replicate shares). A cell missing any per-pod ``cgroup-<pod>.csv`` in any
replicate is skipped with a warning: the share math uses summary.csv only and
cgroup files act as a data-completeness gate. Zero total usage or weight
yields a 0.0 share instead of a division error.

The module is also importable — callers use ``load_summary``,
``compute_weight_shares`` and ``check_cgroup_completeness`` directly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

OUTPUT_CSV = "weight-share-summary.csv"
OUTPUT_COLUMNS = ["cell", "pod", "achieved_share", "weight_share", "ratio_error"]


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


def _split_cell_label(
    label: str, known_cells: set[str] | None = None
) -> tuple[str, str]:
    """Split a runner cell_label into ``(pod, cell)``.

    The runner writes ``cell_label = "<pod>-<cell>"`` where ``<cell>`` is the
    full matrix cell string that NAMES the directory and ``<pod>`` may itself
    contain dashes (``pod-a``, ``batch-stress``). When *known_cells* is given,
    the cell is the longest known cell directory name the label ends with and
    the pod is the text before it — suffix matching is the only split robust
    to dash-containing pod names. Without a match (or without *known_cells*)
    the legacy first-dash split applies. A label without a ``-`` denotes a
    single-pod cell: pod == cell == the whole label.

    Args:
        label: A cell_label column value from summary.csv.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        The ``(pod, cell)`` tuple.
    """
    if known_cells:
        for cell in sorted(known_cells, key=len, reverse=True):
            prefix = "-" + cell
            if label.endswith(prefix):
                return label[: -len(prefix)], cell
    if "-" in label:
        pod, _, cell = label.partition("-")
        return pod, cell
    return label, label


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


def compute_weight_shares(
    summary_df: pd.DataFrame, known_cells: set[str] | None = None
) -> pd.DataFrame:
    """Compute achieved and theoretical CPU shares per (cell, pod).

    One row per (cell, pod), with columns ``cell, pod, achieved_share,
    weight_share, ratio_error`` in that order, sorted by (cell, pod).
    ``achieved_share`` is the aggregate-then-divide usage share,
    ``weight_share`` the equivalent weight share, and ``ratio_error`` the
    signed difference (achieved - weight). Zero total usage or weight degrades
    to a 0.0 share. Cells resolve via *known_cells* when given (cell =
    filesystem directory name), otherwise by the legacy first-dash label
    split.

    Args:
        summary_df: Rows read from summary.csv.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        The share summary DataFrame; an empty DataFrame with the pinned
        columns when *summary_df* is empty.
    """
    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(OUTPUT_COLUMNS))

    df = summary_df.copy()
    df["pod"] = df["cell_label"].map(
        lambda label: _split_cell_label(label, known_cells)[0]
    )
    df["cell"] = df["cell_label"].map(
        lambda label: _split_cell_label(label, known_cells)[1]
    )

    rows: list[dict[str, float | str]] = []
    combos = df[["cell", "pod"]].drop_duplicates().sort_values(["cell", "pod"])
    for cell, pod in zip(combos["cell"], combos["pod"], strict=False):
        group = df[(df["cell"] == cell) & (df["pod"] == pod)]
        usage_total = df.loc[df["cell"] == cell, "usage_usec"].sum()
        weight_total = df.loc[df["cell"] == cell, "cpu_weight"].sum()
        achieved = group["usage_usec"].sum() / usage_total if usage_total > 0 else 0.0
        weight_share = (
            group["cpu_weight"].sum() / weight_total if weight_total > 0 else 0.0
        )
        rows.append(
            {
                "cell": cell,
                "pod": pod,
                "achieved_share": achieved,
                "weight_share": weight_share,
                "ratio_error": achieved - weight_share,
            }
        )
    return pd.DataFrame(rows, columns=pd.Index(OUTPUT_COLUMNS))


def check_cgroup_completeness(
    data_dir: pathlib.Path, summary_df: pd.DataFrame
) -> set[str]:
    """Return cells missing at least one expected per-pod cgroup.csv.

    For every (cell, replicate, pod) combo present in the summary, the runner
    writes ``cgroup-<pod>.csv`` under a ``replicate-<N>`` directory nested
    below *data_dir*. Combos are matched recursively so *data_dir* may point
    at the experiment root or a run-timestamp subdirectory. Cells resolve
    against the cell directory names discovered from the filesystem (suffix
    match of the cell_label), never a first-dash split — pod names contain
    dashes (``pod-a``). A cell is incomplete when any expected combo has no
    matching file anywhere under *data_dir*.

    Args:
        data_dir: Experiment data root (summary.csv lives here).
        summary_df: Rows read from summary.csv.

    Returns:
        The set of cell identifiers that fail the completeness gate.
    """
    known_cells = _discover_cell_dirs(data_dir)
    expected: dict[str, set[tuple[int, str]]] = {}
    for cell_label, replicate in zip(
        summary_df["cell_label"], summary_df["replicate"], strict=False
    ):
        pod, cell = _split_cell_label(str(cell_label), known_cells)
        expected.setdefault(cell, set()).add((int(replicate), pod))

    actual: set[tuple[int, str]] = set()
    for cgroup_path in data_dir.rglob("cgroup-*.csv"):
        pod = cgroup_path.stem.removeprefix("cgroup-")
        for parent in cgroup_path.parents:
            if parent.name.startswith("replicate-"):
                try:
                    replicate = int(parent.name.removeprefix("replicate-"))
                except ValueError:
                    continue
                actual.add((replicate, pod))
                break

    return {cell for cell, combos in expected.items() if not combos.issubset(actual)}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the weight-share summary CSV.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including cells skipped for missing cgroup files), 1 on
        a missing data dir, 2 from argparse for invalid flags.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compare achieved CPU share (usage) with theoretical weight share "
            "for Family A co-located cells."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory containing summary.csv"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory for weight-share-summary.csv"
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

    incomplete = check_cgroup_completeness(data_dir, summary)
    for cell in sorted(incomplete):
        print(
            f"warn: skipping cell {cell!r}: missing per-pod cgroup.csv "
            "(data incomplete)",
            file=sys.stderr,
        )

    known_cells = _discover_cell_dirs(data_dir)
    cells = summary["cell_label"].map(
        lambda label: _split_cell_label(label, known_cells)[1]
    )
    valid = summary[~cells.isin(incomplete)]

    result = compute_weight_shares(valid, known_cells=known_cells)

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_dir / OUTPUT_CSV, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
