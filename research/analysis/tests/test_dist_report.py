"""Tests for dist-report.py — deep-dive report generator.

Test-first design, red until the engineer implements the script.
The module/function/CLI names used here are the contract the implementation must
build:

    research/analysis/dist-report.py  (module: dist_report)

    Constants:
      REPORT_FILENAME          "DEEP-DIVE-EEVDF-EXEC.md"
      REPORT_TITLE             "# EEVDF CPU execution-time distribution: deep dive"
      SECTION_TITLES           the seven pinned section titles
      EXECUTIVE_BULLET_COUNT   5
      GUIDELINE_COUNT          6
      STEP_FILES               tuple of the six pinned step-*.png names
                               (same names as conftest DIST_STEPS_FILES)
      FAMILY_IMAGES            ("slice-dist-comparison.png",
                                "slice-ecdf-overlay.png",
                                "gantt-timeline.png",
                                "runtime-trajectory.png")
      CELL_HISTOGRAM           "slice-histogram.png"
      TIMELINE_GIF             "exec-timeline.gif"
      HIST_GIF                 "slice-dist-build.gif"
      VISUALS_DIR              "visuals"
      DEGRADED_QUALITY         "degraded"

    Pure core (testable without rendering):
      load_family_data(analysis_root: Path, family: str, cells: list[str])
          -> dict[str, dict]
          # {cell: {"summary": dict (FIRST summary row), "quality": str
          #         ("degraded" iff ANY summary row is degraded),
          #         "slices_us": list[float], "percentiles": dict}} read from
          #   <analysis_root>/distribution/<family>/<cell>/
          #     dist-slices.csv | dist-summary.csv | dist-percentiles.json
          # A listed cell with a missing file, or a dist-slices.csv with zero
          # rows, raises an error whose message contains the cell label —
          # never a silent partial report.
      families_from_data_dir(data_dir: Path) -> list[str]
          # sorted family dirs under <data_dir>/distribution/; [] when the
          # distribution dir is empty; raises (message names "distribution")
          # when <data_dir>/distribution/ does not exist
      workload_for_family(family: str) -> str
          # strips the "dist-" prefix ("dist-api-server" -> "api-server")
      degraded_cells(family_data: dict) -> list[str]
          # cells whose quality == DEGRADED_QUALITY, in pinned cell order
      guidelines(data: dict) -> list[str]
          # exactly GUIDELINE_COUNT bullet strings; every bullet contains a
          # measured number with a unit (the grep-able digit/unit rule);
          # collectively they cite the FIRST family's FIRST cell p99 rendered
          # "{v:g} us" and the BestEffort weight-1 floor ("weight 1")
      report_hashes(data_dir: Path, families: list[str]) -> dict[str, str]
          # {relative-path: sha256 hex} for every dist-summary.csv the report
          # renders — deterministic appendix hashes
      image_paths(data_dir: Path, families: list[str], output_path: Path)
          -> list[str]
          # every visual the report embeds, as a path
          # RELATIVE to output_path.parent (all must resolve to existing
          # fixture files)
      build_report(data_dir: Path, families: list[str], output_path: Path)
          -> str
          # the full markdown: REPORT_TITLE + the seven SECTION_TITLES in
          # pinned order; measured slice-distribution tables per family
          # (mean/median/p95/p99/max, slice count, throttle ratio per cell);
          # every visual embedded with a relative path; appendix with
          # reproducibility commands/data paths/hashes + the degraded-cells
          # list. Raises when families == [] ("no families").
      main(argv: list[str] | None = None) -> int

CLI:
    dist-report.py --data-dir <analysis root> --output-file <report .md path>
                   [--families <f1,f2,...>]
Writes DEEP-DIVE-EEVDF-EXEC.md at --output-file (parent dirs created).
Embedded image paths are computed RELATIVE to the report file's parent
directory, so a report written at the canonical location
(output/distribution/DEEP-DIVE-EEVDF-EXEC.md) resolves the natural
<family>/... paths, and the Makefile's research/ copy works too. Exits
non-zero with a stderr message naming the cause when the data dir, the
distribution dir, a listed family, or a listed cell's dist-analyze output is
missing or empty (never a partial report).

Embedded-visual contract:
  - the six step images:            <output-parent>/visuals/step-1..6-*.png
  - per family:                     <family>/slice-dist-comparison.png,
                                    <family>/slice-ecdf-overlay.png,
                                    <family>/gantt-timeline.png,
                                    <family>/runtime-trajectory.png
  - per cell:                       <family>/<cell>/slice-histogram.png
  - per family GIFs:                <family>/visuals/exec-timeline.gif,
                                    <family>/visuals/slice-dist-build.gif
  => at least 10 embedded visuals, every relative path resolvable.

Covered behavior:
  report exists with all 7 required sections, in pinned order
  executive summary in exactly 5 bullets
  mechanism: cpu.weight / cpu.max / vruntime / deadline / two-level /
          crun conversion table with the measured weights (17 / 59)
  method: cluster (w1) / kernel / workloads / cells / measurement
          tools (perfetto) / trace alignment (guard) / quality gates /
          determinism
  measured results per family: slice-distribution tables with the
          fixture stats (mean/median/p95/p99/max, slice count, throttle
          ratio) + cumulative runtime trajectory visuals
  distribution story: the three regimes (no contention / contention /
          quota-capped burst-throttle) + workload profiles (cpu-burner,
          api-server, db-simulator vs stress-ng)
  guidelines: 6 bullets, every bullet carries a measured number with
          a unit; values track the fixture data (provenance test)
  all 10+ visuals embedded with resolvable relative paths
  determinism: two runs -> identical SHA-256 of the doc
  degraded cells listed in the report appendix
  output naming + layout conventions honored by the generator

Run from research/analysis:
    python3 -m pytest tests/test_dist_report.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import zlib

import pandas as pd
import pytest

from tests.conftest import (
    DIST_PERCENTILE_STEPS,
    DIST_RUNTIME_COLUMNS,
    DIST_SLICES_COLUMNS,
    DIST_STEPS_ALT,
    DIST_STEPS_CELLS,
    DIST_STEPS_CELL_SPECS,
    DIST_STEPS_D10,
    DIST_STEPS_FILES,
    DIST_SUMMARY_COLUMNS,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_REPORT_SCRIPT = ANALYSIS_DIR / "dist-report.py"

# The report file the generator writes.
REPORT_FILENAME = "DEEP-DIVE-EEVDF-EXEC.md"
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
# Artifact names the report must embed.
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

# Fixture families mirror the standard six-cell request/limit matrix for
# Family A. dist-stress-ng carries the crun-enforced weights/quota/throttle
# ratios pinned by the shared fixture specs; dist-api-server is a second
# family proving the per-family report machinery (both use the pinned six-cell
# specs).
FIXTURE_FAMILIES = ("dist-stress-ng", "dist-api-server")
DEGRADED_CELL = "request=500m-limit=500m"  # cell index 3, degraded in fixture
# The cell whose p99 the guidelines must cite (first family, first cell).
GUIDELINE_SOURCE_CELL = "request=-limit="

# Hand-computed D10 stats (pandas linear interpolation, pinned):
# mean 55, median 55, p50 55, p95 95.5, p99 99.1, max 100. ALT stats:
# p95 480, p99 496, max 500.
D10_P99 = 99.1
ALT_P99 = 496.0

# The grep-able guideline rule: a measured number immediately followed by a
# unit (millicores / microseconds / milliseconds / percent).
MEASURED_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|us|m|%)")
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
YEAR_RE = re.compile(r"\b20\d{2}\b")


# =========================================================================
# Fixture helpers — a staged distribution tree with placeholder-but-
# valid images (tiny PNG / GIF89a bytes, deterministic, no matplotlib/PIL
# dependency at fixture-build time).
# =========================================================================


def _tiny_png() -> bytes:
    """Minimal valid 1x1 RGB PNG (deterministic bytes)."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + typ + data
        return out + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")  # filter 0, RGB(255, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _tiny_gif() -> bytes:
    """Minimal valid 1x1 GIF89a (deterministic bytes)."""
    return bytes.fromhex(
        "47 49 46 38 39 61 01 00 01 00 80 00 00 ff ff ff 00 00 00 "
        "2c 00 00 00 00 01 00 01 00 00 02 02 44 01 00 3b"
    )


def _stats(durations: list[float]) -> dict[str, float]:
    """dist-summary stats for a duration list (pandas method)."""
    series = pd.Series(list(durations), dtype="float64")
    return {
        "mean_us": float(series.mean()),
        "median_us": float(series.median()),
        "p50_us": float(series.quantile(0.50)),
        "p95_us": float(series.quantile(0.95)),
        "p99_us": float(series.quantile(0.99)),
        "max_us": float(series.max()),
    }


def _write_cell(
    root: pathlib.Path,
    family: str,
    spec: tuple,
    *,
    durations: list[float],
    quality: str = "good",
    qualities: tuple[str, ...] | None = None,
    pod: str = "stress-ng",
) -> pathlib.Path:
    """Write one cell's dist-analyze OUTPUT files + its slice-histogram.png.

    spec is a DIST_STEPS_CELL_SPECS entry (label, cpu_weight, cpu_max_quota,
    nr_periods, nr_throttled). quality applies to every summary row unless
    *qualities* (one per replicate) is given — the multi-replicate variant
    pins the "degraded iff ANY row is degraded" rule.
    """
    cell, weight, quota, periods, throttled = spec
    cell_dir = root / "distribution" / family / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    stats = _stats(durations)
    ratio = throttled / periods

    # dist-slices.csv — pinned SLICES_COLUMNS, 1s-spaced starts.
    slice_rows = [
        (start, start + int(dur), float(dur), 0, 1001, "stress-ng-cpu", pod)
        for i, dur in enumerate(durations)
        for start in [2_500_000 + i * 1_000_000]
    ]
    pd.DataFrame(slice_rows, columns=DIST_SLICES_COLUMNS).to_csv(
        cell_dir / "dist-slices.csv", index=False
    )

    # dist-runtime.csv — pinned RUNTIME_COLUMNS, constant 1.4 ms per-switch
    # deltas (kernel sched_stat_runtime semantics, pinned).
    runtime_rows = [
        (2_500_000 + i * 1_000_000, 0, 1001, 1001, "stress-ng-cpu", pod, 1_400_000)
        for i in range(len(durations))
    ]
    pd.DataFrame(runtime_rows, columns=DIST_RUNTIME_COLUMNS).to_csv(
        cell_dir / "dist-runtime.csv", index=False
    )

    # dist-summary.csv — one row per replicate, pinned SUMMARY_COLUMNS.
    row_qualities = list(qualities) if qualities is not None else [quality]
    summary_rows = [
        [
            cell,
            rep,
            pod,
            len(durations),
            float(sum(durations)) / 1000.0,
            stats["mean_us"],
            stats["median_us"],
            stats["p50_us"],
            stats["p95_us"],
            stats["p99_us"],
            stats["max_us"],
            ratio,
            weight,
            quota,
            q,
        ]
        for rep, q in enumerate(row_qualities, start=1)
    ]
    pd.DataFrame(summary_rows, columns=DIST_SUMMARY_COLUMNS).to_csv(
        cell_dir / "dist-summary.csv", index=False
    )

    # dist-percentiles.json — {replicate: {pod: {p<k>: value}}}, sorted keys.
    series = pd.Series(list(durations), dtype="float64")
    table = {f"p{k}": float(series.quantile(k / 100.0)) for k in DIST_PERCENTILE_STEPS}
    (cell_dir / "dist-percentiles.json").write_text(
        json.dumps({"1": {pod: table}}, indent=2, sort_keys=True) + "\n"
    )

    # Per-cell image (placeholder-but-valid PNG).
    (cell_dir / CELL_HISTOGRAM).write_bytes(_tiny_png())
    return cell_dir


def _write_family_visuals(root: pathlib.Path, family: str) -> None:
    """Write the family images + GIFs for one family."""
    fam_dir = root / "distribution" / family
    fam_dir.mkdir(parents=True, exist_ok=True)
    for name in FAMILY_IMAGES:
        (fam_dir / name).write_bytes(_tiny_png())
    visuals = fam_dir / VISUALS_DIR
    visuals.mkdir(parents=True, exist_ok=True)
    (visuals / TIMELINE_GIF).write_bytes(_tiny_gif())
    (visuals / HIST_GIF).write_bytes(_tiny_gif())


def build_report_fixture(
    root: pathlib.Path,
    *,
    durations: list[float] | None = None,
    degraded_index: int | None = 3,
    families: tuple[str, ...] = FIXTURE_FAMILIES,
) -> pathlib.Path:
    """Write a full staged tree (dist-analyze output + visuals).

    The two fixture families each carry the pinned six-cell matrix; the
    *dist-stress-ng* cell at *degraded_index* (default 3 = the 500m/500m
    quota cell) is marked quality=degraded so the appendix has a degraded
    cell to list.
    """
    if durations is None:
        durations = DIST_STEPS_D10
    for family in families:
        pod = "stress-ng" if family == "dist-stress-ng" else "api-server"
        for idx, spec in enumerate(DIST_STEPS_CELL_SPECS):
            quality = (
                "degraded"
                if family == "dist-stress-ng" and idx == degraded_index
                else "good"
            )
            _write_cell(
                root, family, spec, durations=durations, quality=quality, pod=pod
            )
        _write_family_visuals(root, family)
    # Global step visuals under distribution/visuals/.
    visuals = root / "distribution" / VISUALS_DIR
    visuals.mkdir(parents=True, exist_ok=True)
    for name in DIST_STEPS_FILES:
        (visuals / name).write_bytes(_tiny_png())
    return root


@pytest.fixture
def report_fixture_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Full staged tree: 2 families x 6 cells, one degraded cell, visuals."""
    return build_report_fixture(tmp_path / "report-fixture")


@pytest.fixture
def report_alt_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Same tree with ALT durations [100..500] us (guideline provenance)."""
    return build_report_fixture(tmp_path / "report-alt", durations=DIST_STEPS_ALT)


@pytest.fixture
def report_all_good_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Same tree with no degraded cell (degraded-cells edge)."""
    return build_report_fixture(tmp_path / "report-good", degraded_index=None)


@pytest.fixture
def report_multireplicate_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tree where the 500m/500m cell has (good, degraded) summary rows.

    Pins: a cell is degraded iff ANY of its summary rows is degraded.
    """
    root = build_report_fixture(tmp_path / "report-multirep", degraded_index=None)
    spec = DIST_STEPS_CELL_SPECS[3]  # request=500m-limit=500m
    _write_cell(
        root,
        "dist-stress-ng",
        spec,
        durations=DIST_STEPS_D10,
        qualities=("good", "degraded"),
    )
    return root


@pytest.fixture
def report_missing_cell_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tree with the 500m/500m stress-ng cell directory REMOVED."""
    root = build_report_fixture(tmp_path / "report-missing-cell")
    cell_dir = root / "distribution" / "dist-stress-ng" / DEGRADED_CELL
    for child in cell_dir.iterdir():
        child.unlink()
    cell_dir.rmdir()
    return root


@pytest.fixture
def report_missing_summary_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tree where the no-limit cell keeps its dir but dist-summary.csv is gone."""
    root = build_report_fixture(tmp_path / "report-missing-summary")
    (
        root
        / "distribution"
        / "dist-stress-ng"
        / GUIDELINE_SOURCE_CELL
        / "dist-summary.csv"
    ).unlink()
    return root


@pytest.fixture
def report_empty_slices_data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tree where the no-limit cell's dist-slices.csv has zero rows."""
    root = build_report_fixture(tmp_path / "report-empty-slices")
    no_limit = root / "distribution" / "dist-stress-ng" / GUIDELINE_SOURCE_CELL
    pd.DataFrame(columns=DIST_SLICES_COLUMNS).to_csv(
        no_limit / "dist-slices.csv", index=False
    )
    return root


@pytest.fixture
def report_no_distribution_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A data dir that exists but has no distribution/ subtree."""
    d = tmp_path / "report-no-distribution"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def report_empty_distribution_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A data dir whose distribution/ subtree is empty (no families)."""
    d = tmp_path / "report-empty-distribution"
    (d / "distribution").mkdir(parents=True)
    return d


# =========================================================================
# Helpers
# =========================================================================


def load_dist_report_module():
    """Import the not-yet-existing script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("dist_report", DIST_REPORT_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_REPORT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dist_report(argv: list[str]) -> tuple[int, str, str]:
    """Run dist-report.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(DIST_REPORT_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def loaded_families(module, data_dir: pathlib.Path) -> dict:
    """Load both fixture families through the module's own reader."""
    return {
        family: module.load_family_data(data_dir, family, list(DIST_STEPS_CELLS))
        for family in FIXTURE_FAMILIES
    }


def report_at(data_dir: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    """The canonical report location inside the fixture tree."""
    return data_dir / "distribution" / REPORT_FILENAME


def build_report_text(module, data_dir: pathlib.Path, out_path: pathlib.Path) -> str:
    """build_report with the fixture's pinned family order."""
    return module.build_report(data_dir, list(FIXTURE_FAMILIES), out_path)


def section_text(report: str, header: str) -> str:
    """Return the body under *header* (up to the next '## ' section)."""
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == header:
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.strip().startswith("## "):
                    break
                body.append(nxt)
            return "\n".join(body)
    raise AssertionError(f"section not found: {header!r}")


def section_bullets(report: str, header: str) -> list[str]:
    """Markdown bullet lines ('- ...') inside a section body."""
    return [
        line.strip()[2:].strip()
        for line in section_text(report, header).splitlines()
        if line.strip().startswith("- ")
    ]


def image_refs(report: str) -> list[str]:
    """Every markdown image target the report embeds."""
    return IMAGE_REF_RE.findall(report)


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_api(self):
        module = load_dist_report_module()
        for name in (
            "REPORT_FILENAME",
            "REPORT_TITLE",
            "SECTION_TITLES",
            "EXECUTIVE_BULLET_COUNT",
            "GUIDELINE_COUNT",
            "load_family_data",
            "families_from_data_dir",
            "workload_for_family",
            "degraded_cells",
            "guidelines",
            "report_hashes",
            "image_paths",
            "build_report",
            "main",
        ):
            assert callable(getattr(module, name, None)) or hasattr(module, name), (
                f"missing pinned module member: {name}"
            )

    def test_report_filename_pinned(self):
        module = load_dist_report_module()
        assert module.REPORT_FILENAME == REPORT_FILENAME

    def test_section_titles_pinned(self):
        module = load_dist_report_module()
        assert tuple(module.SECTION_TITLES) == SECTION_TITLES

    def test_executive_and_guideline_counts_pinned(self):
        module = load_dist_report_module()
        assert module.EXECUTIVE_BULLET_COUNT == EXECUTIVE_BULLET_COUNT
        assert module.GUIDELINE_COUNT == GUIDELINE_COUNT

    def test_step_files_are_the_six_pinned_names(self):
        module = load_dist_report_module()
        assert tuple(module.STEP_FILES) == tuple(DIST_STEPS_FILES)

    def test_family_and_gif_constants_pinned(self):
        module = load_dist_report_module()
        assert tuple(module.FAMILY_IMAGES) == FAMILY_IMAGES
        assert module.CELL_HISTOGRAM == CELL_HISTOGRAM
        assert module.TIMELINE_GIF == TIMELINE_GIF
        assert module.HIST_GIF == HIST_GIF
        assert module.VISUALS_DIR == VISUALS_DIR
        assert module.DEGRADED_QUALITY == DEGRADED_QUALITY


# =========================================================================
# Fixture validity (placeholder-but-valid images)
# =========================================================================


class TestFixtureValidity:
    """The synthetic visuals are real PNG/GIF files (Pillow-openable)."""

    def test_fixture_images_are_valid_pngs(self, report_fixture_data_dir):
        from PIL import Image

        probes = [
            report_fixture_data_dir / "distribution" / "visuals" / name
            for name in DIST_STEPS_FILES
        ]
        probes.append(
            report_fixture_data_dir
            / "distribution"
            / "dist-stress-ng"
            / DIST_STEPS_CELLS[0]
            / CELL_HISTOGRAM
        )
        for path in probes:
            with Image.open(path) as img:
                assert img.format == "PNG", f"not a PNG: {path}"

    def test_fixture_gifs_are_valid_gif89a(self, report_fixture_data_dir):
        from PIL import Image

        for family in FIXTURE_FAMILIES:
            for name in (TIMELINE_GIF, HIST_GIF):
                path = (
                    report_fixture_data_dir
                    / "distribution"
                    / family
                    / VISUALS_DIR
                    / name
                )
                with Image.open(path) as img:
                    assert img.format == "GIF", f"not a GIF: {path}"


# =========================================================================
# load_family_data (input contract)
# =========================================================================


class TestLoadFamilyData:
    """Reading the dist-analyze OUTPUT tree the report consumes."""

    def test_loads_all_six_cells_of_a_family(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_fixture_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert set(data) == set(DIST_STEPS_CELLS)

    def test_cell_summary_values_track_fixture_stats(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_fixture_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        cell = data["request=100m-limit=100m"]
        assert cell["summary"]["slice_count"] == 10
        assert cell["summary"]["p95_us"] == pytest.approx(95.5)
        assert cell["summary"]["p99_us"] == pytest.approx(99.1)
        assert cell["summary"]["max_us"] == pytest.approx(100.0)
        assert cell["summary"]["throttle_ratio"] == pytest.approx(0.99)
        assert cell["slices_us"] == pytest.approx(DIST_STEPS_D10)
        assert "p99" in cell["percentiles"]

    def test_good_cell_quality(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_fixture_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert data[DIST_STEPS_CELLS[0]]["quality"] == "good"

    def test_degraded_cell_flagged(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_fixture_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert data[DEGRADED_CELL]["quality"] == "degraded"

    def test_missing_cell_raises_naming_cell(self, report_missing_cell_data_dir):
        module = load_dist_report_module()
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                report_missing_cell_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
            )
        assert DEGRADED_CELL in str(excinfo.value)

    def test_missing_summary_raises_naming_cell(self, report_missing_summary_data_dir):
        module = load_dist_report_module()
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                report_missing_summary_data_dir,
                "dist-stress-ng",
                list(DIST_STEPS_CELLS),
            )
        assert GUIDELINE_SOURCE_CELL in str(excinfo.value)

    def test_empty_slices_raises_naming_cell(self, report_empty_slices_data_dir):
        module = load_dist_report_module()
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                report_empty_slices_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
            )
        assert GUIDELINE_SOURCE_CELL in str(excinfo.value)


# =========================================================================
# families_from_data_dir / workload_for_family
# =========================================================================


class TestFamiliesAndWorkloads:
    def test_workload_for_family_strips_prefix(self):
        module = load_dist_report_module()
        assert module.workload_for_family("dist-stress-ng") == "stress-ng"
        assert module.workload_for_family("dist-api-server") == "api-server"
        assert module.workload_for_family("dist-cpu-burner") == "cpu-burner"
        assert module.workload_for_family("dist-db-simulator") == "db-simulator"

    def test_families_from_data_dir_sorted(self, report_fixture_data_dir):
        module = load_dist_report_module()
        families = module.families_from_data_dir(report_fixture_data_dir)
        assert (
            families
            == sorted(FIXTURE_FAMILIES)
            == ["dist-api-server", "dist-stress-ng"]
        )

    def test_families_from_data_dir_empty(self, report_empty_distribution_dir):
        module = load_dist_report_module()
        assert module.families_from_data_dir(report_empty_distribution_dir) == []

    def test_families_from_data_dir_missing_distribution(
        self, report_no_distribution_dir
    ):
        module = load_dist_report_module()
        with pytest.raises(Exception) as excinfo:
            module.families_from_data_dir(report_no_distribution_dir)
        assert "distribution" in str(excinfo.value)


# =========================================================================
# degraded_cells
# =========================================================================


class TestDegradedCells:
    def test_lists_degraded_cell_in_pinned_order(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_fixture_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert module.degraded_cells(data) == [DEGRADED_CELL]

    def test_all_good_family_has_no_degraded_cells(self, report_all_good_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_all_good_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert module.degraded_cells(data) == []

    def test_any_degraded_summary_row_flags_cell(self, report_multireplicate_data_dir):
        module = load_dist_report_module()
        data = module.load_family_data(
            report_multireplicate_data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS)
        )
        assert data[DEGRADED_CELL]["quality"] == "degraded"
        assert module.degraded_cells(data) == [DEGRADED_CELL]


# =========================================================================
# guidelines (digit/unit rule)
# =========================================================================


class TestGuidelines:
    def test_exactly_six_guideline_bullets(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = loaded_families(module, report_fixture_data_dir)
        bullets = module.guidelines(data)
        assert len(bullets) == GUIDELINE_COUNT

    def test_every_bullet_carries_measured_number_with_unit(
        self, report_fixture_data_dir
    ):
        module = load_dist_report_module()
        data = loaded_families(module, report_fixture_data_dir)
        for bullet in module.guidelines(data):
            assert MEASURED_NUMBER_RE.search(bullet), (
                f"guideline bullet lacks a measured number with a unit "
                f"(digit/unit rule): {bullet!r}"
            )

    def test_cites_fixture_p99_of_first_cell(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = loaded_families(module, report_fixture_data_dir)
        text = "\n".join(module.guidelines(data))
        assert f"{D10_P99:g} us" in text

    def test_cites_besteffort_weight_one_floor(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = loaded_families(module, report_fixture_data_dir)
        text = "\n".join(module.guidelines(data))
        assert "weight 1" in text

    def test_provenance_tracks_alt_data(self, report_alt_data_dir):
        module = load_dist_report_module()
        data = loaded_families(module, report_alt_data_dir)
        text = "\n".join(module.guidelines(data))
        assert f"{ALT_P99:g} us" in text
        assert f"{D10_P99:g} us" not in text

    def test_deterministic_across_calls(self, report_fixture_data_dir):
        module = load_dist_report_module()
        data = loaded_families(module, report_fixture_data_dir)
        assert module.guidelines(data) == module.guidelines(data)


# =========================================================================
# build_report — the seven sections
# =========================================================================


class TestBuildReportSections:
    def test_all_sections_present_in_pinned_order(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        headers = [f"## {title}" for title in SECTION_TITLES]
        positions = [report.index(h) for h in headers]
        assert positions == sorted(positions)
        assert report.index(module.REPORT_TITLE) < positions[0]

    def test_executive_summary_has_exactly_five_bullets(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        bullets = section_bullets(report, "## Executive summary")
        assert len(bullets) == EXECUTIVE_BULLET_COUNT

    def test_mechanism_section_keywords(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Mechanism").lower()
        for keyword in (
            "cpu.weight",
            "cpu.max",
            "vruntime",
            "deadline",
            "two-level",
            "quota",
            "throttl",
            # the crun conversion table carries the measured weights 17/59.
            "17",
            "59",
        ):
            assert keyword in body, f"mechanism section misses {keyword!r}"

    def test_method_section_keywords(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Method").lower()
        for keyword in (
            "w1",  # cluster node
            "kernel",
            "workload",
            "perfetto",  # measurement tools
            "guard",  # trace alignment
            "quality",  # quality gates
            "determinism",
        ):
            assert keyword in body, f"method section misses {keyword!r}"

    def test_story_section_keywords(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## The distribution story").lower()
        for keyword in (
            "no contention",
            "contention",
            "quota",
            "burst",
            "throttl",
            # workload profiles vs stress-ng
            "cpu-burner",
            "api-server",
            "db-simulator",
            "stress-ng",
        ):
            assert keyword in body, f"story section misses {keyword!r}"

    def test_guidelines_section_bullets_have_measured_numbers(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        bullets = section_bullets(report, "## Guidelines")
        assert len(bullets) == GUIDELINE_COUNT
        for bullet in bullets:
            assert MEASURED_NUMBER_RE.search(bullet), (
                f"guideline bullet lacks a measured number with a unit: {bullet!r}"
            )

    def test_appendix_lists_degraded_cells_and_reproducibility(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        appendix = section_text(report, "## Appendix: reproducibility").lower()
        for keyword in (
            "reproducibility",
            "commands",
            "data paths",
            "hash",
            "degraded cells",
        ):
            assert keyword in appendix, f"appendix misses {keyword!r}"
        assert DEGRADED_CELL in appendix, "appendix must list the degraded cell"


# =========================================================================
# Measured results per family
# =========================================================================


class TestMeasuredTables:
    def test_stress_ng_tables_carry_fixture_stats(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Measured results per family")
        # D10 stats + per-cell throttle ratios (pinned fixture values).
        for value in ("95.5", "99.1", "100", "0.99", "0.97", "0.96", "0.01", "0.02"):
            assert value in body, f"measured tables miss {value!r}"

    def test_every_cell_label_present(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Measured results per family")
        for cell in DIST_STEPS_CELLS:
            assert cell in body, f"measured tables miss cell {cell!r}"

    def test_both_families_have_measured_sections(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Measured results per family")
        for family in FIXTURE_FAMILIES:
            assert family in body, f"measured section misses family {family!r}"

    def test_workload_names_in_measured_sections(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        body = section_text(report, "## Measured results per family")
        for workload in ("stress-ng", "api-server"):
            assert workload in body, f"measured section misses workload {workload!r}"


# =========================================================================
# Visuals embedded with resolvable relative paths
# =========================================================================


class TestVisualsEmbedded:
    def test_all_image_refs_resolve_from_report_parent(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        out_path = report_at(report_fixture_data_dir, tmp_path)
        report = build_report_text(module, report_fixture_data_dir, out_path)
        refs = image_refs(report)
        assert refs, "report embeds no visuals"
        for ref in refs:
            resolved = (out_path.parent / ref).resolve()
            assert resolved.is_file(), f"unresolvable image path in report: {ref}"

    def test_at_least_ten_visuals_embedded(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        assert len(image_refs(report)) >= 10

    def test_six_step_images_embedded(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        refs = set(image_refs(report))
        for name in DIST_STEPS_FILES:
            assert f"{VISUALS_DIR}/{name}" in refs, f"missing step image: {name}"

    def test_gifs_embedded_per_family(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        refs = set(image_refs(report))
        for family in FIXTURE_FAMILIES:
            for name in (TIMELINE_GIF, HIST_GIF):
                assert f"{family}/{VISUALS_DIR}/{name}" in refs, (
                    f"missing GIF: {family}/{name}"
                )

    def test_family_images_and_cell_histograms_embedded(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        refs = set(image_refs(report))
        for family in FIXTURE_FAMILIES:
            for name in FAMILY_IMAGES:
                assert f"{family}/{name}" in refs, (
                    f"missing family image: {family}/{name}"
                )
            for cell in DIST_STEPS_CELLS:
                assert f"{family}/{cell}/{CELL_HISTOGRAM}" in refs, (
                    f"missing cell histogram: {family}/{cell}"
                )

    def test_image_paths_matches_embedded_refs(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        out_path = report_at(report_fixture_data_dir, tmp_path)
        report = build_report_text(module, report_fixture_data_dir, out_path)
        pinned = module.image_paths(
            report_fixture_data_dir, list(FIXTURE_FAMILIES), out_path
        )
        assert set(image_refs(report)) == set(pinned)
        for rel in pinned:
            assert (out_path.parent / rel).is_file(), (
                f"image_paths yields missing file: {rel}"
            )


# =========================================================================
# Appendix hashes (reproducibility)
# =========================================================================


class TestReportHashes:
    def test_appendix_contains_sha256_of_pinned_summary(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        summary = (
            report_fixture_data_dir
            / "distribution"
            / "dist-stress-ng"
            / GUIDELINE_SOURCE_CELL
            / "dist-summary.csv"
        )
        digest = hashlib.sha256(summary.read_bytes()).hexdigest()
        appendix = section_text(report, "## Appendix: reproducibility")
        assert digest in appendix, "appendix lacks the measured input hash"

    def test_report_hashes_deterministic_and_cover_summaries(
        self, report_fixture_data_dir
    ):
        module = load_dist_report_module()
        hashes = module.report_hashes(report_fixture_data_dir, list(FIXTURE_FAMILIES))
        expected = {
            f"distribution/{family}/{cell}/dist-summary.csv"
            for family in FIXTURE_FAMILIES
            for cell in DIST_STEPS_CELLS
        }
        assert set(hashes) == expected
        assert hashes == module.report_hashes(
            report_fixture_data_dir, list(FIXTURE_FAMILIES)
        )

    def test_appendix_mentions_commands_and_data_paths(
        self, report_fixture_data_dir, tmp_path
    ):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        appendix = section_text(report, "## Appendix: reproducibility").lower()
        for keyword in ("commands", "data paths", "sha"):
            assert keyword in appendix, f"appendix misses {keyword!r}"


# =========================================================================
# Determinism
# =========================================================================


class TestDeterminism:
    def test_build_report_identical_twice(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        out_path = report_at(report_fixture_data_dir, tmp_path)
        first = build_report_text(module, report_fixture_data_dir, out_path)
        second = build_report_text(module, report_fixture_data_dir, out_path)
        assert first == second

    def test_cli_two_runs_identical_sha256(self, report_fixture_data_dir, tmp_path):
        argv = [
            "--data-dir",
            str(report_fixture_data_dir),
            "--output-file",
            str(tmp_path / "run" / REPORT_FILENAME),
            "--families",
            ",".join(FIXTURE_FAMILIES),
        ]
        rc1, _o1, err1 = run_dist_report(argv)
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, _o2, err2 = run_dist_report(argv)
        assert rc2 == 0, f"second run failed: {err2}"
        digest1 = hashlib.sha256(
            (tmp_path / "run" / REPORT_FILENAME).read_bytes()
        ).hexdigest()
        digest2 = hashlib.sha256(
            (tmp_path / "run" / REPORT_FILENAME).read_bytes()
        ).hexdigest()
        assert digest1 == digest2

    def test_report_has_no_wall_clock_year(self, report_fixture_data_dir, tmp_path):
        module = load_dist_report_module()
        report = build_report_text(
            module,
            report_fixture_data_dir,
            report_at(report_fixture_data_dir, tmp_path),
        )
        assert YEAR_RE.search(report) is None, (
            "report embeds a wall-clock year; clock-dependent values are forbidden"
        )


# =========================================================================
# CLI (--data-dir / --output-file / --families)
# =========================================================================


class TestCli:
    def test_help_flag_prints_usage_and_flags(self):
        rc, out, err = run_dist_report(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        for flag in ("--data-dir", "--output-file", "--families"):
            assert flag in combined

    def test_happy_path_writes_report(self, report_fixture_data_dir, tmp_path):
        out_path = tmp_path / "out" / REPORT_FILENAME
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_fixture_data_dir),
                "--output-file",
                str(out_path),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc == 0, f"stderr: {err}"
        assert out_path.is_file(), f"report not written: {out_path}"
        text = out_path.read_text()
        assert text.strip(), "report is empty"
        assert "Executive summary" in text

    def test_creates_output_parent_dirs(self, report_fixture_data_dir, tmp_path):
        out_path = tmp_path / "a" / "b" / "c" / REPORT_FILENAME
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_fixture_data_dir),
                "--output-file",
                str(out_path),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc == 0, f"stderr: {err}"
        assert out_path.is_file()

    def test_default_families_sorted(self, report_fixture_data_dir, tmp_path):
        out_path = tmp_path / "default" / REPORT_FILENAME
        rc, _out, err = run_dist_report(
            ["--data-dir", str(report_fixture_data_dir), "--output-file", str(out_path)]
        )
        assert rc == 0, f"stderr: {err}"
        text = out_path.read_text()
        for family in FIXTURE_FAMILIES:
            assert family in text

    def test_missing_data_dir_fails(self, tmp_path):
        missing = tmp_path / "no-such-data-dir"
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(missing),
                "--output-file",
                str(tmp_path / REPORT_FILENAME),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc != 0, "missing data dir must fail loudly"
        assert str(missing) in err

    def test_missing_distribution_fails(self, report_no_distribution_dir, tmp_path):
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_no_distribution_dir),
                "--output-file",
                str(tmp_path / REPORT_FILENAME),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc != 0, "missing distribution/ must fail loudly"
        assert "distribution" in err

    def test_empty_distribution_fails(self, report_empty_distribution_dir, tmp_path):
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_empty_distribution_dir),
                "--output-file",
                str(tmp_path / REPORT_FILENAME),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc != 0, "empty distribution must fail loudly"
        assert "no families" in err

    def test_unknown_family_fails_naming_family(
        self, report_fixture_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_fixture_data_dir),
                "--output-file",
                str(tmp_path / REPORT_FILENAME),
                "--families",
                "dist-stress-ng,dist-does-not-exist",
            ]
        )
        assert rc != 0, "unknown family must fail loudly"
        assert "dist-does-not-exist" in err

    def test_missing_cell_fails_naming_cell(
        self, report_missing_cell_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_report(
            [
                "--data-dir",
                str(report_missing_cell_data_dir),
                "--output-file",
                str(tmp_path / REPORT_FILENAME),
                "--families",
                ",".join(FIXTURE_FAMILIES),
            ]
        )
        assert rc != 0, "missing cell must fail loudly"
        assert DEGRADED_CELL in err

    def test_missing_required_flag_fails(self, report_fixture_data_dir, tmp_path):
        rc, _out, err = run_dist_report(["--data-dir", str(report_fixture_data_dir)])
        assert rc != 0
        assert "--output-file" in err
