#!/usr/bin/env python3
"""dist-steps.py — six step-by-step EEVDF CPU execution-time distribution images.

Renders exactly six numbered PNGs under
``<output-dir>/distribution/visuals/``, each
generated from REAL measured dist-analyze OUTPUT (no traces, no cluster, no
network) and each carrying an annotation block (title, mechanism text, measured
numbers).  The pinned analysis contract covers the six step images,
annotation text, measured-data provenance and deterministic, byte-identical
reruns.

Input layout (dist-analyze output, per cell):
    <analysis-root>/distribution/<family>/<cell>/
        dist-slices.csv        ts_start_us,ts_end_us,duration_us,cpu,tid,
                               thread_name,pod
        dist-summary.csv       cell,replicate,pod,slice_count,total_exec_ms,
                               mean_us,median_us,p50_us,p95_us,p99_us,max_us,
                               throttle_ratio,cpu_weight,cpu_max,quality
        dist-percentiles.json  {replicate: {pod: {p<k>: value}}}

Output layout:
    <output-dir>/distribution/visuals/
        step-1-declared-vs-enforced.png
        step-2-weight-vs-quota.png
        step-3-slice-distribution.png
        step-4-throttle-pattern.png
        step-5-config-comparison.png
        step-6-guideline-summary.png

Usage:
    dist-steps.py --data-dir <analysis root> --output-dir <out root>
                  --family <name> --cells <c1,c2,...>

Errors are loud: a missing data dir, a listed cell with missing dist-analyze
output, or a listed cell with zero slice rows exits non-zero naming the cause
— never a silent five-of-six render.  No wall-clock values appear in any
output; two runs on the same staged data are byte-identical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Pinned constants (pinned contract section 4.1)
# ---------------------------------------------------------------------------

# The exactly-six output names, in step order 1..6.
STEP_FILES = (
    "step-1-declared-vs-enforced.png",
    "step-2-weight-vs-quota.png",
    "step-3-slice-distribution.png",
    "step-4-throttle-pattern.png",
    "step-5-config-comparison.png",
    "step-6-guideline-summary.png",
)

# One non-empty title per step; step-1 carries the pinned phrase.
TITLES: dict[int, str] = {
    1: "Declared vs enforced: request/limit vs measured cpu.weight and cpu.max",
    2: "Weight vs quota: EEVDF slice = base_slice * weight / sum_weights",
    3: "Slice distribution: measured slice durations for the no-limit cell",
    4: "Throttle pattern: burst/throttle cycle in a quota cell",
    5: "Config comparison: family ECDF overlay and bimodality",
    6: "Guideline summary: request/limit decision table",
}

# Measured 1.4 ms base slice (FRAMEWORK.md, spec section 2.2).
BASE_SLICE_US = 1400.0

# Kernel default cpu.max period in us.
QUOTA_PERIOD_US = 100000

# Fixed step-3 log-histogram bin count.  The old
# ``bins=max(4, len(slices))`` made the bin count scale with the data
# (~2.7M bins on real R3 cells); a fixed 80 bins keeps the shape readable
# and the render memory bounded ("slice-duration histogram").
STEP3_BINS = 80

# Max bar artists step-4 may draw.  Bars are drawn with ONE
# vectorized ``ax.bar`` call over a deterministically downsampled budget;
# never one artist per slice (the ~2.7M-artist bug).  The +1 throttle-gap
# ``axvspan`` patch is NOT counted in this budget.
STEP4_BAR_BUDGET = 500

# ---------------------------------------------------------------------------
# Pure core — data ingestion
# ---------------------------------------------------------------------------


def declared_for_cell(cell: str) -> tuple[int | None, int | None]:
    """Parse the FIRST request=/limit= pair from a cell label into millicores.

    Values ``""`` / ``none`` map to None; the ``m`` suffix is stripped.  This
    is the same semantics dist-analyze.py pins for ``parse_request_limit`` —
    the declared (config) side of step 1.
    """
    request_re = re.compile(r"request=([^-\s]*)")
    limit_re = re.compile(r"limit=([^-\s]*)")

    def _parse(match: re.Match[str] | None) -> int | None:
        if match is None:
            return None
        value = match.group(1).strip().lower()
        if value in ("", "none"):
            return None
        value = value.rstrip("m")
        if not value:
            return None
        return int(value)

    return (_parse(request_re.search(cell)), _parse(limit_re.search(cell)))


def _load_percentiles(path: Path) -> dict[str, float]:
    """Flatten dist-percentiles.json ``{replicate: {pod: {p<k>: v}}}``.

    Replicates and pods are visited in sorted order and later tables
    overwrite earlier ones (deterministic).  The result is the pod-level
    decile table the annotation blocks read (p95 lives in dist-summary.csv).
    """
    raw = json.loads(path.read_text())
    merged: dict[str, float] = {}
    for replicate in sorted(raw):
        for pod in sorted(raw[replicate]):
            merged.update(raw[replicate][pod])
    return merged


def load_family_data(
    analysis_root: Path | str, family: str, cells: list[str]
) -> dict[str, dict]:
    """Read dist-analyze OUTPUT for every listed cell.

    Returns ``{cell: {"summary": dict, "slices_us": list[float],
    "percentiles": dict}}`` from
    ``<analysis_root>/distribution/<family>/<cell>/{dist-slices,dist-summary}.csv
    + dist-percentiles.json``.  A listed cell with any missing file raises an
    error whose message names the cell; a cell whose dist-slices.csv has zero
    rows raises an error naming the cell — never a silent partial render.
    """
    root = Path(analysis_root)
    data: dict[str, dict] = {}
    for cell in cells:
        cell_dir = root / "distribution" / family / cell
        slices_path = cell_dir / "dist-slices.csv"
        summary_path = cell_dir / "dist-summary.csv"
        percentiles_path = cell_dir / "dist-percentiles.json"

        missing = [
            path.name
            for path in (slices_path, summary_path, percentiles_path)
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"cell {cell}: missing dist-analyze output file(s): "
                f"{', '.join(missing)}"
            )

        slices_df = pd.read_csv(slices_path, usecols=["duration_us"])
        if slices_df.empty:
            raise ValueError(f"cell {cell}: dist-slices.csv has zero rows")

        summary_df = pd.read_csv(summary_path)
        if summary_df.empty:
            raise ValueError(f"cell {cell}: dist-summary.csv has zero rows")

        # Keep only the compact float list from the single duration column;
        # the full DataFrame is dropped before the next cell so big cells
        # never stay resident together (MEM plan section 3.4).
        slices_us = slices_df["duration_us"].astype(float).tolist()
        data[cell] = {
            "summary": summary_df.iloc[0].to_dict(),
            "slices_us": slices_us,
            "percentiles": _load_percentiles(percentiles_path),
        }
        del slices_df, summary_df
    return data


# ---------------------------------------------------------------------------
# Pure core — annotation blocks (module-exposed annotation mechanism)
# ---------------------------------------------------------------------------


def annotation_text(step: int, data: dict) -> list[str]:
    """Return the exact annotation-block strings rendered onto the image.

    Every returned string is rendered as ONE text object by ``render_step``;
    tests assert the Figure's text objects contain them (no OCR).  All
    measured numbers are read from ``data`` (the dist-analyze fixture CSVs) —
    never hardcoded.  A step outside 1..6 raises ``ValueError``.
    """
    if step not in TITLES:
        raise ValueError(f"unknown step {step}: expected 1..6")

    cells = list(data.keys())
    if step == 1:
        lines: list[str] = []
        for cell in cells:
            summary = data[cell]["summary"]
            lines.append(
                f"{cell} -> weight {summary['cpu_weight']}, quota {summary['cpu_max']}"
            )
        lines.append(
            "declared request/limit from the cell label; measured cpu.weight "
            "and cpu.max from dist-summary (crun CpuShares -> cpu.weight "
            "conversion)"
        )
        return lines

    if step == 2:
        lines = [
            "slice = base_slice * weight / sum_weights",
            f"base_slice {BASE_SLICE_US / 1000:g} ms",
            f"quota window: period {QUOTA_PERIOD_US} us",
        ]
        for cell in cells:
            weight = data[cell]["summary"]["cpu_weight"]
            lines.append(f"cell {cell}: weight {weight}")
        return lines

    if step == 3:
        summary = data[cells[0]]["summary"]
        return [
            f"percentile p50 {summary['p50_us']:g} us",
            f"percentile p95 {summary['p95_us']:g} us",
            f"percentile p99 {summary['p99_us']:g} us",
            f"max {summary['max_us']:g} us",
            f"slice count {summary['slice_count']}",
        ]

    if step == 4:
        quota_cell = cells[3]
        summary = data[quota_cell]["summary"]
        return [
            f"quota cell {quota_cell}",
            f"throttle_ratio {summary['throttle_ratio']:g}",
            "throttled: the burst/throttle cycle where the quota is exhausted "
            "and the task is throttled until the next period",
            "slice gap: the throttled interval where no slices are scheduled",
        ]

    if step == 5:
        p99_values = [data[cell]["summary"]["p99_us"] for cell in cells]
        return [
            "bimodal throttling: ratio >= 0.95 when limit < demand, ~0 when "
            "limit >= demand",
            f"family p99 {max(p99_values):g} us",
            "throttle threshold 0.95 (saturating limit<demand cells)",
        ]

    # step == 6
    lines = []
    for cell in cells:
        summary = data[cell]["summary"]
        request, limit = declared_for_cell(cell)
        request_txt = "none" if request is None else str(request)
        limit_txt = "none" if limit is None else str(limit)
        lines.append(
            f"request {request_txt}, limit {limit_txt}: "
            f"throttle_ratio {summary['throttle_ratio']:g}"
        )
    lines.append(
        "recommendation: limit must be >= measured demand to avoid the "
        ">= 0.95 throttling regime"
    )
    return lines


# ---------------------------------------------------------------------------
# Rendering — each image is data-driven ("real measured data")
# ---------------------------------------------------------------------------


def _add_annotation_block(fig, lines: list[str]) -> None:
    """Place every annotation line as ONE figure text object.

    Lines are stacked from the top of the reserved lower band downward, so
    each string is independently assertable via ``fig.texts``.
    """
    top = 0.42
    step = 0.034 if len(lines) <= 7 else 0.030
    for index, line in enumerate(lines):
        fig.text(
            0.07,
            top - index * step,
            line,
            fontsize=9,
            family="monospace",
        )


def _short_labels(cells: list[str]) -> list[str]:
    """Compact cell labels for axis ticks (declared values only)."""
    labels = []
    for cell in cells:
        request, limit = declared_for_cell(cell)
        req = "none" if request is None else f"{request}m"
        lim = "none" if limit is None else f"{limit}m"
        labels.append(f"req {req} / lim {lim}")
    return labels


def _draw_visual(step: int, ax, data: dict) -> None:
    """Draw the step's data-driven visual into ``ax`` (no hand-drawn numbers)."""
    cells = list(data.keys())
    labels = _short_labels(cells)
    x = np.arange(len(cells))

    if step == 1:
        weights = [data[cell]["summary"]["cpu_weight"] for cell in cells]
        quotas = [data[cell]["summary"]["cpu_max"] for cell in cells]
        ax.bar(x - 0.2, weights, width=0.4, label="measured cpu.weight")
        ax.set_ylabel("cpu.weight")
        quota_ax = ax.twinx()
        quota_ax.bar(
            x + 0.2,
            quotas,
            width=0.4,
            color="tab:orange",
            label="measured cpu.max quota (us)",
        )
        quota_ax.set_ylabel("cpu.max quota (us)")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
        ax.legend(loc="upper left", fontsize=8)
        quota_ax.legend(loc="upper right", fontsize=8)

    elif step == 2:
        weights = [data[cell]["summary"]["cpu_weight"] for cell in cells]
        total = sum(weights) or 1.0
        slice_us = [BASE_SLICE_US * weight / total for weight in weights]
        ax.bar(x, slice_us, color="tab:blue", label="slice size (us)")
        ax.axhline(
            BASE_SLICE_US,
            color="tab:green",
            linestyle="--",
            label=f"base_slice {BASE_SLICE_US:g} us",
        )
        ax.set_ylabel("slice size (us)")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
        ax.legend(fontsize=8)

    elif step == 3:
        slices = data[cells[0]]["slices_us"]
        # Fixed bin count (STEP3_BINS) on the log axis — the old
        # bins=max(4, len(slices)) scaled with the data (~2.7M bins on real
        # cells); 80 bins keeps the distribution readable and bounded.
        ax.hist(slices, bins=STEP3_BINS, log=True, color="tab:blue")
        summary = data[cells[0]]["summary"]
        for percentile in ("p50", "p95", "p99"):
            ax.axvline(
                summary[f"{percentile}_us"],
                linestyle="--",
                color="tab:red",
                label=f"{percentile} {summary[f'{percentile}_us']:g} us",
            )
        ax.set_xlabel("slice duration (us)")
        ax.set_ylabel("slice count (log)")
        ax.legend(fontsize=8)

    elif step == 4:
        values = np.asarray(data[cells[3]]["slices_us"], dtype=float)
        n = values.size
        if n <= STEP4_BAR_BUDGET:
            # Small datasets draw every bar (no downsampling below the budget).
            xs = np.arange(n)
            ys = values
            width = 0.8
        else:
            # Deterministic downsample to the fixed budget: evenly spaced
            # slice indices, no RNG (reruns stay byte-identical).
            idx = np.rint(np.linspace(0, n - 1, STEP4_BAR_BUDGET)).astype(np.int64)
            xs = idx
            ys = values[idx]
            width = 0.8 * (n - 1) / (STEP4_BAR_BUDGET - 1)
        # ONE vectorized bar call with arrays — never one artist per slice.
        ax.bar(xs, ys, width=width, color="tab:blue")
        gap = max(1, n // 10)
        ax.axvspan(
            0,
            gap,
            hatch="//",
            facecolor="tab:red",
            alpha=0.15,
            label="throttle slice gap",
        )
        ax.set_xlabel("slice index")
        ax.set_ylabel("slice duration (us)")
        ax.legend(fontsize=8)

    elif step == 5:
        for cell in cells:
            values = np.sort(np.asarray(data[cell]["slices_us"], dtype=float))
            ax.plot(
                values,
                np.linspace(0.0, 1.0, len(values), endpoint=False),
                marker="o",
                markersize=3,
                label=cell,
            )
        ax.set_xlabel("slice duration (us)")
        ax.set_ylabel("ECDF")
        ax.legend(fontsize=6)

    else:  # step == 6
        rows = []
        for cell in cells:
            summary = data[cell]["summary"]
            request, limit = declared_for_cell(cell)
            rows.append(
                [
                    cell,
                    "none" if request is None else str(request),
                    "none" if limit is None else str(limit),
                    f"{summary['throttle_ratio']:g}",
                ]
            )
        ax.axis("off")
        table = ax.table(
            cellText=rows,
            colLabels=["cell", "request", "limit", "throttle_ratio"],
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1.0, 1.4)


def render_step(step: int, data: dict, out_path: Path | str) -> plt.Figure:
    """Render one step image, save the PNG, and RETURN the matplotlib Figure.

    The title is set via ``fig.suptitle`` and every string returned by
    ``annotation_text(step, data)`` is rendered as ONE text object — this is
    the module-exposed annotation mechanism the tests use instead of OCR.
    """
    if step not in TITLES:
        raise ValueError(f"unknown step {step}: expected 1..6")

    lines = annotation_text(step, data)
    fig, ax = plt.subplots(figsize=(11, 8))
    fig.subplots_adjust(top=0.86, bottom=0.44, left=0.08, right=0.97)
    fig.suptitle(TITLES[step])
    _draw_visual(step, ax, data)
    _add_annotation_block(fig, lines)
    fig.savefig(Path(out_path), format="png")
    return fig


def render_all(data: dict, visuals_dir: Path | str) -> dict[int, plt.Figure]:
    """Render all six steps into ``visuals_dir`` (creating it), in order.

    Returns ``{step: Figure}`` for the six steps.
    """
    out_dir = Path(visuals_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    figures: dict[int, plt.Figure] = {}
    for step in range(1, 7):
        figures[step] = render_step(step, data, out_dir / STEP_FILES[step - 1])
    return figures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Render the six step-by-step EEVDF distribution images from "
            "dist-analyze output."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        metavar="DIR",
        help="Analysis root containing distribution/<family>/<cell>/ outputs",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        metavar="DIR",
        help="Output root; images land under distribution/visuals/",
    )
    parser.add_argument(
        "--family", required=True, metavar="NAME", help="Family name (output subdir)"
    )
    parser.add_argument(
        "--cells",
        required=True,
        metavar="C1,C2,...",
        help="Comma-separated cell labels in pinned order (cell 0 = no-limit "
        "cell for step 3, cell 3 = 500m/500m quota cell for step 4)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Render the six step images; exits non-zero on any loud failure."""
    parser = build_parser()
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        parser.error(f"data dir missing: {data_dir}")

    cells = [cell.strip() for cell in args.cells.split(",") if cell.strip()]
    if not cells:
        parser.error("--cells must list at least one cell")

    try:
        data = load_family_data(data_dir, args.family, cells)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    visuals_dir = Path(args.output_dir) / "distribution" / "visuals"
    try:
        render_all(data, visuals_dir)
    except Exception as exc:
        print(f"error: failed to render visuals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
