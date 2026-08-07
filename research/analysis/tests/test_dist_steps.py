"""Tests for dist-steps.py — six step-by-step distribution images.

Test-first design, red until the engineer implements the script.
The module/function/CLI names used here are the contract the implementation must
build:

    research/analysis/dist-steps.py  (module: dist_steps)

    Constants:
      STEP_FILES       tuple of exactly the six pinned step-*.png names, in
                       step order 1..6 (pinned layout)
      TITLES           dict[int, str], one title per step 1..6
      BASE_SLICE_US    1400.0  (measured 1.4 ms base slice, FRAMEWORK.md)
      QUOTA_PERIOD_US  100000  (kernel default cpu.max period in us)

    Pure core (testable without rendering):
      load_family_data(analysis_root: Path, family: str, cells: list[str])
          -> dict[str, dict]
          # {cell: {"summary": dict, "slices_us": list[float],
          #         "percentiles": dict}} read from
          #   <analysis_root>/distribution/<family>/<cell>/
          #     dist-slices.csv | dist-summary.csv | dist-percentiles.json
      declared_for_cell(cell: str) -> tuple[int | None, int | None]
          # declared request/limit millicores parsed from the cell label
      annotation_text(step: int, data: dict) -> list[str]
          # the exact annotation-block strings rendered onto the image; every
          # string is rendered as ONE text object (fig.text or ax.text)

    Render (each writes the PNG AND returns the matplotlib Figure):
      render_step(step: int, data: dict, out_path: Path) -> Figure
      render_all(data: dict, visuals_dir: Path) -> dict[int, Figure]

    main(argv: list[str] | None = None) -> int

CLI:
    dist-steps.py --data-dir <analysis root> --output-dir <out root>
                  --family <name> --cells <c1,c2,...>
Writes <out root>/distribution/visuals/step-1..6-*.png (canonical layout).
Exits non-zero when a listed cell's dist-analyze output is missing or its
dist-slices.csv has zero rows (loud failure naming the cell).

Annotation contract (exact substrings the tests assert):
  step 1  per cell: "{cell} -> weight {w}, quota {q}"  (measured cpu.weight /
          cpu.max from dist-summary; declared request/limit from the label)
  step 2  "slice = base_slice * weight / sum_weights",
          "base_slice 1.4 ms", "quota window: period 100000 us",
          plus the step-1 weight lines
  step 3  "p50 55 us", "p95 95.5 us", "p99 99.1 us", "max 100 us" for the
          no-limit cell (cells[0]) — the fixture D10 hand-computed stats
  step 4  "request=500m-limit=500m", "throttle_ratio 0.96", "throttled",
          "slice gap" for the quota cell (cells[3])
  step 5  "bimodal", "p99", "99.1", "0.95" (family ECDF + bimodality finding)
  step 6  "0.99", "0.96", "0.95", "request", "limit" (guideline numbers)

Covered behavior:
  exactly six images under output/distribution/visuals/
  each image non-empty, openable via Pillow
  annotation text present in each image (figure text objects, no OCR)
  measured-data provenance: annotations track the fixture data, not
          hardcoded values (changed-data fixture proves it)
  identical SHA-256 across two runs

Run from research/analysis:
    python3 -m pytest tests/test_dist_steps.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import subprocess
import sys

import matplotlib

matplotlib.use("Agg")  # headless rendering for the in-process figure tests

from PIL import Image  # noqa: E402

import pytest  # noqa: E402

from tests.conftest import (  # noqa: E402
    DIST_STEPS_ALT,
    DIST_STEPS_CELLS,
    DIST_STEPS_D10,
    DIST_STEPS_FILES,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
DIST_STEPS_SCRIPT = ANALYSIS_DIR / "dist-steps.py"

# Hand-computed stats for the fixture D10 durations [10..100] us (pandas
# linear interpolation, same constants as dist-analyze):
D10_P50 = 55.0
D10_P95 = 95.5
D10_P99 = 99.1
D10_MAX = 100.0
# Alt durations [100..500] us -> pandas linear interpolation quantiles:
# p50 = 300.0, p95 = 480.0, p99 = 496.0, max = 500.0
ALT_P50 = 300.0
ALT_P95 = 480.0
ALT_MAX = 500.0


# =========================================================================
# Helpers
# =========================================================================


def load_dist_steps_module():
    """Import the not-yet-existing script so pinned names are callable."""
    spec = importlib.util.spec_from_file_location("dist_steps", DIST_STEPS_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {DIST_STEPS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dist_steps(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run dist-steps.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(DIST_STEPS_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def agg_env() -> dict[str, str]:
    """Environment for subprocess renders: deterministic Agg backend."""
    env = dict(os.environ)
    env["MPLBACKEND"] = "Agg"
    return env


def load_family_data(module, data_dir: pathlib.Path) -> dict:
    """Load the six-cell fixture data through the module's own reader."""
    return module.load_family_data(data_dir, "dist-stress-ng", list(DIST_STEPS_CELLS))


def figure_texts(fig) -> list[str]:
    """Collect every human-visible text object from a matplotlib Figure.

    Gathers fig.text annotations, axis-level texts, axis titles and the
    figure suptitle — this is the module-exposed annotation mechanism the
    tests use instead of OCR.
    """
    texts = [t.get_text() for t in fig.texts]
    if fig._suptitle is not None:
        texts.append(fig._suptitle.get_text())
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.extend(t.get_text() for t in ax.texts)
    return [t for t in texts if t]


def assert_openable_png(path: pathlib.Path):
    """Assert *path* is a non-empty PNG Pillow can open."""
    assert path.exists(), f"missing image: {path}"
    assert path.stat().st_size > 0, f"empty image: {path}"
    with Image.open(path) as img:
        img.verify()  # raises on truncated/corrupt image data
    assert Image.open(path).format == "PNG"


def sha256_of(path: pathlib.Path) -> str:
    """SHA-256 hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root*."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return manifest


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_api(self):
        module = load_dist_steps_module()
        for name in (
            "STEP_FILES",
            "TITLES",
            "BASE_SLICE_US",
            "QUOTA_PERIOD_US",
            "load_family_data",
            "declared_for_cell",
            "annotation_text",
            "render_step",
            "render_all",
            "main",
        ):
            assert hasattr(module, name), f"missing pinned name: {name}"
        for func in (
            "load_family_data",
            "declared_for_cell",
            "annotation_text",
            "render_step",
            "render_all",
            "main",
        ):
            assert callable(getattr(module, func)), f"not callable: {func}"

    def test_step_files_are_the_six_pinned_names(self):
        module = load_dist_steps_module()
        assert tuple(module.STEP_FILES) == DIST_STEPS_FILES
        assert len(module.STEP_FILES) == 6

    def test_titles_cover_all_six_steps(self):
        module = load_dist_steps_module()
        assert set(module.TITLES.keys()) == {1, 2, 3, 4, 5, 6}
        for step in range(1, 7):
            assert module.TITLES[step].strip(), f"empty title for step {step}"

    def test_measured_constants_pinned(self):
        module = load_dist_steps_module()
        assert module.BASE_SLICE_US == pytest.approx(1400.0)
        assert module.QUOTA_PERIOD_US == 100000


# =========================================================================
# Declared request/limit from the cell label
# =========================================================================


class TestDeclaredForCell:
    """declared_for_cell parses the config-derived label (step-1 left side)."""

    def test_none_none_cell(self):
        module = load_dist_steps_module()
        assert module.declared_for_cell("request=-limit=") == (None, None)

    def test_100m_100m_cell(self):
        module = load_dist_steps_module()
        assert module.declared_for_cell("request=100m-limit=100m") == (100, 100)

    def test_500m_2000m_cell(self):
        module = load_dist_steps_module()
        assert module.declared_for_cell("request=500m-limit=2000m") == (500, 2000)


# =========================================================================
# load_family_data — dist-analyze OUTPUT ingestion
# =========================================================================


class TestLoadFamilyData:
    """The reader must consume the pinned dist-analyze output files."""

    def test_loads_all_six_cells_with_summary_values(
        self, dist_steps_family_data_dir: pathlib.Path
    ):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        assert set(data.keys()) == set(DIST_STEPS_CELLS)
        quota = data["request=500m-limit=500m"]["summary"]
        assert quota["cpu_weight"] == 59
        assert quota["cpu_max"] == 50000
        assert quota["throttle_ratio"] == pytest.approx(0.96, abs=1e-9)
        assert quota["p95_us"] == pytest.approx(D10_P95, abs=1e-9)

    def test_slices_durations_loaded(self, dist_steps_family_data_dir: pathlib.Path):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        slices = data["request=-limit="]["slices_us"]
        assert slices == [float(v) for v in DIST_STEPS_D10]

    def test_percentiles_loaded(self, dist_steps_family_data_dir: pathlib.Path):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        # dist-percentiles.json carries the decile key set (p1..p99, no p95 —
        # p95 lives in dist-summary.csv per the pinned dist-analyze contract).
        table = data["request=-limit="]["percentiles"]
        assert table["p99"] == pytest.approx(D10_P99, abs=1e-9)
        assert table["p51"] == pytest.approx(55.9, abs=1e-9)

    def test_missing_cell_raises_naming_cell(
        self, dist_steps_missing_cell_data_dir: pathlib.Path
    ):
        module = load_dist_steps_module()
        missing = "request=500m-limit=500m"
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                dist_steps_missing_cell_data_dir,
                "dist-stress-ng",
                list(DIST_STEPS_CELLS),
            )
        assert missing in str(excinfo.value)

    def test_empty_slices_raises_naming_cell(
        self, dist_steps_empty_slices_data_dir: pathlib.Path
    ):
        module = load_dist_steps_module()
        no_limit = DIST_STEPS_CELLS[0]
        with pytest.raises(Exception) as excinfo:
            module.load_family_data(
                dist_steps_empty_slices_data_dir,
                "dist-stress-ng",
                list(DIST_STEPS_CELLS),
            )
        assert no_limit in str(excinfo.value)


# =========================================================================
# annotation_text — the pinned annotation blocks (module-exposed metadata)
# =========================================================================


class TestAnnotationText:
    """Every step's annotation block must carry its title, mechanism text
    and the measured numbers."""

    def test_step1_weight_and_quota_lines(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(1, data))
        for expected in (
            "weight 1",
            "weight 17",
            "weight 59",
            "weight 100",
            "quota 10000",
            "quota 50000",
            "quota 200000",
            "quota 100000",
            "request=100m-limit=100m",
            "request=-limit=",
        ):
            assert expected in block, f"step-1 annotation missing {expected!r}"

    def test_step2_mechanism_and_base_slice(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(2, data))
        for expected in (
            "slice = base_slice * weight / sum_weights",
            "base_slice 1.4 ms",
            "period 100000",
            "weight 17",
            "weight 100",
        ):
            assert expected in block, f"step-2 annotation missing {expected!r}"

    def test_step3_percentile_annotations(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(3, data))
        for expected in (
            "percentile",
            "p50 55 us",
            "p95 95.5 us",
            "p99 99.1 us",
            "max 100 us",
        ):
            assert expected in block, f"step-3 annotation missing {expected!r}"

    def test_step4_throttle_annotations(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(4, data))
        for expected in (
            "request=500m-limit=500m",
            "throttle_ratio 0.96",
            "throttled",
            "slice gap",
        ):
            assert expected in block, f"step-4 annotation missing {expected!r}"

    def test_step5_ecdf_bimodality(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(5, data))
        for expected in ("bimodal", "p99", "99.1", "0.95"):
            assert expected in block, f"step-5 annotation missing {expected!r}"

    def test_step6_guideline_numbers(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        block = "\n".join(module.annotation_text(6, data))
        for expected in ("0.99", "0.96", "0.95", "request", "limit"):
            assert expected in block, f"step-6 annotation missing {expected!r}"

    def test_unknown_step_raises_valueerror(self, dist_steps_family_data_dir):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        with pytest.raises(ValueError):
            module.annotation_text(0, data)
        with pytest.raises(ValueError):
            module.annotation_text(7, data)

    def test_annotations_reflect_fixture_data_not_hardcoded(
        self, tmp_path: pathlib.Path
    ):
        """Provenance ("real measured data"): with DIFFERENT fixture
        slice durations the annotations must carry the new measured
        percentiles — a hardcoded 95.5 would be caught here."""
        from tests.conftest import build_dist_steps_family

        alt_dir = build_dist_steps_family(tmp_path / "alt", durations=DIST_STEPS_ALT)
        module = load_dist_steps_module()
        data = load_family_data(module, alt_dir)
        block = "\n".join(module.annotation_text(3, data))
        assert f"p50 {ALT_P50:g} us" in block
        assert f"p95 {ALT_P95:g} us" in block
        assert f"max {ALT_MAX:g} us" in block


# =========================================================================
# In-process rendering — figure text objects carry the annotation block
# =========================================================================


class TestRenderInProcess:
    """render_step writes the PNG AND returns a Figure whose text objects
    contain every annotation string (the no-OCR annotation mechanism)."""

    @pytest.mark.parametrize("step", [1, 2, 3, 4, 5, 6])
    def test_render_step_writes_openable_png(
        self, step, dist_steps_family_data_dir, tmp_path
    ):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        out = tmp_path / module.STEP_FILES[step - 1]
        fig = module.render_step(step, data, out)
        assert fig is not None
        assert_openable_png(out)

    @pytest.mark.parametrize("step", [1, 2, 3, 4, 5, 6])
    def test_render_step_figure_contains_annotation_text(
        self, step, dist_steps_family_data_dir, tmp_path
    ):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        out = tmp_path / module.STEP_FILES[step - 1]
        fig = module.render_step(step, data, out)
        texts = figure_texts(fig)
        # title present
        assert module.TITLES[step] in texts, (
            f"step-{step} title {module.TITLES[step]!r} not rendered"
        )
        # every annotation-block line is a rendered text object
        for line in module.annotation_text(step, data):
            assert line in texts, f"step-{step} annotation line not in figure: {line!r}"

    def test_render_all_writes_exactly_six_pngs(
        self, dist_steps_family_data_dir, tmp_path
    ):
        module = load_dist_steps_module()
        data = load_family_data(module, dist_steps_family_data_dir)
        visuals = tmp_path / "visuals"
        figures = module.render_all(data, visuals)
        assert set(figures.keys()) == {1, 2, 3, 4, 5, 6}
        pngs = {p.name for p in visuals.iterdir() if p.suffix == ".png"}
        assert pngs == set(DIST_STEPS_FILES)
        for name in DIST_STEPS_FILES:
            assert_openable_png(visuals / name)


# =========================================================================
# CLI contract
# =========================================================================


class TestCli:
    """--data-dir / --output-dir / --family / --cells contract."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_dist_steps(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        for flag in ("--data-dir", "--output-dir", "--family", "--cells"):
            assert flag in combined

    def test_missing_required_flags_exits_nonzero(self):
        rc, _out, err = run_dist_steps([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero(self, tmp_path):
        rc, _out, err = run_dist_steps(
            [
                "--data-dir",
                str(tmp_path / "missing"),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                "dist-stress-ng",
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0
        assert "missing" in err or "data-dir" in err

    def test_missing_cell_fails_loudly_naming_cell(
        self, dist_steps_missing_cell_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_steps(
            [
                "--data-dir",
                str(dist_steps_missing_cell_data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                "dist-stress-ng",
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0, "missing cell must fail loudly"
        assert "request=500m-limit=500m" in err

    def test_empty_slices_fails_loudly_naming_cell(
        self, dist_steps_empty_slices_data_dir, tmp_path
    ):
        rc, _out, err = run_dist_steps(
            [
                "--data-dir",
                str(dist_steps_empty_slices_data_dir),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                "dist-stress-ng",
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc != 0, "empty slices must fail loudly"
        assert DIST_STEPS_CELLS[0] in err

    def test_e2e_produces_exactly_six_pngs(self, dist_steps_family_data_dir, tmp_path):
        out_root = tmp_path / "out"
        rc, _out, err = run_dist_steps(
            [
                "--data-dir",
                str(dist_steps_family_data_dir),
                "--output-dir",
                str(out_root),
                "--family",
                "dist-stress-ng",
                "--cells",
                ",".join(DIST_STEPS_CELLS),
            ],
            env=agg_env(),
        )
        assert rc == 0, f"stderr: {err}"
        visuals = out_root / "distribution" / "visuals"
        assert visuals.is_dir(), f"missing visuals dir: {visuals}"
        pngs = {p.name for p in visuals.iterdir() if p.suffix == ".png"}
        assert pngs == set(DIST_STEPS_FILES), (
            f"expected exactly the six pinned files, got {pngs}"
        )
        for name in DIST_STEPS_FILES:
            assert_openable_png(visuals / name)


# =========================================================================
# Determinism
# =========================================================================


class TestDeterminism:
    """Two CLI runs on the same staged data yield byte-identical PNGs."""

    def test_two_runs_produce_identical_sha256(
        self, dist_steps_family_data_dir, tmp_path
    ):
        argv = [
            "--data-dir",
            str(dist_steps_family_data_dir),
            "--family",
            "dist-stress-ng",
            "--cells",
            ",".join(DIST_STEPS_CELLS),
        ]
        rc1, _o1, err1 = run_dist_steps(
            [*argv, "--output-dir", str(tmp_path / "out1")], env=agg_env()
        )
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, _o2, err2 = run_dist_steps(
            [*argv, "--output-dir", str(tmp_path / "out2")], env=agg_env()
        )
        assert rc2 == 0, f"second run failed: {err2}"
        m1 = sha256_manifest(tmp_path / "out1" / "distribution")
        m2 = sha256_manifest(tmp_path / "out2" / "distribution")
        assert m1 == m2
        assert set(m1.keys()) == {f"visuals/{name}" for name in DIST_STEPS_FILES}
