#!/usr/bin/env python3
"""Plots for the linux-sched series, as SVG, from the data root.

Two figures the plan asks for:

* the primary outcome per scheduler arm, grouped by paired block, so the
  comparison a block actually makes is visible rather than only its summary;
* run-queue latency and run-queue length histograms per arm, which is the
  scheduler behaviour the plan wants shown for real workloads.

Everything is standard library. The series' analysis core is deliberately
dependency-free and deterministic, and the plots keep that property: SVG is
written directly rather than through a plotting library, so the output is
byte-identical for the same input and needs no fonts or display.

Inputs are read, never written: ``blocks.csv`` for the primary outcomes and the
per-arm observer artifacts for the histograms.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

BLOCKS_FILENAME = "blocks.csv"

# A run-queue histogram line is "<bucket> : <count> |bar|". Both
# `bpf-runqlat` (usecs) and `bpf-runqlen` (length) print that shape, and the
# bucket label differs, so neither is hard-coded.
_HIST_LINE = re.compile(r"^\s*(?P<bucket>[^\s:][^:]*?)\s*:\s*(?P<count>\d+)")

ARM_COLOURS = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#17becf",
)


def read_blocks(path: Path) -> list[dict[str, str]]:
    """Load blocks.csv rows, one primary outcome per scheduler per block."""

    if not path.is_file():
        raise FileNotFoundError(f"no {BLOCKS_FILENAME} at {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("scheduler")]
    if not rows:
        raise ValueError(f"{path} has no data rows")
    return rows


def parse_histogram(text: str) -> list[tuple[str, int]]:
    """Parse a run-queue histogram into (bucket, count) pairs in file order."""

    buckets: list[tuple[str, int]] = []
    for line in text.splitlines():
        match = _HIST_LINE.match(line)
        if not match:
            continue
        bucket = match.group("bucket").strip()
        # The header line ("usecs : count distribution") has no digits, so the
        # count group is what keeps it out.
        buckets.append((bucket, int(match.group("count"))))
    return buckets


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def grouped_bar_svg(
    title: str,
    subtitle: str,
    categories: list[str],
    series: dict[str, list[float]],
    ylabel: str,
) -> str:
    """A grouped bar chart as standalone SVG.

    ``series`` maps an arm name to one value per category; every arm must have
    the same number of values.
    """

    width, height = 960, 540
    left, right, top, bottom = 90, 30, 90, 120
    plot_w = width - left - right
    plot_h = height - top - bottom
    arms = list(series)
    n_cat = len(categories) or 1
    n_arm = len(arms) or 1
    top_value = max((v for values in series.values() for v in values), default=1.0)
    scale_max = top_value if top_value > 0 else 1.0

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-family="sans-serif" font-size="20" '
        f'fill="#111">{_escape(title)}</text>',
        f'<text x="{left}" y="58" font-family="sans-serif" font-size="13" '
        f'fill="#555">{_escape(subtitle)}</text>',
    ]

    # Axes.
    out.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" '
        f'stroke="#333" stroke-width="1"/>'
    )
    out.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" '
        f'y2="{top + plot_h}" stroke="#333" stroke-width="1"/>'
    )

    # Y gridlines and labels.
    for step in range(6):
        frac = step / 5
        y = top + plot_h - frac * plot_h
        value = frac * scale_max
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e5e5e5" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#555">'
            f"{value:.3g}</text>"
        )
    out.append(
        f'<text x="24" y="{top + plot_h / 2:.0f}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12" fill="#333" '
        f'transform="rotate(-90 24 {top + plot_h / 2:.0f})">'
        f"{_escape(ylabel)}</text>"
    )

    # Group geometry.
    group_w = plot_w / n_cat
    bar_gap = 2
    bar_w = max(1.0, (group_w * 0.8) / n_arm - bar_gap)

    for cat_index, category in enumerate(categories):
        group_left = left + cat_index * group_w + group_w * 0.1
        for arm_index, arm in enumerate(arms):
            values = series[arm]
            if cat_index >= len(values):
                continue
            value = values[cat_index]
            bar_h = 0 if scale_max <= 0 else (value / scale_max) * plot_h
            x = group_left + arm_index * (bar_w + bar_gap)
            y = top + plot_h - bar_h
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{bar_h:.1f}" fill="{ARM_COLOURS[arm_index % len(ARM_COLOURS)]}"/>'
            )
        label_x = left + cat_index * group_w + group_w / 2
        out.append(
            f'<text x="{label_x:.1f}" y="{top + plot_h + 18}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#333" '
            f'transform="rotate(-35 {label_x:.1f} {top + plot_h + 18})">'
            f"{_escape(category)}</text>"
        )

    # Legend.
    legend_y = height - 34
    legend_x = left
    for arm_index, arm in enumerate(arms):
        out.append(
            f'<rect x="{legend_x}" y="{legend_y - 10}" width="12" height="12" '
            f'fill="{ARM_COLOURS[arm_index % len(ARM_COLOURS)]}"/>'
        )
        out.append(
            f'<text x="{legend_x + 18}" y="{legend_y}" font-family="sans-serif" '
            f'font-size="12" fill="#333">{_escape(arm)}</text>'
        )
        legend_x += 22 + 8 * len(arm) + 28

    out.append("</svg>")
    return "\n".join(out) + "\n"


def primary_series(
    rows: list[dict[str, str]],
) -> dict[str, dict[str, dict[int, float]]]:
    """workload -> metric -> arm -> value, plus the block a value came from.

    The block is part of the key because a scenario runs several blocks and the
    per-block comparison is the one the protocol makes.
    """

    nested: dict[str, dict[str, dict[int, float]]] = {}
    for row in rows:
        workload = row["workload"]
        metric = row["metric"]
        block = int(row["block_id"])
        nested.setdefault(workload, {}).setdefault(metric, {})[block] = float(
            row["value"]
        )
    return nested


def plot_primary(
    data_root: Path, out_dir: Path, rows: list[dict[str, str]]
) -> list[Path]:
    """One figure per scenario: primary outcome per arm, grouped by block."""

    written: list[Path] = []
    for workload, metrics in sorted(primary_series(rows).items()):
        for metric, by_block in sorted(metrics.items()):
            arms = sorted(
                {row["scheduler"] for row in rows if row["workload"] == workload}
            )
            blocks = sorted(by_block)
            # A block is only comparable if every arm ran in it.
            comparable = [
                block
                for block in blocks
                if all(
                    _arm_value(rows, workload, block, arm) is not None for arm in arms
                )
            ]
            if not comparable:
                continue
            series = {
                arm: [
                    _arm_value(rows, workload, block, arm) or 0.0
                    for block in comparable
                ]
                for arm in arms
            }
            direction = next(
                row.get("direction", "")
                for row in rows
                if row["workload"] == workload and row["metric"] == metric
            )
            svg = grouped_bar_svg(
                f"{workload}: {metric}",
                f"per paired block ({direction}); only blocks with every arm",
                [f"block {block}" for block in comparable],
                series,
                metric,
            )
            path = out_dir / f"primary-{workload}-{metric}.svg"
            path.write_text(svg, encoding="utf-8")
            written.append(path)
    return written


def _arm_value(
    rows: list[dict[str, str]], workload: str, block: int, arm: str
) -> float | None:
    for row in rows:
        if (
            row["workload"] == workload
            and int(row["block_id"]) == block
            and row["scheduler"] == arm
        ):
            return float(row["value"])
    return None


def _histogram_series(
    block_dir: Path, queue: str
) -> tuple[dict[str, list[float]], list[str]]:
    """Collect one histogram series per arm for a block directory.

    Handles the arm-scoped layout (`observer-<arm>/<queue>.txt`, which is what
    the capture writes now) and, so that earlier data still plots, the older
    layout where the artifacts sat at the block level with no arm in the name.
    """

    series: dict[str, list[float]] = {}
    buckets: list[str] = []
    for hist_path in sorted(block_dir.glob(f"observer-*/{queue}.txt")):
        arm = hist_path.parent.name.removeprefix("observer-")
        parsed = parse_histogram(hist_path.read_text(encoding="utf-8"))
        if not parsed:
            continue
        if not buckets:
            buckets = [bucket for bucket, _ in parsed]
        series[arm] = [float(count) for _, count in parsed]

    legacy = block_dir / f"{queue}.txt"
    if not series and legacy.is_file():
        parsed = parse_histogram(legacy.read_text(encoding="utf-8"))
        if parsed:
            buckets = [bucket for bucket, _ in parsed]
            series["(arm not recorded)"] = [float(count) for _, count in parsed]
    return series, buckets


def plot_runq(data_root: Path, out_dir: Path) -> list[Path]:
    """Run-queue histograms per arm, one figure per scenario/block/queue."""

    written: list[Path] = []
    for scenario_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        for block_dir in sorted(scenario_dir.glob("block-*")):
            for queue in ("runqlat", "runqlen"):
                series, buckets = _histogram_series(block_dir, queue)
                if not series or not buckets:
                    continue
                svg = grouped_bar_svg(
                    f"{scenario_dir.name}: {queue}",
                    f"{block_dir.name}, counts per bucket, per scheduler arm",
                    buckets,
                    series,
                    "events",
                )
                path = out_dir / f"{queue}-{scenario_dir.name}-{block_dir.name}.svg"
                path.write_text(svg, encoding="utf-8")
                written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    out_dir = args.output_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = read_blocks(args.data_root / BLOCKS_FILENAME)
    written = plot_primary(args.data_root, out_dir, rows)
    written += plot_runq(args.data_root, out_dir)

    for path in written:
        print(path)
    if not written:
        print(
            "no plots produced: no blocks.csv rows and no observer artifacts",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
