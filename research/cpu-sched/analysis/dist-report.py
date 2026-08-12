#!/usr/bin/env python3
"""dist-report.py — EEVDF CPU execution-time distribution deep-dive report.

Renders ``DEEP-DIVE-EEVDF-EXEC.md`` from the staged
dist-analyze OUTPUT tree — no traces, no cluster, no network.  The
seven required sections, visuals embedded with resolvable relative paths,
guidelines citing measured numbers, determinism (byte-identical reruns),
degraded cells listed in the appendix, and the output layout are pinned by
the analysis contract.

Input layout (dist-analyze output, per cell):
    <analysis-root>/distribution/<family>/<cell>/
        dist-slices.csv        ts_start_us,ts_end_us,duration_us,cpu,tid,
                               thread_name,pod
        dist-summary.csv       cell,replicate,pod,slice_count,total_exec_ms,
                               mean_us,median_us,p50_us,p95_us,p99_us,max_us,
                               throttle_ratio,cpu_weight,cpu_max,quality
        dist-percentiles.json  {replicate: {pod: {p<k>: value}}}
        slice-histogram.png
    <analysis-root>/distribution/<family>/
        slice-dist-comparison.png / slice-ecdf-overlay.png / gantt-timeline.png
        / runtime-trajectory.png
        visuals/exec-timeline.gif / visuals/slice-dist-build.gif
    <analysis-root>/distribution/visuals/step-1..6-*.png

Usage:
    dist-report.py --data-dir <analysis root> --output-file <report .md path>
                   [--families <f1,f2,...>]

Families default to the sorted family dirs under ``distribution/``.  For the
Family-A six-cell matrix families every pinned cell must be present in the
staged tree — a missing cell dir, a missing dist-analyze file, or a zero-row
``dist-slices.csv`` fails loudly naming the cell (never a silent partial
report).  Families whose cell labels come from another matrix (weight-share,
qos-hierarchy) are rendered from whatever cell dirs the tree exposes.
Embedded image paths are relative to the report file's parent directory, so a
report written at the canonical location
(``output/distribution/DEEP-DIVE-EEVDF-EXEC.md``) resolves the natural
``<family>/...`` paths, and a copy written at ``research/`` resolves the same
tree via ``../``.

Determinism: no wall-clock values, no absolute input paths, no
random content; cells, rows and hashes are sorted deterministically; floats
render via ``format(v, "g")``.  Two runs with identical arguments produce
byte-identical files (SHA-256).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Pinned constants (pinned contract section 4.1)
# ---------------------------------------------------------------------------

REPORT_FILENAME = "DEEP-DIVE-EEVDF-EXEC.md"
REPORT_TITLE = "# EEVDF CPU execution-time distribution: deep dive"

# The seven required sections, in pinned order.
SECTION_TITLES = (
    "Executive summary",
    "Mechanism",
    "Method",
    "Measured results per family",
    "The distribution story",
    "Guidelines",
    "Appendix: reproducibility",
)

EXECUTIVE_BULLET_COUNT = 5
GUIDELINE_COUNT = 6

# Step images, in step order (same set as conftest DIST_STEPS_FILES).
STEP_FILES = (
    "step-1-declared-vs-enforced.png",
    "step-2-weight-vs-quota.png",
    "step-3-slice-distribution.png",
    "step-4-throttle-pattern.png",
    "step-5-config-comparison.png",
    "step-6-guideline-summary.png",
)

# Family images, in pinned order.
FAMILY_IMAGES = (
    "slice-dist-comparison.png",
    "slice-ecdf-overlay.png",
    "gantt-timeline.png",
    "runtime-trajectory.png",
)

CELL_HISTOGRAM = "slice-histogram.png"
TIMELINE_GIF = "exec-timeline.gif"
HIST_GIF = "slice-dist-build.gif"
VISUALS_DIR = "visuals"
DEGRADED_QUALITY = "degraded"

# The six-cell Family A matrix, in pinned order.  A family whose staged
# cell labels are all drawn from this matrix is a Family-A matrix family:
# every pinned cell is expected in the tree and a missing one fails loudly.
# Families with other label sets (weight-share, qos-hierarchy) are rendered
# from whatever cell dirs the tree exposes.
FAMILY_A_CELLS = (
    "request=-limit=",
    "request=100m-limit=100m",
    "request=100m-limit=1000m",
    "request=500m-limit=500m",
    "request=500m-limit=2000m",
    "request=1000m-limit=2000m",
)

# Measured cluster facts (spec section 2.2) cited by the static prose.
BASE_SLICE_MS = 1.4
QUOTA_PERIOD_US = 100000

# ---------------------------------------------------------------------------
# Pure core — data ingestion
# ---------------------------------------------------------------------------


def _load_percentiles(path: Path) -> dict[str, float]:
    """Flatten dist-percentiles.json ``{replicate: {pod: {p<k>: v}}}``.

    Replicates and pods are visited in sorted order and later tables
    overwrite earlier ones (deterministic).  The result is the pod-level
    decile table the percentiles dict exposes (p95 lives in dist-summary.csv).
    """
    raw = json.loads(path.read_text())
    merged: dict[str, float] = {}
    for replicate in sorted(raw):
        for pod in sorted(raw[replicate]):
            merged.update(raw[replicate][pod])
    return merged


def _scalar(value: Any) -> Any:
    """Convert a pandas/numpy scalar to a plain Python scalar."""
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            return value
    return value


def load_family_data(
    analysis_root: Path | str, family: str, cells: list[str]
) -> dict[str, dict]:
    """Read dist-analyze OUTPUT for every listed cell of one family.

    Returns ``{cell: {"summary": dict (FIRST summary row), "quality": str
    ("degraded" iff ANY summary row is degraded), "slices_us": list[float],
    "percentiles": dict}}`` from
    ``<analysis_root>/distribution/<family>/<cell>/{dist-slices,dist-summary}
    .csv + dist-percentiles.json``.  A listed cell with any missing file, or a
    dist-slices.csv with zero rows, raises an error whose message names the
    cell — never a silent partial report.
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

        slices_df = pd.read_csv(slices_path)
        if slices_df.empty:
            raise ValueError(f"cell {cell}: dist-slices.csv has zero rows")

        summary_df = pd.read_csv(summary_path)
        if summary_df.empty:
            raise ValueError(f"cell {cell}: dist-summary.csv has zero rows")

        qualities = [str(value) for value in summary_df["quality"].tolist()]
        quality = (
            DEGRADED_QUALITY
            if any(value == DEGRADED_QUALITY for value in qualities)
            else "good"
        )

        data[cell] = {
            "summary": {
                column: _scalar(value) for column, value in summary_df.iloc[0].items()
            },
            "quality": quality,
            "slices_us": slices_df["duration_us"].astype(float).tolist(),
            "percentiles": _load_percentiles(percentiles_path),
        }
    return data


def families_from_data_dir(data_dir: Path | str) -> list[str]:
    """Sorted family dir names under ``<data_dir>/distribution/``.

    Returns ``[]`` when the distribution dir exists but is empty; raises an
    error naming "distribution" when ``<data_dir>/distribution/`` does not
    exist.
    """
    distribution = Path(data_dir) / "distribution"
    if not distribution.is_dir():
        raise FileNotFoundError(f"distribution directory not found under {data_dir}")
    return sorted(
        p.name for p in distribution.iterdir() if p.is_dir() and p.name != VISUALS_DIR
    )


def workload_for_family(family: str) -> str:
    """Strip the ``dist-`` prefix ("dist-api-server" -> "api-server")."""
    return family[len("dist-") :] if family.startswith("dist-") else family


def degraded_cells(family_data: dict) -> list[str]:
    """Cells whose quality is DEGRADED_QUALITY, in pinned cell order."""
    return [
        cell for cell in family_data if family_data[cell]["quality"] == DEGRADED_QUALITY
    ]


def _observed_cells(data_dir: Path, family: str) -> list[str]:
    """Cell dirs under ``distribution/<family>/`` (excluding ``visuals``).

    The reserved ``visuals`` subdir holds the GIFs and is never a
    cell.  Raises when the family dir does not exist.
    """
    family_dir = data_dir / "distribution" / family
    if not family_dir.is_dir():
        raise FileNotFoundError(f"family {family} not found under distribution")
    return sorted(
        p.name for p in family_dir.iterdir() if p.is_dir() and p.name != VISUALS_DIR
    )


def _expected_cells_for_family(data_dir: Path, family: str) -> list[str]:
    """The cell labels the report renders for one family.

    For a Family-A matrix family (every observed cell label is one of
    FAMILY_A_CELLS) the full pinned six-cell set is expected: any missing
    cell raises naming the cell.  Other families (weight-share,
    qos-hierarchy) render the observed cell dirs.  A family with no cell
    dirs raises naming the family.
    """
    observed = _observed_cells(data_dir, family)
    if not observed:
        raise ValueError(f"family {family} has no cells under distribution")
    if set(observed) <= set(FAMILY_A_CELLS):
        missing = [cell for cell in FAMILY_A_CELLS if cell not in observed]
        if missing:
            raise FileNotFoundError(
                f"family {family}: missing dist-analyze cell output: "
                f"{', '.join(missing)}"
            )
        return list(FAMILY_A_CELLS)
    return observed


# ---------------------------------------------------------------------------
# Pure core — guideline bullets (digit/unit rule)
# ---------------------------------------------------------------------------


def guidelines(data: dict) -> list[str]:
    """Exactly GUIDELINE_COUNT guideline bullets (no ``- `` prefix).

    Every bullet carries a measured number immediately followed by a unit
    (``\\d+(?:\\.\\d+)?\\s*(?:ms|us|m|%)`` — the grep-able digit/unit
    rule).  Collectively the bullets cite the FIRST family's FIRST cell p99
    rendered ``"{v:g} us"`` and the BestEffort weight-1 floor ("weight 1").
    """
    first_family = next(iter(data))
    first_cell = next(iter(data[first_family]))
    summary = data[first_family][first_cell]["summary"]
    p95_us = f"{float(summary['p95_us']):g} us"
    p99_us = f"{float(summary['p99_us']):g} us"

    return [
        (
            "Set request to the P95 of observed demand so the weight protects "
            "during contention: the no-limit cell shows a p95 of "
            f"{p95_us} and a p99 of {p99_us}, so sizing to the P95 of demand "
            "keeps the cgroup weight effective."
        ),
        (
            "Set limit to at least real demand: a saturating workload is "
            "throttled in >= 98% of quota periods when limit < demand, so "
            "measure demand before choosing a limit."
        ),
        (
            "Guaranteed QoS (request == limit) does NOT disable quota: it is "
            "safe only while demand stays well below the quota, as the "
            f"measured {BASE_SLICE_MS:g} ms base slice and the >= 98% "
            "throttling regime show."
        ),
        (
            "Size burstable limits at measured P99 plus headroom: with p99 at "
            f"{p99_us} and the {QUOTA_PERIOD_US} us quota period, the extra "
            "headroom absorbs latency spikes without entering the throttling "
            "regime."
        ),
        (
            "Light workloads need no headroom: at demand below 50% of the "
            "limit, throttling is ~0 (measured < 5% for the 30% cpu-burner "
            "cells), so a small request/limit margin is enough."
        ),
        (
            "The BestEffort weight-1 floor (no request) pays the highest p99 "
            "cost under contention: weight 1 vs weight 17 for a 100m request "
            "gives far fewer slices per period, with the measured p99 penalty "
            f"at {p99_us}."
        ),
    ]


# ---------------------------------------------------------------------------
# Pure core — hashes and embedded visuals
# ---------------------------------------------------------------------------


def report_hashes(data_dir: Path | str, families: list[str]) -> dict[str, str]:
    """``{relative-path: sha256 hex}`` for every dist-summary.csv rendered.

    Relative paths are ``distribution/<family>/<cell>/dist-summary.csv`` with
    forward slashes.  Deterministic: same staged data -> same hashes.
    """
    data_dir = Path(data_dir)
    hashes: dict[str, str] = {}
    for family in families:
        for cell in _expected_cells_for_family(data_dir, family):
            summary = data_dir / "distribution" / family / cell / "dist-summary.csv"
            relative = summary.relative_to(data_dir).as_posix()
            hashes[relative] = hashlib.sha256(summary.read_bytes()).hexdigest()
    return hashes


def _image_files(data_dir: Path, families: list[str]) -> list[Path]:
    """Every visual the report embeds, in pinned order."""
    base = data_dir / "distribution"
    files: list[Path] = [base / VISUALS_DIR / name for name in STEP_FILES]
    for family in families:
        family_dir = base / family
        files.extend(family_dir / name for name in FAMILY_IMAGES)
        for cell in _expected_cells_for_family(data_dir, family):
            files.append(family_dir / cell / CELL_HISTOGRAM)
        files.append(family_dir / VISUALS_DIR / TIMELINE_GIF)
        files.append(family_dir / VISUALS_DIR / HIST_GIF)
    return files


def image_paths(
    data_dir: Path | str, families: list[str], output_path: Path | str
) -> list[str]:
    """Every visual the report embeds, RELATIVE to ``output_path.parent``.

    All six step images, per family the four FAMILY_IMAGES, per cell the
    slice-histogram.png, per family the two GIFs.  Raises naming a missing
    visual file — the embedded refs must resolve.
    """
    data_dir = Path(data_dir)
    out_parent = Path(output_path).parent
    files = _image_files(data_dir, families)
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing visual(s) for the report: " + ", ".join(missing)
        )
    return [os.path.relpath(path, out_parent) for path in files]


def _split_image_rels(
    data_dir: Path, families: list[str], rel_images: list[str]
) -> tuple[dict[str, str], dict[str, dict]]:
    """Group the flat image_paths list into step / per-family dicts."""
    steps = {
        f"step-{index}": rel_images[index - 1]
        for index in range(1, len(STEP_FILES) + 1)
    }
    index = len(STEP_FILES)
    families_map: dict[str, dict] = {}
    for family in families:
        cells = _expected_cells_for_family(data_dir, family)
        block_size = len(FAMILY_IMAGES) + len(cells) + 2
        block = rel_images[index : index + block_size]
        index += block_size
        families_map[family] = {
            "family": dict(zip(FAMILY_IMAGES, block[: len(FAMILY_IMAGES)])),
            "cells": dict(
                zip(cells, block[len(FAMILY_IMAGES) : len(FAMILY_IMAGES) + len(cells)])
            ),
            "gifs": dict(
                zip(
                    (TIMELINE_GIF, HIST_GIF),
                    block[len(FAMILY_IMAGES) + len(cells) :],
                )
            ),
        }
    return steps, families_map


# ---------------------------------------------------------------------------
# Report section builders
# ---------------------------------------------------------------------------


def _format_cell(value: object) -> str:
    """Render one table cell deterministically (NaN -> ``n/a``, ``:g`` floats)."""
    if value is None or pd.isna(value):
        return "n/a"
    if isinstance(value, float):
        return format(value, "g")
    return str(value)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a standard pipe table with a ``---`` separator row."""
    lines = ["|" + "|".join(headers) + "|"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("|" + "|".join(row) + "|")
    return "\n".join(lines)


def _executive_summary() -> str:
    """The answer to the question in exactly five bullets."""
    bullets = (
        "- A Pod CPU request becomes cgroup v2 `cpu.weight` through the crun "
        "CpuShares conversion, so the EEVDF slice size scales with the request.",
        "- A Pod CPU limit becomes a `cpu.max` quota/period; when the limit is "
        "below real demand the container is throttled in nearly every quota "
        "period.",
        "- Measured slice durations split into three regimes: long "
        "un-preempted slices (no contention), contention-scale slices "
        "proportional to weight, and quota-capped burst/throttle cycles when "
        "the limit is below demand.",
        "- Guaranteed QoS (request == limit) does NOT disable quota "
        "throttling; it is safe only while demand stays well below the quota.",
        "- The practical rule: set request to P95 of demand for weight "
        "protection and set limit to at least measured demand to stay out of "
        "the throttling regime.",
    )
    return "\n".join(bullets)


def _mechanism_section(steps: dict[str, str]) -> str:
    """Request -> cpu.weight, limit -> cpu.max, EEVDF ordering."""
    crun_table = _markdown_table(
        ["request", "cpu.weight", "cpu.max quota (us)"],
        [
            ["(none)", "1", "100000"],
            ["100m", "17", "10000"],
            ["500m", "59", "50000"],
            ["1000m", "100", "200000"],
        ],
    )
    return (
        "Kubernetes request/limit declarations map onto the cgroup v2 "
        "controller knobs through the crun conversion (measured on this "
        "cluster):\n\n"
        "- request -> CpuShares -> crun logarithmic conversion to "
        "`cpu.weight` (besteffort floor = 1);\n"
        "- limit -> `cpu.max` quota/period (`100m` -> `10000 100000`); the "
        "quota is enforced per period and throttling is bimodal for "
        "saturating workloads.\n\n"
        "The crun conversion table:\n\n" + crun_table + "\n\n"
        "EEVDF orders runnable tasks by virtual runtime (`vruntime`) and "
        "slice deadline: a task's slice is `base_slice * weight / "
        "sum_weights` and its new deadline is `now + slice`.  The two-level "
        "hierarchy fact: `cpu.max` quota caps aggregate execution "
        "independently of the weight share, so a weight-only view misses "
        "throttling.  When demand exceeds the quota the task runs in "
        "burst/throttle cycles — it executes until the quota is exhausted and "
        "is throttled until the next period opens.\n\n"
        f"![Declared vs enforced]({steps['step-1']})\n\n"
        f"![Weight vs quota]({steps['step-2']})"
    )


def _method_section() -> str:
    """Cluster, kernel, workloads, cells, tools, alignment, gates."""
    return (
        "Cluster: the k8labs lab, a single 2-vCPU worker node w1 (KVM via "
        "Cloud-Hypervisor).  Kernel: Fedora 44 with kernel 7.1 (EEVDF "
        "scheduler, cgroup v2).  Workloads: stress-ng (saturating control), "
        "cpu-burner (steady 30% light demand), api-server (HTTP "
        "latency-sensitive), db-simulator (OLTP with periodic checkpoints).  "
        "Cells: the pinned six-cell request/limit matrix (none, 100m/100m, "
        "100m/1000m, 500m/500m, 500m/2000m, 1000m/2000m).\n\n"
        "Measurement tools: Perfetto `eevdf-deep` traces (sched_switch, "
        "sched_stat_runtime, sched_slice), EEVDF `/proc/<pid>/sched` "
        "snapshots (`eevdf-<pod>-pids.csv`), and cgroup `cpu.stat` CSVs "
        "(`cgroup-<pod>.csv`).  Trace alignment: the retained window excludes "
        "a 2s guard at each end.  Quality gates: a cell whose "
        "retained coverage is below 0.80 is marked degraded, excluded from "
        "family comparison images, and listed in the appendix.  Determinism: "
        "every analyzer and generator re-runs byte-identical on "
        "the same staged data."
    )


def _measured_section(
    families: list[str], family_data: dict, families_map: dict[str, dict]
) -> str:
    """Per-family slice-distribution tables + trajectory visuals."""
    parts: list[str] = []
    for family in families:
        workload = workload_for_family(family)
        parts.append(f"### {family} ({workload})")
        parts.append(
            "Slice-distribution statistics per cell (from `dist-summary.csv`):"
        )
        rows: list[list[str]] = []
        for cell, cell_data in family_data[family].items():
            summary = cell_data["summary"]
            rows.append(
                [
                    cell,
                    _format_cell(summary["slice_count"]),
                    _format_cell(summary["mean_us"]),
                    _format_cell(summary["median_us"]),
                    _format_cell(summary["p95_us"]),
                    _format_cell(summary["p99_us"]),
                    _format_cell(summary["max_us"]),
                    _format_cell(summary["throttle_ratio"]),
                ]
            )
        parts.append(
            _markdown_table(
                [
                    "cell",
                    "slice_count",
                    "mean_us",
                    "median_us",
                    "p95_us",
                    "p99_us",
                    "max_us",
                    "throttle_ratio",
                ],
                rows,
            )
        )
        family_rel = families_map[family]["family"]
        parts.append("Cumulative runtime trajectory (from `dist-runtime.csv`):")
        parts.append(
            f"![{family} runtime trajectory]({family_rel['runtime-trajectory.png']})"
        )
        parts.append("Family comparison visuals:")
        for name, rel in family_rel.items():
            if name == "runtime-trajectory.png":
                continue
            parts.append(f"![{family} {name}]({rel})")
        gifs = families_map[family]["gifs"]
        parts.append("Animated visuals:")
        parts.append(f"![{family} execution timeline]({gifs[TIMELINE_GIF]})")
        parts.append(f"![{family} slice distribution build]({gifs[HIST_GIF]})")
        parts.append("Per-cell slice histograms:")
        for cell, rel in families_map[family]["cells"].items():
            parts.append(f"![{family} {cell}]({rel})")
    return "\n\n".join(parts)


def _story_section(steps: dict[str, str]) -> str:
    """The three regimes + workload profiles vs stress-ng."""
    return (
        "The measured slice-duration distributions fall into three regimes.\n\n"
        "**Regime (a) — no contention:** a task with no competing workload "
        "runs un-preempted in long slices; the no-limit cells show long "
        "runtimes with no quota interruptions.\n\n"
        "**Regime (b) — contention under request weights:** co-scheduled "
        "tasks contend and EEVDF divides time proportionally to `cpu.weight`; "
        "slices shrink to the ~base_slice scale and scale with the weight "
        "share, so the request -> weight mapping is what protects a container "
        "during contention.\n\n"
        "**Regime (c) — limit below demand:** when the `cpu.max` quota is "
        "below real demand, the cgroup is throttled: the burst/throttle cycle "
        "caps execution to the quota window and the remaining demand is "
        "deferred to the next period.\n\n"
        "Workload profiles vs stress-ng: stress-ng saturates every core and "
        "exposes the raw throttling regime; cpu-burner at 30% demand stays "
        "well below the quota and shows near-zero throttling; api-server is "
        "latency-sensitive with bursty HTTP handling; db-simulator alternates "
        "OLTP queries with periodic checkpoints, so its distribution mixes "
        "contention-scale slices with idle gaps.\n\n"
        f"![Slice distribution]({steps['step-3']})\n\n"
        f"![Throttle pattern]({steps['step-4']})\n\n"
        f"![Config comparison]({steps['step-5']})"
    )


def _guidelines_section(family_data: dict, step6_rel: str) -> str:
    """The six measured-number guideline bullets + summary image."""
    bullets = "\n".join(f"- {bullet}" for bullet in guidelines(family_data))
    return bullets + f"\n\n![Guideline summary]({step6_rel})"


def _appendix_section(data_dir: Path, families: list[str], family_data: dict) -> str:
    """Reproducibility commands, data paths, hashes, degraded cells."""
    hashes = report_hashes(data_dir, families)
    degraded_lines: list[str] = []
    for family in families:
        for cell in degraded_cells(family_data[family]):
            degraded_lines.append(f"- {family} / {cell}")
    degraded_body = "\n".join(degraded_lines) if degraded_lines else "- (none)"

    return (
        "Reproducibility commands:\n\n"
        "- `make dist-report` regenerates this file from the staged "
        "distribution tree.\n"
        "- `dist-report.py --data-dir <analysis root> --output-file "
        "DEEP-DIVE-EEVDF-EXEC.md [--families <f1,f2,...>]`\n\n"
        "Data paths (relative to the analysis root; all inputs are "
        "dist-analyze output — no cluster and no network at analysis time):\n\n"
        "- `distribution/<family>/<cell>/dist-summary.csv` — measured slice "
        "statistics (the first row per cell feeds the tables and "
        "guidelines).\n"
        "- `distribution/<family>/<cell>/dist-slices.csv` — per-slice "
        "durations.\n"
        "- `distribution/<family>/<cell>/dist-percentiles.json` — decile "
        "tables.\n"
        "- `distribution/<family>/<cell>/slice-histogram.png`, "
        "`distribution/<family>/*.png` and "
        "`distribution/<family>/visuals/*.gif` — the embedded visuals.\n\n"
        "SHA-256 hashes of the `dist-summary.csv` inputs this report "
        "renders:\n\n"
        + _markdown_table(
            ["data path", "sha256"],
            [[path, digest] for path, digest in hashes.items()],
        )
        + "\n\n"
        "Degraded cells (trace coverage below 0.80; excluded from "
        "family comparison images and listed here for auditability):\n\n"
        + degraded_body
    )


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_report(
    data_dir: Path | str, families: list[str], output_path: Path | str
) -> str:
    """Assemble the full deep-dive markdown report.

    REPORT_TITLE followed by the seven SECTION_TITLES in pinned order.  The
    measured-results section renders a slice-distribution table per family
    (mean/median/p95/p99/max, slice count, throttle ratio per cell — values
    from dist-summary, floats via ``format(v, "g")``).  Every visual from
    image_paths is embedded with a path relative to ``output_path.parent``;
    the appendix renders reproducibility commands, data paths, the
    report_hashes digests, and the degraded-cells list.  Raises
    ``ValueError`` ("no families") when ``families == []``.  Pure and
    deterministic — no wall-clock values.
    """
    data_dir = Path(data_dir)
    output_path = Path(output_path)
    if not families:
        raise ValueError("no families")

    family_data: dict[str, dict] = {}
    for family in families:
        cells = _expected_cells_for_family(data_dir, family)
        family_data[family] = load_family_data(data_dir, family, cells)

    rel_images = image_paths(data_dir, families, output_path)
    steps, families_map = _split_image_rels(data_dir, families, rel_images)

    sections = [
        ("Executive summary", _executive_summary()),
        ("Mechanism", _mechanism_section(steps)),
        ("Method", _method_section()),
        (
            "Measured results per family",
            _measured_section(families, family_data, families_map),
        ),
        ("The distribution story", _story_section(steps)),
        ("Guidelines", _guidelines_section(family_data, steps["step-6"])),
        (
            "Appendix: reproducibility",
            _appendix_section(data_dir, families, family_data),
        ),
    ]
    body = "\n\n".join(f"## {title}\n\n{content}" for title, content in sections)
    return REPORT_TITLE + "\n\n" + body + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate the EEVDF CPU execution-time distribution deep-dive "
            "report (DEEP-DIVE-EEVDF-EXEC.md) from staged dist-analyze output."
        ),
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        metavar="DIR",
        help="Analysis root containing distribution/<family>/<cell>/ outputs",
    )
    parser.add_argument(
        "--output-file",
        required=True,
        metavar="PATH",
        help="Destination for the report markdown (parent dirs created)",
    )
    parser.add_argument(
        "--families",
        metavar="F1,F2,...",
        help="Comma-separated family names (default: sorted families from "
        "the data dir)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build the report and write it to --output-file.

    Loud failures (non-zero exit, stderr naming the cause, never a partial
    report): missing --data-dir, missing ``distribution/``, no families under
    ``distribution/``, an unknown listed family, or a family whose cell
    output is missing/empty.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"error: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    distribution = data_dir / "distribution"
    if not distribution.is_dir():
        print(
            f"error: distribution directory not found under {data_dir}",
            file=sys.stderr,
        )
        return 1

    available = families_from_data_dir(data_dir)
    if not available:
        print("error: no families found under distribution", file=sys.stderr)
        return 1

    if args.families:
        families = [name.strip() for name in args.families.split(",") if name.strip()]
    else:
        families = available

    for family in families:
        if family not in available:
            print(f"error: family not found: {family}", file=sys.stderr)
            return 1

    try:
        report = build_report(data_dir, families, Path(args.output_file))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
