#!/usr/bin/env python3
"""qos-analyze.py — QoS hierarchy competition analysis.

For Family C experiments (qos-hierarchy.yaml, co-located guaranteed /
burstable / besteffort pods), compare each QoS class's achieved usage share
with the cgroup hierarchy weight model and emit ``qos-summary.csv``.

Usage:
    qos-analyze.py --data-dir <dir> --output-dir <dir>

Math (pinned by the TASK-016 contract, TEST-DESIGN.md section 4):

    qos             = cell_label up to the first '-'
    cell            = remainder of the cell_label
    achieved_share  = sum(usage_usec for class) / sum(usage_usec for cell)
    throttled_usec  = sum(throttled_usec for class)    (across replicates)

Aggregation is sum-then-divide across replicates (never the mean of
per-replicate shares). The ``qos_slice`` / ``pod`` / ``cpu_weight`` columns
come from the per-cell cgroup-hierarchy-<node>.json snapshot (TASK-009
schema): the slice named ``kubepods-<qos>.slice``, its first pod entry, and
that pod's ``cpu_weight``. A class with no matching slice is omitted from the
table; a cell with no hierarchy JSON is skipped with a warning (never a
crash). The JSON pod weights are verified against the summary ``cpu_weight``
column and mismatches are reported as warnings.

PNG rendering is lazy and non-fatal: matplotlib is imported only inside the
render function, and any failure warns to stderr while the CSV is already
written and the exit code stays 0.

The module is also importable — callers use ``load_summary``,
``discover_hierarchy_files``, ``load_hierarchy``, ``build_qos_table``,
``qos_achieved_shares`` and ``verify_hierarchy_weights`` directly.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pandas as pd

OUTPUT_CSV = "qos-summary.csv"
OUTPUT_PNG = "qos-share.png"
OUTPUT_COLUMNS = [
    "cell",
    "qos_slice",
    "pod",
    "cpu_weight",
    "achieved_share",
    "throttled_usec",
]
QOS_PRIORITY = ("guaranteed", "burstable", "besteffort")


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


def _split_qos_label(
    label: str, known_cells: set[str] | None = None
) -> tuple[str, str]:
    """Split a runner cell_label into ``(qos, cell)``.

    The runner writes ``cell_label = "<qos>-<cell>"`` where ``<cell>`` is the
    full matrix cell string that NAMES the directory. When *known_cells* is
    given, the cell is the longest known cell directory name the label ends
    with and the qos class is the text before it (suffix match — robust to
    dash-named pods). Without a match (or without *known_cells*) the legacy
    first-dash split applies. A label without a ``-`` denotes qos == cell ==
    the whole label.

    Args:
        label: A cell_label column value from summary.csv.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        The ``(qos, cell)`` tuple.
    """
    if known_cells:
        for cell in sorted(known_cells, key=len, reverse=True):
            prefix = "-" + cell
            if label.endswith(prefix):
                return label[: -len(prefix)], cell
    if "-" in label:
        qos, _, cell = label.partition("-")
        return qos, cell
    return label, label


def _summary_cells(
    summary_df: pd.DataFrame, known_cells: set[str] | None = None
) -> set[str]:
    """Return the set of cell names present in the summary."""
    return {
        _split_qos_label(str(label), known_cells)[1]
        for label in summary_df["cell_label"]
    }


def _slice_by_qos(hierarchy: dict) -> dict[str, dict]:
    """Map QoS class name -> slice entry from a hierarchy snapshot.

    A slice entry qualifies when its name is exactly ``kubepods-<qos>.slice``;
    the QoS class is the text between ``kubepods-`` and ``.slice``. A slice
    named ``kubepods-pod<uid>.slice`` is a direct TRUE Guaranteed pod slice
    (systemd cgroup driver: a Guaranteed pod has no
    ``kubepods-guaranteed.slice`` wrapper) and is attributed to QoS class
    ``guaranteed``. When both the wrapper slice and a direct pod slice are
    present, the later slice in the snapshot (find order) wins — the pinned
    contract only fixes the direct-slice layout that actually occurs.

    Args:
        hierarchy: Parsed cgroup-hierarchy JSON.

    Returns:
        QoS class name -> slice dict.
    """
    result: dict[str, dict] = {}
    for slice_ in hierarchy.get("slices", []):
        name = slice_.get("name", "")
        if not (name.startswith("kubepods-") and name.endswith(".slice")):
            continue
        qos = name[len("kubepods-") : -len(".slice")]
        if qos.startswith("pod"):
            qos = "guaranteed"
        result[qos] = slice_
    return result


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


def discover_hierarchy_files(
    data_dir: pathlib.Path,
    summary_df: pd.DataFrame,
    known_cells: set[str] | None = None,
) -> dict[str, pathlib.Path]:
    """Map each summary cell to its cgroup-hierarchy-*.json snapshot.

    For every cell named in *summary_df* (resolved via *known_cells*, or via
    the cell dirs discovered from *data_dir* when not given, falling back to
    the first-dash label split), the first match of the recursive glob
    ``<data-dir>/**/<cell>/**/cgroup-hierarchy-*.json`` is returned. The glob
    covers BOTH layouts: snapshots nested at ``replicate-<N>/`` (real layout)
    and direct children of the cell dir (flat fixtures). Cells with no
    snapshot anywhere under *data_dir* are absent from the result (REQ-2).

    Args:
        data_dir: Experiment data root (summary.csv lives here).
        summary_df: Rows read from summary.csv.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        Cell name -> snapshot path, one entry per discoverable cell.
    """
    if known_cells is None:
        known_cells = _discover_cell_dirs(data_dir)
    found: dict[str, pathlib.Path] = {}
    for cell in sorted(_summary_cells(summary_df, known_cells)):
        matches = sorted(data_dir.glob(f"**/{cell}/**/cgroup-hierarchy-*.json"))
        if matches:
            found[cell] = matches[0]
    return found


def load_hierarchy(path: pathlib.Path) -> dict:
    """Parse a cgroup-hierarchy-<node>.json snapshot.

    Args:
        path: Path to the snapshot JSON file.

    Returns:
        The parsed hierarchy dict (TASK-009 schema: ``node``,
        ``kubepods_slice_weight``, ``slices``).

    Raises:
        ValueError: If the file is not valid JSON (never a silent partial
            parse).
    """
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed hierarchy JSON: {path}: {exc}") from exc


def build_qos_table(
    summary_df: pd.DataFrame,
    hierarchy: dict,
    known_cells: set[str] | None = None,
) -> pd.DataFrame:
    """Build the per-QoS-class table for ONE cell.

    Pure. The caller passes the rows of a single cell (subset by cell) plus
    that cell's hierarchy snapshot. One row per QoS class with columns
    ``cell, qos_slice, pod, cpu_weight, achieved_share, throttled_usec`` in
    that order, sorted by QoS priority (guaranteed, burstable, besteffort).
    ``achieved_share`` is the aggregate-then-divide usage share (zero total
    degrades to 0.0); ``cpu_weight`` comes from the hierarchy JSON pod entry;
    a class with no matching slice is omitted. Cells resolve via *known_cells*
    when given (cell = filesystem directory name), otherwise by the legacy
    first-dash label split.

    Args:
        summary_df: Summary rows for one cell.
        hierarchy: Parsed cgroup-hierarchy JSON for the same cell.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        The QoS table; an empty DataFrame with the pinned columns when
        *summary_df* is empty.
    """
    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(OUTPUT_COLUMNS))

    slice_by_qos = _slice_by_qos(hierarchy)
    df = summary_df.copy()
    df["qos"] = df["cell_label"].map(
        lambda label: _split_qos_label(str(label), known_cells)[0]
    )
    df["cell"] = df["cell_label"].map(
        lambda label: _split_qos_label(str(label), known_cells)[1]
    )

    rows: list[dict[str, object]] = []
    for cell in df["cell"].unique():
        cell_rows = df[df["cell"] == cell]
        usage_by_qos = cell_rows.groupby("qos")["usage_usec"].sum()
        total_usage = usage_by_qos.sum()
        for qos in QOS_PRIORITY:
            slice_ = slice_by_qos.get(qos)
            if slice_ is None or not slice_.get("pods", []):
                continue
            if qos not in usage_by_qos.index:
                continue
            usage = usage_by_qos[qos]
            share = usage / total_usage if total_usage > 0 else 0.0
            class_rows = cell_rows[cell_rows["qos"] == qos]
            rows.append(
                {
                    "cell": cell,
                    "qos_slice": slice_["name"],
                    "pod": slice_["pods"][0].get("name", ""),
                    "cpu_weight": int(slice_["pods"][0].get("cpu_weight", 0)),
                    "achieved_share": share,
                    "throttled_usec": class_rows["throttled_usec"].sum(),
                }
            )
    return pd.DataFrame(rows, columns=pd.Index(OUTPUT_COLUMNS))


def qos_achieved_shares(
    summary_df: pd.DataFrame, known_cells: set[str] | None = None
) -> pd.DataFrame:
    """Compute per-(cell, QoS class) achieved usage share.

    Args:
        summary_df: Rows read from summary.csv (any number of cells).
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        One row per (cell, class) with columns ``cell, qos, achieved_share``;
        an empty DataFrame with those columns when *summary_df* is empty.
    """
    if summary_df.empty:
        return pd.DataFrame(columns=pd.Index(["cell", "qos", "achieved_share"]))

    df = summary_df.copy()
    df["qos"] = df["cell_label"].map(
        lambda label: _split_qos_label(str(label), known_cells)[0]
    )
    df["cell"] = df["cell_label"].map(
        lambda label: _split_qos_label(str(label), known_cells)[1]
    )

    rows: list[dict[str, object]] = []
    for cell in df["cell"].unique():
        cell_rows = df[df["cell"] == cell]
        usage_by_qos = cell_rows.groupby("qos")["usage_usec"].sum()
        total_usage = usage_by_qos.sum()
        for qos in QOS_PRIORITY:
            if qos in usage_by_qos.index:
                share = usage_by_qos[qos] / total_usage if total_usage > 0 else 0.0
                rows.append({"cell": cell, "qos": qos, "achieved_share": share})
    return pd.DataFrame(rows, columns=pd.Index(["cell", "qos", "achieved_share"]))


def verify_hierarchy_weights(
    summary_df: pd.DataFrame,
    hierarchy: dict,
    known_cells: set[str] | None = None,
) -> list[str]:
    """Compare hierarchy JSON pod weights with the summary cpu_weight column.

    For every QoS class present in *summary_df* whose slice exists in the
    hierarchy, the slice's first pod ``cpu_weight`` must equal the summary
    ``cpu_weight`` (first row of the class). Disagreements produce one warning
    string per class; a fully consistent fixture yields an empty list.

    Args:
        summary_df: Rows read from summary.csv.
        hierarchy: Parsed cgroup-hierarchy JSON.
        known_cells: Cell directory names discovered from the filesystem.

    Returns:
        Warning strings, one per mismatching class.
    """
    slice_by_qos = _slice_by_qos(hierarchy)
    df = summary_df.copy()
    df["qos"] = df["cell_label"].map(
        lambda label: _split_qos_label(str(label), known_cells)[0]
    )

    warnings: list[str] = []
    for qos in QOS_PRIORITY:
        rows = df[df["qos"] == qos]
        if rows.empty:
            continue
        slice_ = slice_by_qos.get(qos)
        if slice_ is None or not slice_.get("pods", []):
            continue
        json_weight = int(slice_["pods"][0].get("cpu_weight", 0))
        summary_weight = int(rows.iloc[0]["cpu_weight"])
        if json_weight != summary_weight:
            warnings.append(
                f"hierarchy weight mismatch for qos class {qos!r}: "
                f"summary cpu_weight={summary_weight}, "
                f"JSON pod weight={json_weight}"
            )
    return warnings


def _render_qos_share_png(table: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Render achieved share per QoS slice as a PNG (lazy, non-fatal).

    Args:
        table: QoS table from :func:`build_qos_table`.
        output_path: Destination PNG path.
    """
    if table.empty:
        print("warn: no qos table data; skipping PNG", file=sys.stderr)
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
        labels = list(table["qos_slice"])
        shares = table["achieved_share"].to_numpy(dtype=float)
        ax.bar(labels, shares, color="steelblue")
        ax.set_ylabel("achieved usage share")
        ax.set_title("achieved CPU share by QoS class")
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
    except Exception as exc:
        print(f"warn: QoS share PNG failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and write the QoS summary CSV (plus optional PNG).

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including cells skipped for missing hierarchy JSON), 1
        on a missing data dir or summary.csv, 2 from argparse for invalid
        flags. PNG rendering failures never change the exit code.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Build the QoS hierarchy competition table from Family C summary "
            "data and cgroup-hierarchy snapshots."
        ),
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory containing summary.csv"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for qos-summary.csv and qos-share.png",
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

    known_cells = _discover_cell_dirs(data_dir)
    hierarchy_files = discover_hierarchy_files(data_dir, summary, known_cells)

    tables: list[pd.DataFrame] = []
    for cell in sorted(hierarchy_files):
        cell_rows = summary[
            summary["cell_label"].map(
                lambda label: _split_qos_label(str(label), known_cells)[1] == cell
            )
        ]
        hierarchy = load_hierarchy(hierarchy_files[cell])
        for warning in verify_hierarchy_weights(cell_rows, hierarchy, known_cells):
            print(f"warn: {warning}", file=sys.stderr)
        tables.append(build_qos_table(cell_rows, hierarchy, known_cells))

    for cell in sorted(_summary_cells(summary, known_cells) - set(hierarchy_files)):
        print(
            f"warn: skipping cell {cell!r}: no cgroup-hierarchy-*.json found",
            file=sys.stderr,
        )

    result = (
        pd.concat(tables, ignore_index=True)
        if tables
        else pd.DataFrame(columns=pd.Index(OUTPUT_COLUMNS))
    )

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_dir / OUTPUT_CSV, index=False)
    _render_qos_share_png(result, output_dir / OUTPUT_PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
