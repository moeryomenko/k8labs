"""Tests for dist-gif.py — animated visualization of the CPU execution-time distribution.

Test revision (re-delegated; replaces an earlier version of this file). The
current `research/analysis/dist-gif.py` implements the OLD contract
and MUST fail these tests (RED) until the revision is implemented.

Revision drivers (user-approved settings change after the original design measured
~37K bars/frame x ~1400 frames = 5.5-6h/family for exec-timeline.gif on the
merged per-cell slices):

    1. exec-timeline.gif renders PER-REPLICATE (each replicate is its own
       retained window) from per-replicate slice files
       `dist-slices-replicate-<n>.csv`; the canonical `exec-timeline.gif` is
       replicate 1's GIF (byte-identical).
    2. timeline frame count = min(floor(retained_us / 500000), 120) via
       integer-microsecond math, hard-capped at 120 frames.
    3. each frame draws ONLY the slice bars whose start falls in the moving
       2s window (pinned by the `window_slice_count` helper).
    4. slice-dist-build.gif: one frame per second, capped at 120 frames.
    5. fps kept: 10fps timeline / 4fps histogram (behavioral via frame
       duration ~100ms/~250ms).
    6. determinism and Pillow-missing error unchanged.
    7. annotation format (family | cell | elapsed) unchanged; elapsed derived
       from slice timestamps.

Module: `research/analysis/dist-gif.py`  (import name: `dist_gif`)

Constants:
  FPS_TIMELINE_DEFAULT   10        exec-timeline.gif playback fps (kept)
  FPS_HIST_DEFAULT       4         slice-dist-build.gif playback fps (kept)
  WINDOW_S_DEFAULT       2.0       moving window width (s, kept)
  STEP_S_DEFAULT         0.5       window step (s) — CHANGED from 0.2
  TIMELINE_MAX_FRAMES    120       hard cap for exec-timeline.gif (NEW)
  HIST_MAX_FRAMES        120       hard cap for slice-dist-build.gif (NEW)
  TIMELINE_GIF           "exec-timeline.gif"            (canonical rep-1 alias)
  HIST_GIF               "slice-dist-build.gif"

Pure core (testable with pinned dist-analyze output CSVs, no rendering):
  retained_window_s(slices_df) -> float
  timeline_frame_count(retained_s, *, step_s=0.5, max_frames=120) -> int
      min(int(retained_s * 1_000_000) // int(step_s * 1_000_000), max_frames)
  hist_frame_count(retained_s, *, max_frames=120) -> int
      min(int(retained_s), max_frames)
  elapsed_for_frame(slices_df, frame_index, step_s=0.5) -> float
  annotation_text(family, cell, elapsed_s) -> str
  representative_cell(family_dir, cell=None) -> str
  quota_cells(family_dir) -> set[str]
  replicate_slice_files(cell_dir) -> list[Path]        (NEW)
  window_slice_count(slices_df, frame_index, *, window_s=2.0, step_s=0.5)
      -> int                                           (NEW)

Render layer:
  require_pillow()                     raises RuntimeError naming Pillow (kept)
  generate_family_gifs(data_dir, output_dir, family, *, cell=None,
                       window_s=2.0, step_s=0.5, fps_timeline=10, fps_hist=4)
      -> dict[str, Path]
      Renders `exec-timeline-replicate-<n>.gif` for every per-replicate slice
      file in the representative cell (each replicate its own retained window),
      writes the canonical TIMELINE_GIF as replicate 1's bytes, and renders
      HIST_GIF from replicate 1's slices. Raises when no per-replicate slice
      files exist, when the representative cell has no animatable data, or
      when Pillow is unavailable.
  main(argv=None) -> int

CLI: --data-dir <root> --output-dir <root> --family <name>
     [--cell <name>] [--window-s 2.0] [--step-s 0.5]
     [--fps-timeline 10] [--fps-hist 4]
Reads <data-dir>/distribution/<family>/<cell>/dist-slices-replicate-<n>.csv
(per-replicate slice files) — NOT the merged dist-slices.csv.
Writes <output-dir>/distribution/<family>/visuals/
  exec-timeline-replicate-<n>.gif, exec-timeline.gif (== replicate 1),
  slice-dist-build.gif.

Covered behavior:
  exec-timeline.gif PER-REPLICATE: min(floor(retained_window_s/0.5),120)
  per-frame window-only bar rendering (window_slice_count)
  slice-dist-build.gif: one frame per second, capped 120, 4 fps
  GIF89a validity, per-frame data-derived annotation
  determinism: byte-identical SHA-256 across two runs
  Pillow-missing: clear message naming Pillow

Run from research/analysis:
    python3 -m pytest tests/test_dist_gif.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    DIST_SLICES_COLUMNS,
    DIST_SUMMARY_COLUMNS,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
GIF_SCRIPT = ANALYSIS_DIR / "dist-gif.py"

TIMELINE_GIF = "exec-timeline.gif"
HIST_GIF = "slice-dist-build.gif"
REPLICATE_TIMELINE_TMPL = "exec-timeline-replicate-{n}.gif"
REPLICATE_SLICES_PREFIX = "dist-slices-replicate-"
FAMILY = "dist-stress-ng"
REP_CELL = "request=100m-limit=100m"
NO_LIMIT_CELL = "request=none-limit=none"

# Per-replicate fixture geometry: each replicate is its own retained window.
# Replicate 1 spans 4.0s..10.0s -> retained 6.0s.
REP1_MIN_TS_US = 4_000_000
REP1_MAX_TS_US = 10_000_000
REP1_RETENTION_S = 6.0  # (10_000_000 - 4_000_000) / 1e6
REP1_TIMELINE_FRAMES = 12  # min(6_000_000 // 500_000, 120)
REP1_HIST_FRAMES = 6  # min(6, 120)

# Replicate 2 spans 1.0s..5.0s -> retained 4.0s.
REP2_RETENTION_S = 4.0  # (5_000_000 - 1_000_000) / 1e6
REP2_TIMELINE_FRAMES = 8  # min(4_000_000 // 500_000, 120)

# Cap replicate spans 0.0s..130.0s -> retained 130.0s (exercises both caps).
CAP_RETENTION_S = 130.0
CAP_TIMELINE_FRAMES = 120  # min(130_000_000 // 500_000, 120) — capped
CAP_HIST_FRAMES = 120  # min(130, 120) — capped

# Spec reference: 90s measurement minus 2s guards on an 86s trace = 82.0s.
SPEC_RETENTION_S = 82.0
SPEC_TIMELINE_FRAMES = 120  # min(floor(82.0 / 0.5), 120) — capped
SPEC_HIST_FRAMES = 82  # min(floor(82.0), 120) — uncapped

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_dist_gif_module():
    """Import the script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("dist_gif", GIF_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {GIF_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_gif(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run dist-gif.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(GIF_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_slices_csv(path: pathlib.Path, rows: list[tuple]) -> pathlib.Path:
    """Write a pinned dist-slices.csv (SLICES_COLUMNS schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(DIST_SLICES_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_summary_csv(path: pathlib.Path, rows: list[tuple]) -> pathlib.Path:
    """Write a pinned dist-summary.csv (SUMMARY_COLUMNS schema)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(DIST_SUMMARY_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return path


def slice_row(
    ts_start_us: int,
    duration_us: int,
    cpu: int = 0,
    tid: int = 1001,
    thread_name: str = "stress-ng-cpu",
    pod: str = "stress-ng",
) -> tuple:
    """A single dist-slices.csv row (ts_start_us, ts_end_us, duration_us, ...)."""
    return (
        ts_start_us,
        ts_start_us + duration_us,
        duration_us,
        cpu,
        tid,
        thread_name,
        pod,
    )


def _rep_summary_rows() -> list[tuple]:
    """REP_CELL dist-summary rows: 3 stress-ng replicates + 1 system row.

    cpu_max = 10000 -> REP_CELL is a quota cell (hatched throttle gaps).
    """
    return [
        (
            REP_CELL,
            1,
            "stress-ng",
            7,
            2.8,
            400.0,
            400.0,
            400.0,
            670.0,
            694.0,
            700.0,
            0.99,
            17,
            10000,
            "good",
        ),
        (
            REP_CELL,
            1,
            "system",
            1,
            0.25,
            250.0,
            250.0,
            250.0,
            250.0,
            250.0,
            250.0,
            0.0,
            0,
            0,
            "good",
        ),
        (
            REP_CELL,
            2,
            "stress-ng",
            5,
            1.75,
            350.0,
            350.0,
            350.0,
            530.0,
            546.0,
            550.0,
            0.99,
            17,
            10000,
            "good",
        ),
        (
            REP_CELL,
            3,
            "stress-ng",
            66,
            6.6,
            100.0,
            100.0,
            100.0,
            100.0,
            100.0,
            100.0,
            0.99,
            17,
            10000,
            "good",
        ),
    ]


def _rep1_slices() -> list[tuple]:
    """Replicate 1: 7 stress-ng slices 4.0s..10.0s + 1 system slice at 6.5s."""
    slices = [
        slice_row(REP1_MIN_TS_US + i * 1_000_000, dur)
        for i, dur in enumerate((100, 200, 300, 400, 500, 600, 700))
    ]
    slices.append(slice_row(6_500_000, 250, cpu=1, tid=999, pod="system"))
    return slices


def _rep2_slices() -> list[tuple]:
    """Replicate 2: 5 stress-ng slices 1.0s..5.0s (retained 4.0s)."""
    return [
        slice_row(1_000_000 + i * 1_000_000, dur)
        for i, dur in enumerate((150, 250, 350, 450, 550))
    ]


def _cap_slices() -> list[tuple]:
    """Cap replicate: 66 stress-ng slices 0.0s..130.0s (retained 130.0s)."""
    return [slice_row(i * 2_000_000, 100) for i in range(66)]


def build_family_fixture(root: pathlib.Path) -> pathlib.Path:
    """Write a dist-analyze output tree with two cells under <root>/distribution.

    REP_CELL (request=100m-limit=100m) sorts first, so it is the default
    representative cell. It carries per-replicate slice files
    dist-slices-replicate-{1,2}.csv (retained 6.0s and 4.0s) plus the merged
    dist-slices.csv (which dist-gif must NOT use for timeline rendering).
    NO_LIMIT_CELL (request=none-limit=none) has cpu_max=0 (not a quota cell).
    """
    family_root = root / "distribution" / FAMILY
    rep_dir = family_root / REP_CELL

    write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}1.csv", _rep1_slices())
    write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}2.csv", _rep2_slices())
    # Merged file is emitted by dist-analyze; the revised dist-gif must ignore
    # it for timeline rendering (per-replicate files drive the GIFs).
    merged = pd.concat(
        [
            pd.DataFrame(_rep1_slices(), columns=DIST_SLICES_COLUMNS),
            pd.DataFrame(_rep2_slices(), columns=DIST_SLICES_COLUMNS),
        ],
        ignore_index=True,
    ).sort_values(["ts_start_us", "tid"], kind="mergesort")
    merged.to_csv(rep_dir / "dist-slices.csv", index=False)
    write_summary_csv(rep_dir / "dist-summary.csv", _rep_summary_rows()[:3])

    no_limit_dir = family_root / NO_LIMIT_CELL
    write_slices_csv(
        no_limit_dir / f"{REPLICATE_SLICES_PREFIX}1.csv",
        [slice_row(4_000_000, 150)],
    )
    write_slices_csv(
        no_limit_dir / "dist-slices.csv",
        [slice_row(4_000_000, 150)],
    )
    write_summary_csv(
        no_limit_dir / "dist-summary.csv",
        [
            (
                NO_LIMIT_CELL,
                1,
                "stress-ng",
                1,
                0.15,
                150.0,
                150.0,
                150.0,
                150.0,
                150.0,
                150.0,
                0.0,
                100,
                0,
                "good",
            ),
        ],
    )
    return root


def build_cap_fixture(root: pathlib.Path) -> pathlib.Path:
    """Write a family whose representative cell has ONE replicate spanning
    130.0s (dist-slices-replicate-3.csv) — exercises the 120-frame caps."""
    rep_dir = root / "distribution" / FAMILY / REP_CELL
    write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}3.csv", _cap_slices())
    merged = pd.DataFrame(_cap_slices(), columns=DIST_SLICES_COLUMNS).sort_values(
        ["ts_start_us", "tid"], kind="mergesort"
    )
    merged.to_csv(rep_dir / "dist-slices.csv", index=False)
    write_summary_csv(rep_dir / "dist-summary.csv", _rep_summary_rows()[3:])
    return root


def build_merged_only_fixture(root: pathlib.Path) -> pathlib.Path:
    """Write a family whose representative cell has ONLY the merged
    dist-slices.csv (retained 1.0s) — NO per-replicate slice files. The
    revised dist-gif must refuse to render without per-replicate files."""
    rep_dir = root / "distribution" / FAMILY / REP_CELL
    write_slices_csv(
        rep_dir / "dist-slices.csv",
        [slice_row(4_000_000, 100), slice_row(5_000_000, 100)],
    )
    write_summary_csv(rep_dir / "dist-summary.csv", _rep_summary_rows()[:2])
    return root


def build_empty_fixture(root: pathlib.Path) -> pathlib.Path:
    """Write a family whose representative cell has an EMPTY merged file and
    an EMPTY per-replicate-1 file (0 rows each) — the CLI must fail loudly."""
    rep_dir = root / "distribution" / FAMILY / REP_CELL
    write_slices_csv(rep_dir / f"{REPLICATE_SLICES_PREFIX}1.csv", [])
    write_slices_csv(rep_dir / "dist-slices.csv", [])
    write_summary_csv(
        rep_dir / "dist-summary.csv",
        [
            (
                REP_CELL,
                1,
                "stress-ng",
                0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                17,
                10000,
                "good",
            ),
        ],
    )
    return root


def sha256(path: pathlib.Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_manifest(root: pathlib.Path) -> dict[str, str]:
    """Map relative path -> sha256 for every file under *root*."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root))
            manifest[rel] = sha256(path)
    return manifest


def fixture_rep_slices_df(replicate: int = 1) -> pd.DataFrame:
    """The REP_CELL's per-replicate dist-slices CSV as a DataFrame."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = build_family_fixture(pathlib.Path(d))
        return pd.read_csv(
            root
            / "distribution"
            / FAMILY
            / REP_CELL
            / f"{REPLICATE_SLICES_PREFIX}{replicate}.csv"
        )


def run_ok(
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    family: str = FAMILY,
    extra: list[str] | None = None,
):
    """Run the script against a fixture and return (rc, stderr, visuals dir)."""
    out_dir = tmp_path / "output"
    argv = [
        "--data-dir",
        str(fixture_dir),
        "--output-dir",
        str(out_dir),
        "--family",
        family,
    ] + (extra or [])
    rc, _out, err = run_gif(argv, env=AGG_ENV)
    visuals = out_dir / "distribution" / family / "visuals"
    return rc, err, visuals


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        module = load_dist_gif_module()
        for name in (
            "retained_window_s",
            "timeline_frame_count",
            "hist_frame_count",
            "elapsed_for_frame",
            "annotation_text",
            "representative_cell",
            "quota_cells",
            "replicate_slice_files",
            "window_slice_count",
            "require_pillow",
            "generate_family_gifs",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )

    def test_module_exposes_pinned_constants(self):
        module = load_dist_gif_module()
        assert module.FPS_TIMELINE_DEFAULT == 10
        assert module.FPS_HIST_DEFAULT == 4
        assert module.WINDOW_S_DEFAULT == 2.0
        assert module.STEP_S_DEFAULT == 0.5
        assert module.TIMELINE_MAX_FRAMES == 120
        assert module.HIST_MAX_FRAMES == 120
        assert module.TIMELINE_GIF == TIMELINE_GIF
        assert module.HIST_GIF == HIST_GIF


# =========================================================================
# Frame-count formulas
# =========================================================================


class TestFrameFormulas:
    """timeline_frame_count / hist_frame_count pin the revised formulas:
    integer-microsecond step division with a hard 120-frame cap."""

    def test_timeline_spec_number_capped_120(self):
        """The real 90s-measurement case (82.0s retained) now caps at 120."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(SPEC_RETENTION_S) == SPEC_TIMELINE_FRAMES

    def test_timeline_fixture_replicate1(self):
        """Replicate 1's 6.0s retained window -> min(12, 120) = 12 frames."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(REP1_RETENTION_S) == REP1_TIMELINE_FRAMES

    def test_timeline_caps_at_120(self):
        """Retained windows far above 60s are hard-capped at 120 frames."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(300.0) == 120
        assert module.timeline_frame_count(CAP_RETENTION_S) == CAP_TIMELINE_FRAMES

    def test_timeline_cap_boundary(self):
        """The cap engages at exactly 120 x 0.5s = 60.0s retained."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(60.0) == 120
        assert module.timeline_frame_count(59.5) == 119

    def test_timeline_floors_partial_step(self):
        """A partial trailing step is NOT a frame: 5.9s -> 11, 6.1s -> 12."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(5.9) == 11
        assert module.timeline_frame_count(6.1) == 12

    def test_timeline_zero_window(self):
        """Zero retained window -> zero frames (caller must refuse to render)."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(0.0) == 0

    def test_timeline_custom_step(self):
        """The step flag scales the frame count (integer-microsecond math)."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(82.0, step_s=1.0) == 82
        assert module.timeline_frame_count(REP1_RETENTION_S, step_s=1.5) == 4
        assert module.timeline_frame_count(300.0, step_s=1.0) == 120  # capped

    def test_timeline_max_frames_override_kwarg(self):
        """max_frames is a keyword parameter of the capped formula."""
        module = load_dist_gif_module()
        assert module.timeline_frame_count(300.0, max_frames=50) == 50
        assert module.timeline_frame_count(REP1_RETENTION_S, max_frames=120) == 12

    def test_hist_one_frame_per_second(self):
        """slice-dist-build = min(int(retained_s), 120): 82 -> 82, 6.9 -> 6."""
        module = load_dist_gif_module()
        assert module.hist_frame_count(SPEC_RETENTION_S) == SPEC_HIST_FRAMES
        assert module.hist_frame_count(REP1_RETENTION_S) == REP1_HIST_FRAMES
        assert module.hist_frame_count(6.9) == 6
        assert module.hist_frame_count(0.0) == 0

    def test_hist_capped_at_120(self):
        """slice-dist-build caps at 120 frames for retained >= 120s."""
        module = load_dist_gif_module()
        assert module.hist_frame_count(300.0) == 120
        assert module.hist_frame_count(CAP_RETENTION_S) == CAP_HIST_FRAMES
        assert module.hist_frame_count(121.0) == 120
        assert module.hist_frame_count(120.0) == 120
        assert module.hist_frame_count(119.9) == 119

    def test_hist_max_frames_override_kwarg(self):
        """max_frames is a keyword parameter of the capped formula."""
        module = load_dist_gif_module()
        assert module.hist_frame_count(300.0, max_frames=30) == 30


# =========================================================================
# Per-replicate slice discovery
# =========================================================================


class TestReplicateSlices:
    """Per-replicate slice files drive the timeline; merged slices do not."""

    def test_replicate_slice_files_sorted_numerically(self, tmp_path: pathlib.Path):
        """dist-slices-replicate-<n>.csv discovered in NUMERIC order: a
        replicate-10 file sorts after replicate-2, not before."""
        module = load_dist_gif_module()
        cell_dir = tmp_path / "cell"
        for n in (2, 10, 1):
            write_slices_csv(
                cell_dir / f"{REPLICATE_SLICES_PREFIX}{n}.csv",
                [slice_row(4_000_000, 100)],
            )
        files = module.replicate_slice_files(cell_dir)
        assert [p.name for p in files] == [
            f"{REPLICATE_SLICES_PREFIX}1.csv",
            f"{REPLICATE_SLICES_PREFIX}2.csv",
            f"{REPLICATE_SLICES_PREFIX}10.csv",
        ]

    def test_replicate_slice_files_empty_when_absent(self, tmp_path: pathlib.Path):
        """A cell with only merged dist-slices.csv has NO per-replicate files."""
        module = load_dist_gif_module()
        cell_dir = tmp_path / "cell"
        write_slices_csv(
            cell_dir / "dist-slices.csv",
            [slice_row(4_000_000, 100), slice_row(5_000_000, 100)],
        )
        assert module.replicate_slice_files(cell_dir) == []

    def test_generate_family_gifs_requires_replicate_files(
        self, tmp_path: pathlib.Path
    ):
        """Rendering from the merged file alone is FORBIDDEN: per-replicate
        files are required (raises ValueError naming replicate)."""
        module = load_dist_gif_module()
        root = build_merged_only_fixture(tmp_path / "fixture")
        with pytest.raises(ValueError, match="replicate"):
            module.generate_family_gifs(
                data_dir=root,
                output_dir=tmp_path / "out",
                family=FAMILY,
            )


# =========================================================================
# Retained-window derivation
# =========================================================================


class TestRetainedWindow:
    """retained_window_s is derived from the pinned dist-slices.csv data."""

    def test_retained_window_from_slices_df(self):
        module = load_dist_gif_module()
        df = fixture_rep_slices_df(1)
        assert module.retained_window_s(df) == pytest.approx(REP1_RETENTION_S, abs=1e-9)

    def test_retained_window_empty_df_zero(self):
        module = load_dist_gif_module()
        empty = pd.DataFrame(columns=DIST_SLICES_COLUMNS)
        assert module.retained_window_s(empty) == 0.0

    def test_retained_window_single_row_zero(self):
        module = load_dist_gif_module()
        one = pd.DataFrame(
            [
                {
                    "ts_start_us": 4_000_000,
                    "ts_end_us": 4_000_100,
                    "duration_us": 100,
                    "cpu": 0,
                    "tid": 1001,
                    "thread_name": "stress-ng-cpu",
                    "pod": "stress-ng",
                }
            ]
        )
        assert module.retained_window_s(one) == 0.0


# =========================================================================
# Per-frame window-only bar rendering
# =========================================================================


class TestWindowBarCount:
    """window_slice_count proves each frame draws ONLY the slice bars whose
    start falls in the moving 2s window — the count scales with the window
    content, never with the full slice count."""

    def test_window_slice_count_rep1_frames(self):
        """Replicate 1 fixture: frame windows contain the expected starts."""
        module = load_dist_gif_module()
        df = fixture_rep_slices_df(1)
        # frame 0: [4.0, 6.0) -> starts at 4.0, 5.0
        assert module.window_slice_count(df, 0) == 2
        # frame 2: [5.0, 7.0) -> starts at 5.0, 6.0, 6.5 (system)
        assert module.window_slice_count(df, 2) == 3
        # frame 11 (last): [9.5, 11.5) -> starts at 10.0
        assert module.window_slice_count(df, REP1_TIMELINE_FRAMES - 1) == 1

    def test_window_slice_count_never_full_slice_count(self):
        """No frame draws all 8 slices: max per-frame count < len(df)."""
        module = load_dist_gif_module()
        df = fixture_rep_slices_df(1)
        per_frame = [
            module.window_slice_count(df, i) for i in range(REP1_TIMELINE_FRAMES)
        ]
        assert max(per_frame) < len(df)

    def test_window_slice_count_scales_with_density(self):
        """A dense 0.1s grid: per-frame count ~= window width x density (20),
        far below the 101-row full slice count."""
        module = load_dist_gif_module()
        dense = pd.DataFrame(
            [
                {
                    "ts_start_us": i * 100_000,
                    "ts_end_us": i * 100_000 + 100,
                    "duration_us": 100,
                    "cpu": 0,
                    "tid": 1001,
                    "thread_name": "stress-ng-cpu",
                    "pod": "stress-ng",
                }
                for i in range(101)  # 0.0s..10.0s
            ]
        )
        assert module.window_slice_count(dense, 0) == 20  # [0.0, 2.0)
        assert module.window_slice_count(dense, 10) == 20  # [5.0, 7.0)
        assert module.window_slice_count(dense, 0) < len(dense)

    def test_window_slice_count_frame_past_end_and_empty(self):
        """A frame whose window lies past the data end yields 0; empty df -> 0."""
        module = load_dist_gif_module()
        df = fixture_rep_slices_df(1)
        assert module.window_slice_count(df, 20) == 0
        empty = pd.DataFrame(columns=DIST_SLICES_COLUMNS)
        assert module.window_slice_count(empty, 0) == 0


# =========================================================================
# Per-frame annotation (config label, cell, elapsed time)
# =========================================================================


class TestAnnotation:
    """elapsed time is data-derived (never wall-clock); format is pinned."""

    def test_elapsed_for_frame_derives_from_data(self):
        module = load_dist_gif_module()
        df = fixture_rep_slices_df(1)
        # min ts_start is 4.0s (fixture); frame i elapsed = 4.0 + i * 0.5
        assert module.elapsed_for_frame(df, 0) == pytest.approx(4.0, abs=1e-9)
        assert module.elapsed_for_frame(df, 1) == pytest.approx(4.5, abs=1e-9)
        assert module.elapsed_for_frame(df, 5) == pytest.approx(6.5, abs=1e-9)
        assert module.elapsed_for_frame(df, REP1_TIMELINE_FRAMES - 1) == pytest.approx(
            4.0 + (REP1_TIMELINE_FRAMES - 1) * 0.5, abs=1e-9
        )

    def test_annotation_text_pinned_format(self):
        module = load_dist_gif_module()
        text = module.annotation_text(FAMILY, REP_CELL, 4.0)
        assert text == "dist-stress-ng | request=100m-limit=100m | elapsed 4.0s"
        assert FAMILY in text
        assert REP_CELL in text


# =========================================================================
# Representative cell selection + quota-cell decision
# =========================================================================


class TestCellSelection:
    """One representative cell drives the GIFs (default = first sorted)."""

    def test_representative_cell_default_first_sorted(self, tmp_path: pathlib.Path):
        module = load_dist_gif_module()
        root = build_family_fixture(tmp_path / "fixture")
        family_dir = root / "distribution" / FAMILY
        assert module.representative_cell(family_dir) == REP_CELL

    def test_representative_cell_explicit_and_missing(self, tmp_path: pathlib.Path):
        module = load_dist_gif_module()
        root = build_family_fixture(tmp_path / "fixture")
        family_dir = root / "distribution" / FAMILY
        assert (
            module.representative_cell(family_dir, cell=NO_LIMIT_CELL) == NO_LIMIT_CELL
        )
        with pytest.raises(ValueError):
            module.representative_cell(tmp_path / "no-such-family")


class TestQuotaCells:
    """Cells with cpu_max > 0 in dist-summary.csv are quota cells (hatching)."""

    def test_quota_cell_detected_from_summary(self, tmp_path: pathlib.Path):
        module = load_dist_gif_module()
        root = build_family_fixture(tmp_path / "fixture")
        family_dir = root / "distribution" / FAMILY
        assert REP_CELL in module.quota_cells(family_dir)

    def test_no_limit_cell_not_quota(self, tmp_path: pathlib.Path):
        module = load_dist_gif_module()
        root = build_family_fixture(tmp_path / "fixture")
        family_dir = root / "distribution" / FAMILY
        assert NO_LIMIT_CELL not in module.quota_cells(family_dir)


# =========================================================================
# Pillow unavailable: clear message naming Pillow
# =========================================================================


class TestPillowMissing:
    """Simulate a missing Pillow via sys.modules — works regardless of the
    host's Pillow installation (PIL -> None makes any subsequent PIL import
    raise ImportError)."""

    def _block_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)

    def test_require_pillow_raises_naming_pillow_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        module = load_dist_gif_module()
        self._block_pillow(monkeypatch)
        with pytest.raises(Exception) as exc:
            module.require_pillow()
        assert "pillow" in str(exc.value).lower()

    def test_generate_family_gifs_raises_naming_pillow_when_unavailable(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ):
        module = load_dist_gif_module()
        root = build_family_fixture(tmp_path / "fixture")
        self._block_pillow(monkeypatch)
        with pytest.raises(Exception) as exc:
            module.generate_family_gifs(
                data_dir=root,
                output_dir=tmp_path / "out",
                family=FAMILY,
            )
        assert "pillow" in str(exc.value).lower()


# =========================================================================
# CLI
# =========================================================================


class TestCli:
    """--data-dir/--output-dir/--family + tuning flags contract."""

    def test_help_prints_usage_and_all_flags(self):
        rc, out, err = run_gif(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        for flag in (
            "--data-dir",
            "--output-dir",
            "--family",
            "--cell",
            "--window-s",
            "--step-s",
            "--fps-timeline",
            "--fps-hist",
        ):
            assert flag in combined, f"missing flag in help: {flag}"

    def test_missing_required_flags_exits_nonzero(self):
        rc, _out, err = run_gif([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_family_dir_exits_nonzero(self, tmp_path: pathlib.Path):
        rc, _out, err = run_gif(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                "no-such-family",
            ],
            env=AGG_ENV,
        )
        assert rc != 0
        assert "no-such-family" in err or "missing" in err.lower()

    def test_zero_fps_rejected(self, tmp_path: pathlib.Path):
        root = build_family_fixture(tmp_path / "fixture")
        rc, _out, err = run_gif(
            [
                "--data-dir",
                str(root),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--fps-timeline",
                "0",
            ],
            env=AGG_ENV,
        )
        assert rc != 0
        assert "fps" in err.lower()

    def test_zero_step_rejected(self, tmp_path: pathlib.Path):
        root = build_family_fixture(tmp_path / "fixture")
        rc, _out, err = run_gif(
            [
                "--data-dir",
                str(root),
                "--output-dir",
                str(tmp_path / "out"),
                "--family",
                FAMILY,
                "--step-s",
                "0",
            ],
            env=AGG_ENV,
        )
        assert rc != 0
        assert "step" in err.lower(), f"expected a step-validation message, got: {err}"


# =========================================================================
# End-to-end: staged dist-analyze output -> per-replicate, capped, deterministic
# =========================================================================


class TestEndToEnd:
    """The GIF pipeline via the CLI."""

    def test_per_replicate_timeline_gifs_written(self, tmp_path: pathlib.Path):
        """One exec-timeline GIF per replicate + canonical + histogram GIF."""
        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        expected = [
            TIMELINE_GIF,
            REPLICATE_TIMELINE_TMPL.format(n=1),
            REPLICATE_TIMELINE_TMPL.format(n=2),
            HIST_GIF,
        ]
        for name in expected:
            path = visuals / name
            assert path.is_file(), f"missing GIF: {path}"
            assert path.stat().st_size > 0, f"empty GIF: {path}"
            assert path.read_bytes()[:6] == b"GIF89a", f"not GIF89a: {path}"

    def test_canonical_exec_timeline_identical_to_replicate_1(
        self, tmp_path: pathlib.Path
    ):
        """exec-timeline.gif is replicate 1's GIF (canonical name)."""
        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        canonical = visuals / TIMELINE_GIF
        rep1 = visuals / REPLICATE_TIMELINE_TMPL.format(n=1)
        assert canonical.is_file()
        assert rep1.is_file()
        assert sha256(canonical) == sha256(rep1)

    def test_exec_timeline_replicate_1_frame_count_and_duration(
        self, tmp_path: pathlib.Path
    ):
        """Replicate 1 (6.0s retained) -> 12 frames at ~100ms (10 fps)."""
        from PIL import Image

        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        im = Image.open(visuals / REPLICATE_TIMELINE_TMPL.format(n=1))
        assert im.format == "GIF"
        assert im.is_animated
        assert im.n_frames == REP1_TIMELINE_FRAMES, (
            f"expected {REP1_TIMELINE_FRAMES} frames, got {im.n_frames}"
        )
        # PillowWriter(fps=10) writes duration = int(1000/10) = 100 ms.
        assert im.info.get("duration") == pytest.approx(100, abs=25)

    def test_exec_timeline_replicate_2_frame_count(self, tmp_path: pathlib.Path):
        """Replicate 2 (4.0s retained) -> 8 frames (its own window)."""
        from PIL import Image

        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        im = Image.open(visuals / REPLICATE_TIMELINE_TMPL.format(n=2))
        assert im.is_animated
        assert im.n_frames == REP2_TIMELINE_FRAMES, (
            f"expected {REP2_TIMELINE_FRAMES} frames, got {im.n_frames}"
        )

    def test_exec_timeline_replicate_capped_at_120(self, tmp_path: pathlib.Path):
        """A 130.0s replicate renders EXACTLY 120 timeline frames (cap)."""
        from PIL import Image

        root = build_cap_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        im = Image.open(visuals / REPLICATE_TIMELINE_TMPL.format(n=3))
        assert im.is_animated
        assert im.n_frames == CAP_TIMELINE_FRAMES, (
            f"expected {CAP_TIMELINE_FRAMES} frames, got {im.n_frames}"
        )
        # The histogram caps too: one frame per second, max 120.
        hist = Image.open(visuals / HIST_GIF)
        assert hist.n_frames == CAP_HIST_FRAMES, (
            f"expected {CAP_HIST_FRAMES} hist frames, got {hist.n_frames}"
        )

    def test_slice_dist_build_gif_valid_and_frame_count(self, tmp_path: pathlib.Path):
        """slice-dist-build.gif: 6 frames from replicate 1 at ~250ms (4 fps)."""
        from PIL import Image

        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc == 0, f"stderr: {err}"
        im = Image.open(visuals / HIST_GIF)
        assert im.format == "GIF"
        assert im.is_animated
        assert im.n_frames == REP1_HIST_FRAMES, (
            f"expected {REP1_HIST_FRAMES} frames, got {im.n_frames}"
        )
        # PillowWriter(fps=4) writes duration = int(1000/4) = 250 ms.
        assert im.info.get("duration") == pytest.approx(250, abs=50)

    def test_fps_timeline_override_changes_frame_duration(self, tmp_path: pathlib.Path):
        """--fps-timeline 5 -> ~200 ms per timeline frame, count unchanged."""
        from PIL import Image

        root = build_family_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path, extra=["--fps-timeline", "5"])
        assert rc == 0, f"stderr: {err}"
        im = Image.open(visuals / REPLICATE_TIMELINE_TMPL.format(n=1))
        assert im.n_frames == REP1_TIMELINE_FRAMES
        # PillowWriter(fps=5) -> int(1000/5) = 200 ms.
        assert im.info.get("duration") == pytest.approx(200, abs=50)

    def test_two_runs_byte_identical(self, tmp_path: pathlib.Path):
        """Determinism: identical SHA-256 manifests across two
        runs on the same staged data — no wall-clock values in the GIFs."""
        root = build_family_fixture(tmp_path / "fixture")
        rc1, err1, visuals1 = run_ok(root, tmp_path)
        assert rc1 == 0, f"first run failed: {err1}"
        rc2, err2, visuals2 = run_ok(root, tmp_path)
        assert rc2 == 0, f"second run failed: {err2}"
        m1 = sha256_manifest(visuals1)
        m2 = sha256_manifest(visuals2)
        assert m1 == m2
        assert set(m1.keys()) == {
            TIMELINE_GIF,
            REPLICATE_TIMELINE_TMPL.format(n=1),
            REPLICATE_TIMELINE_TMPL.format(n=2),
            HIST_GIF,
        }
        assert m1[TIMELINE_GIF] == m1[REPLICATE_TIMELINE_TMPL.format(n=1)]

    def test_missing_replicate_slices_fails_loudly(self, tmp_path: pathlib.Path):
        """A cell with only merged dist-slices.csv must be refused (per-replicate
        files are the only valid timeline input)."""
        root = build_merged_only_fixture(tmp_path / "fixture")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc != 0, "merged-only data must fail loudly (no per-replicate files)"
        assert "replicate" in err.lower(), f"expected a replicate message, got: {err}"
        assert not (visuals / TIMELINE_GIF).exists()
        assert not (visuals / HIST_GIF).exists()

    def test_empty_replicate_slices_fails_loudly(self, tmp_path: pathlib.Path):
        """A zero-frame GIF is un-openable; the CLI must refuse to render it."""
        root = build_empty_fixture(tmp_path / "empty")
        rc, err, visuals = run_ok(root, tmp_path)
        assert rc != 0, "empty data must fail loudly (no zero-frame GIF)"
        assert any(
            s in err
            for s in ("no slices", "nothing to animate", "retained window", "replicate")
        ), f"expected a clear no-data message, got: {err}"
        assert not (visuals / TIMELINE_GIF).exists()
        assert not (visuals / HIST_GIF).exists()
