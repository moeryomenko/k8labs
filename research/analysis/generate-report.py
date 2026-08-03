#!/usr/bin/env python3
"""generate-report.py — markdown practical guide from analysis outputs.

Consumes the seven analysis-output CSVs written by the TASK-014/016 analyzers
(weight-share-analyze.py, interaction-heatmap.py, qos-analyze.py,
latency-analyze.py, tunables-analyze.py) and renders ``interaction-report.md``,
a practical guide on the request/limit scheduler interaction.

Usage:
    generate-report.py --input-dir <dir> --output-dir <dir>

Report structure (pinned by the TASK-018 contract, TEST-DESIGN.md section 5):

    # Request/limit scheduler interaction
    ## Weight-share validation
    ## Request x limit interaction heatmap
    ## Throttling region thresholds
    ## QoS guidance
    ## Latency under throttling
    ## Tunables verdict
    ## Burst verdict

Section-presence rule (REQ-2, pinned): the six data-driven sections are never
omitted; when their CSV is missing or has zero data rows they render the exact
marker line ``_no data_`` under the header. The burst verdict section is always
present and always renders the burst-disabled note (``cpu.max.burst`` defaults
to 0 in this cluster per TASK-001; no burst analysis CSV exists in the
pipeline).

Determinism (REQ-4, pinned): no timestamps, no absolute paths, no
env-dependent content; rows are sorted as pinned in section 5; floats render
via ``format(v, "g")``; NaN renders as ``n/a``. Same input dir -> byte-identical
output.

The module is also importable — callers use ``load_table``, ``build_report``
and ``main`` directly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

REPORT_FILENAME = "interaction-report.md"
REPORT_TITLE = "# Request/limit scheduler interaction"

SECTION_TITLES = [
    "Weight-share validation",
    "Request x limit interaction heatmap",
    "Throttling region thresholds",
    "QoS guidance",
    "Latency under throttling",
    "Tunables verdict",
    "Burst verdict",
]

NO_DATA_MARKER = "_no data_"

WEIGHT_SHARE_CSV = "weight-share-summary.csv"
HEATMAP_CSV = "heatmap-throttling_ratio.csv"
QOS_CSV = "qos-summary.csv"
LATENCY_CSV = "latency-summary.csv"
CORRELATION_CSV = "latency-correlation.csv"
TUN_COMPARISON_CSV = "tunables-comparison.csv"
TUN_SIGNIFICANCE_CSV = "tunables-significance.csv"

_REGION_SAFE = 0.25
_REGION_CAUTION = 0.75


def load_table(input_dir: pathlib.Path, filename: str) -> pd.DataFrame | None:
    """Read ``<input_dir>/<filename>`` as a DataFrame.

    Args:
        input_dir: Directory containing the analysis-output CSVs.
        filename: CSV filename under *input_dir*.

    Returns:
        A DataFrame of the CSV rows, or ``None`` when the file does not exist
        (the caller renders ``_no data_``). A header-only CSV yields an empty
        DataFrame, which also counts as no data.
    """
    path = input_dir / filename
    if not path.is_file():
        return None
    return pd.read_csv(path)


def _format_cell(value: object) -> str:
    """Render one table cell deterministically.

    NaN/None render as ``n/a``; floats render via ``format(v, "g")`` (so
    integers like 18000000 stay ``18000000``, never ``1.8e+07``); everything
    else (ints, strings) via ``str()``.
    """
    if value is None or pd.isna(value):
        return "n/a"
    if isinstance(value, float):
        return format(value, "g")
    return str(value)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a standard pipe table with a ``---`` separator row.

    Cells never contain blanks; the header and separator are drawn with no
    surrounding whitespace so the ``|--`` separator is greppable.
    """
    lines = ["|" + "|".join(headers) + "|"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("|" + "|".join(row) + "|")
    return "\n".join(lines)


def _dataframe_table(df: pd.DataFrame, sort_by: list[str]) -> str:
    """Render a non-empty DataFrame as a markdown table, rows sorted.

    Args:
        df: Non-empty DataFrame whose columns become the table header.
        sort_by: Column names to sort by (ascending).

    Returns:
        The pipe-table markdown with one row per input row.
    """
    ordered = df.sort_values(sort_by)
    rows = [[_format_cell(v) for v in row] for row in ordered.itertuples(index=False)]
    return _markdown_table(list(df.columns), rows)


def _no_data_body() -> str:
    """Return the exact marker body for a section with no data."""
    return NO_DATA_MARKER


def _weight_share_section(weight_share: pd.DataFrame | None) -> str:
    """Weight-share validation table, rows sorted by (cell, pod)."""
    if weight_share is None or weight_share.empty:
        return _no_data_body()
    return _dataframe_table(weight_share, ["cell", "pod"])


def _heatmap_section(heatmap: pd.DataFrame | None) -> str:
    """Interaction heatmap: max throttling ratio line plus the pivot table.

    The max is over non-NaN cells; ties resolve to the lowest request then
    limit. Pivot rows are sorted by request ascending, columns by limit
    ascending; NaN renders as ``n/a``.
    """
    if heatmap is None or heatmap.empty:
        return _no_data_body()

    limit_cols = sorted((c for c in heatmap.columns if c != "request"), key=int)

    best: tuple[float, tuple[int, int]] | None = None
    for _, row in heatmap.iterrows():
        for lim_col in limit_cols:
            value = row[lim_col]
            if pd.isna(value):
                continue
            pair = (int(row["request"]), int(lim_col))
            if best is None or value > best[0] or (value == best[0] and pair < best[1]):
                best = (float(value), pair)
    if best is None:
        return _no_data_body()
    best_ratio, best_pair = best

    headline = (
        f"Max throttling ratio: {format(best_ratio, 'g')} "
        f"(request={best_pair[0]}m, limit={best_pair[1]}m)"
    )
    ordered = heatmap.sort_values("request")
    rows = [
        [_format_cell(row["request"])] + [_format_cell(row[c]) for c in limit_cols]
        for _, row in ordered.iterrows()
    ]
    return headline + "\n\n" + _markdown_table(["request"] + limit_cols, rows)


def _region_section(heatmap: pd.DataFrame | None) -> str:
    """Throttling region thresholds table for every non-NaN heatmap cell.

    Regions: safe (ratio < 0.25), caution (0.25 <= ratio < 0.75), throttled
    (ratio >= 0.75). Rows sorted by request then limit.
    """
    if heatmap is None or heatmap.empty:
        return _no_data_body()

    prose = (
        "Regions classify each heatmap cell by throttling ratio: "
        "safe (ratio < 0.25), caution (0.25 <= ratio < 0.75), "
        "throttled (ratio >= 0.75)."
    )
    limit_cols = sorted((c for c in heatmap.columns if c != "request"), key=int)

    cells: list[tuple[int, int, str]] = []
    for _, row in heatmap.iterrows():
        for lim_col in limit_cols:
            value = row[lim_col]
            if pd.isna(value):
                continue
            if value < _REGION_SAFE:
                region = "safe"
            elif value < _REGION_CAUTION:
                region = "caution"
            else:
                region = "throttled"
            cells.append((int(row["request"]), int(lim_col), region))
    if not cells:
        return _no_data_body()

    cells.sort(key=lambda triple: (triple[0], triple[1]))
    rows = [[str(request), str(limit), region] for request, limit, region in cells]
    return prose + "\n\n" + _markdown_table(["request", "limit", "region"], rows)


def _qos_priority(slice_name: str) -> int:
    """Map a kubepods slice name to its QoS ordering key.

    Returns:
        ``0`` for guaranteed, ``1`` for burstable, ``2`` for besteffort, and
        ``3`` for anything unrecognized.
    """
    lowered = slice_name.lower()
    if "guaranteed" in lowered:
        return 0
    if "burstable" in lowered:
        return 1
    if "besteffort" in lowered:
        return 2
    return 3


def _qos_section(qos: pd.DataFrame | None) -> str:
    """QoS guidance table, rows ordered guaranteed, burstable, besteffort."""
    if qos is None or qos.empty:
        return _no_data_body()
    ordered = qos.copy()
    ordered["_priority"] = ordered["qos_slice"].map(_qos_priority)
    ordered = ordered.sort_values(["_priority", "qos_slice", "pod"])
    ordered = ordered.drop(columns=["_priority"])
    rows = [[_format_cell(v) for v in row] for row in ordered.itertuples(index=False)]
    return _markdown_table(list(qos.columns), rows)


def _latency_section(
    latency: pd.DataFrame | None, correlation: pd.DataFrame | None
) -> str:
    """Latency table (sorted by cell) plus correlation metrics as bullets."""
    parts: list[str] = []
    if latency is None or latency.empty:
        parts.append(_no_data_body())
    else:
        parts.append(_dataframe_table(latency, ["cell"]))
    if correlation is not None and not correlation.empty:
        ordered = correlation.sort_values("metric")
        bullets = [
            f"- {_format_cell(metric)}: {_format_cell(corr)}"
            for metric, corr in zip(
                ordered["metric"], ordered["correlation"], strict=True
            )
        ]
        parts.append("Correlation with throttled time:\n" + "\n".join(bullets))
    return "\n\n".join(parts)


def _tunables_section(
    significance: pd.DataFrame | None, comparison: pd.DataFrame | None
) -> str:
    """Tunables verdict table from the significance CSV (+ comparison context).

    The last column is the verdict cell: ``significant`` / ``not significant``
    derived from the boolean ``significant`` column. Rows sorted by tunable.
    """
    if significance is None or significance.empty:
        return _no_data_body()

    value_cols = [
        "tunable",
        "mean_p99",
        "default_mean_p99",
        "diff_p99",
        "noise_threshold",
    ]
    work = significance[value_cols + ["significant"]].copy()
    if comparison is not None and not comparison.empty:
        context = comparison[["tunable", "mean_slice_us", "std_slice_us", "n"]].copy()
        work = work.merge(context, on="tunable", how="left")
        value_cols = value_cols + ["mean_slice_us", "std_slice_us", "n"]

    work = work.sort_values("tunable")
    rows: list[list[str]] = []
    for _, row in work.iterrows():
        cells = [_format_cell(row[col]) for col in value_cols]
        verdict = "significant" if bool(row["significant"]) else "not significant"
        rows.append(cells + [verdict])
    return _markdown_table(value_cols + ["verdict"], rows)


def _burst_section() -> str:
    """Static burst verdict note (cpu.max.burst defaults to 0 on this cluster)."""
    return (
        "Burst is disabled: `cpu.max.burst` defaults to 0 on this cluster, "
        "so no burst credit is available and throttled workloads cannot "
        "absorb latency spikes with burst capacity."
    )


def build_report(input_dir: pathlib.Path) -> str:
    """Assemble the complete markdown report from the CSVs under *input_dir*.

    Pure and deterministic: the same input dir always yields byte-identical
    output. All seven pinned sections are present in order; the six
    data-driven sections render ``_no data_`` when their CSV is missing or
    empty, and the burst verdict is always the static disabled note.

    Args:
        input_dir: Directory containing the analysis-output CSVs.

    Returns:
        The full ``interaction-report.md`` content.
    """
    weight_share = load_table(input_dir, WEIGHT_SHARE_CSV)
    heatmap = load_table(input_dir, HEATMAP_CSV)
    qos = load_table(input_dir, QOS_CSV)
    latency = load_table(input_dir, LATENCY_CSV)
    correlation = load_table(input_dir, CORRELATION_CSV)
    comparison = load_table(input_dir, TUN_COMPARISON_CSV)
    significance = load_table(input_dir, TUN_SIGNIFICANCE_CSV)

    bodies = [
        _weight_share_section(weight_share),
        _heatmap_section(heatmap),
        _region_section(heatmap),
        _qos_section(qos),
        _latency_section(latency, correlation),
        _tunables_section(significance, comparison),
        _burst_section(),
    ]
    sections = "\n\n".join(
        f"## {title}\n\n{body}"
        for title, body in zip(SECTION_TITLES, bodies, strict=True)
    )
    return REPORT_TITLE + "\n\n" + sections + "\n"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build the report and write ``interaction-report.md``.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success (including empty/missing CSVs), 1 when ``--input-dir``
        does not exist, 2 from argparse for missing/invalid flags.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Generate the request/limit scheduler interaction markdown report "
            "from analysis-output CSVs."
        ),
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Directory containing the analysis-output CSVs",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory for interaction-report.md"
    )
    args = parser.parse_args(argv)

    input_dir = pathlib.Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"error: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    report = build_report(input_dir)

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / REPORT_FILENAME).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
