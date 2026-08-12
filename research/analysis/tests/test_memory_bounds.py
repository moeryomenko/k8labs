"""Memory-bound tests — subprocess RSS harness + budget gates.

Pins the peak-RSS budgets for the four analysis stages (dist-analyze
<= 3 GB, dist-plot <= 2 GB, dist-gif <= 2 GB, dist-steps <= 1.5 GB) via a
subprocess wrapper that reports the STAGE's own peak RSS.

REVISION: the wrapper (``memory_harness.py``) now runs the stage in
a GRANDCHILD process and measures it via ``RUSAGE_CHILDREN`` in a freshly
forked monitor, so the reported peak is the stage's own peak and NOT an
inherited ``ru_maxrss`` counter.  On Linux, ``exec`` folds the parent's
current RSS into the child's ``signal->maxrss``; with the old in-process
wrapper the session-scoped gif fixture build (~3.07 GiB peak in the pytest
parent) polluted the stage measurement.  See memory_harness.py's REVISION
note for the semantics.

Fixture sizing (chosen so the CURRENT implementation exceeds each budget —
RED — while the memory-optimized rewrites pass; measured on the dev host in
Aug 2026, 30 Gi RAM / 22 Gi available / 16 cores):

    dist-analyze: 4,000,000 sched_slice rows per replicate x 3 replicates,
                  served through a cached pickle-backed fake perfetto
                  TraceProcessor -> current peak 4.96 GB (budget 3 GB)
    dist-plot:    2,000,000 rows in the two non-representative big cells
                  (representative cell stays small so the per-slice Gantt
                  does not dominate wall time) -> current peak 4.42 GB
                  (budget 2 GB)
    dist-gif:     10,000,000 rows x 3 per-replicate files in the
                  representative cell -> current peak 2.97 GB (budget 2 GB)
    dist-steps:   150,000 rows in the step-3 and step-4 cells -> current
                  peak 3.40 GB (budget 1.5 GB)

All fixtures are synthetic, deterministic, offline (no cluster, no network,
no real traces).  The memory budget tests are marked ``memory`` so the
default suite (``pytest -m "not memory"`` via pyproject addopts) stays fast
and green; run them explicitly with ``python3 -m pytest -m memory``.

Run from research/cpu-sched/analysis:
    python3 -m pytest tests/test_memory_bounds.py -q                 (harness)
    python3 -m pytest -m memory tests/test_memory_bounds.py -v       (budgets)
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ANALYSIS_DIR = Path(__file__).resolve().parent.parent
WRAPPER_PATH = Path(__file__).resolve().parent / "memory_harness.py"

DIST_ANALYZE_SCRIPT = ANALYSIS_DIR / "dist-analyze.py"
DIST_PLOT_SCRIPT = ANALYSIS_DIR / "dist-plot.py"
DIST_GIF_SCRIPT = ANALYSIS_DIR / "dist-gif.py"
DIST_STEPS_SCRIPT = ANALYSIS_DIR / "dist-steps.py"

# ---------------------------------------------------------------------------
# Pinned budget constants
# ---------------------------------------------------------------------------

# Nominal per-stage peak-RSS budget in MiB.  These are the contract values
# the optimization work must satisfy; verified on real R3 data.
BUDGETS_MIB = {
    "dist-analyze": 3 * 1024,
    "dist-plot": 2 * 1024,
    "dist-gif": 2 * 1024,
    "dist-steps": int(1.5 * 1024),
}

# Flakiness margin applied ONLY at the test assertion level (a loaded host
# may push RSS a few percent either way).  The nominal BUDGETS_MIB remain
# the contract; the margin is a documented test tolerance.
RSS_TOLERANCE = 1.10

PEAK_MARKER = "PEAK_RSS_KB="

FAMILY = "dist-stress-ng"

# (cell label, cpu_weight, cpu_max_quota, throttle_ratio) — the pinned
# six-cell Family A stress-ng order used by the dist-* scripts (same labels
# as tests/conftest.py DIST_STEPS_CELL_SPECS).
MEM_CELL_SPECS = [
    ("request=-limit=", 1, 100000, 0.0),
    ("request=100m-limit=100m", 17, 10000, 0.99),
    ("request=100m-limit=1000m", 17, 100000, 0.97),
    ("request=500m-limit=500m", 59, 50000, 0.96),
    ("request=500m-limit=2000m", 59, 200000, 0.01),
    ("request=1000m-limit=2000m", 100, 200000, 0.02),
]
MEM_CELLS = [spec[0] for spec in MEM_CELL_SPECS]

# Fixture sizes (rows) — see module docstring for the measured rationale.
ANALYZE_ROWS_PER_REP = 4_000_000
ANALYZE_REPS = 3
PLOT_BIG_CELLS = (0, 3)
PLOT_BIG_ROWS = 2_000_000
PLOT_SMALL_ROWS = 10
STEPS_BIG_CELLS = (0, 3)
STEPS_BIG_ROWS = 150_000
STEPS_SMALL_ROWS = 10
GIF_BIG_ROWS = 10_000_000
GIF_REPS = 3
GIF_SMALL_ROWS = 10

SLICES_COLUMNS = [
    "ts_start_us",
    "ts_end_us",
    "duration_us",
    "cpu",
    "tid",
    "thread_name",
    "pod",
]
SUMMARY_COLUMNS = [
    "cell",
    "replicate",
    "pod",
    "slice_count",
    "total_exec_ms",
    "mean_us",
    "median_us",
    "p50_us",
    "p95_us",
    "p99_us",
    "max_us",
    "throttle_ratio",
    "cpu_weight",
    "cpu_max",
    "quality",
]
RUNTIME_COLUMNS = [
    "ts",
    "cpu",
    "pid",
    "tid",
    "thread_name",
    "pod",
    "runtime_ns",
]
PERCENTILE_STEPS = (1, 11, 21, 31, 41, 51, 61, 71, 81, 91, 99)


# ---------------------------------------------------------------------------
# Synthetic slice/runtime row builders (deterministic, no RNG)
# ---------------------------------------------------------------------------


def make_slice_frame(
    n_rows: int, pod: str = "stress-ng", tid: int = 1001
) -> pd.DataFrame:
    """Deterministic sched_slice DataFrame with the pinned SLICES_COLUMNS.

    ts_start_us = 2_500_000 + i * 1000, duration_us = (i % 1000) + 50 —
    the same pattern used across the memory fixtures so coverage stats and
    percentile monotonicity hold at every size.
    """
    idx = np.arange(0, n_rows, dtype=np.int64)
    starts = idx * 1000 + 2_500_000
    durs = (idx % 1000) + 50
    return pd.DataFrame(
        {
            "ts_start_us": starts,
            "ts_end_us": starts + durs,
            "duration_us": durs.astype(float),
            "cpu": np.zeros(n_rows, dtype=np.int64),
            "tid": np.full(n_rows, tid, dtype=np.int64),
            "thread_name": np.full(n_rows, "stress-ng-cpu", dtype=object),
            "pod": np.full(n_rows, pod, dtype=object),
        },
        columns=pd.Index(SLICES_COLUMNS),
    )


def make_runtime_frame(
    n_rows: int, pod: str = "stress-ng", tid: int = 1001
) -> pd.DataFrame:
    """Deterministic sched_stat_runtime DataFrame with pinned RUNTIME_COLUMNS."""
    idx = np.arange(0, n_rows, dtype=np.int64)
    starts = idx * 1000 + 2_500_000
    durs = (idx % 1000) + 50
    return pd.DataFrame(
        {
            "ts": starts,
            "cpu": np.zeros(n_rows, dtype=np.int64),
            "pid": np.full(n_rows, tid, dtype=np.int64),
            "tid": np.full(n_rows, tid, dtype=np.int64),
            "thread_name": np.full(n_rows, "stress-ng-cpu", dtype=object),
            "pod": np.full(n_rows, pod, dtype=object),
            "runtime_ns": (durs * 1000).astype(np.int64),
        },
        columns=pd.Index(RUNTIME_COLUMNS),
    )


def make_summary_frame(
    cell: str, n_rows: int, weight: int, quota: int, ratio: float
) -> pd.DataFrame:
    """One-row dist-summary.csv frame with pinned SUMMARY_COLUMNS."""
    durs = ((np.arange(n_rows, dtype=np.int64) % 1000) + 50).astype(float)
    series = pd.Series(durs)
    return pd.DataFrame(
        [
            [
                cell,
                1,
                "stress-ng",
                n_rows,
                float(durs.sum()) / 1000.0,
                float(series.mean()),
                float(series.median()),
                float(series.quantile(0.50)),
                float(series.quantile(0.95)),
                float(series.quantile(0.99)),
                float(series.max()),
                ratio,
                weight,
                quota,
                "good",
            ]
        ],
        columns=pd.Index(SUMMARY_COLUMNS),
    )


def make_percentiles_json(n_rows: int) -> str:
    """dist-percentiles.json content for the synthetic durations."""
    series = pd.Series(((np.arange(n_rows, dtype=np.int64) % 1000) + 50).astype(float))
    table = {f"p{k}": float(series.quantile(k / 100.0)) for k in PERCENTILE_STEPS}
    return json.dumps({"1": {"stress-ng": table}}, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# dist-analyze fixture: run-data dir + cached pickle-backed fake perfetto
# ---------------------------------------------------------------------------

# Source of the fake perfetto.trace_processor package used by the dist-analyze
# memory subprocess.  Unlike tests/conftest.py's JSON-backed fake, this one
# loads a PICKLE ONCE (module-level cache) so a 4M-row dataset is not
# re-parsed per query — keeping the memory test tractable.
FAKE_PERFETTO_TP_SOURCE = """
import os
import pickle

import pandas as pd

_CACHE = {}


def _load():
    if "data" not in _CACHE:
        with open(os.environ["DIST_MEM_FAKE_DATA"], "rb") as f:
            _CACHE["data"] = pickle.load(f)
    return _CACHE["data"]


class QueryResult:
    def __init__(self, df):
        self._df = df

    def __iter__(self):
        return iter(self._df.itertuples(index=False))

    def as_pandas_dataframe(self):
        return self._df


class TraceProcessor:
    def __init__(self, *args, **kwargs):
        pass

    def query(self, sql):
        data = _load()
        sql_lower = sql.lower()
        if "sched_stat_runtime" in sql_lower:
            return QueryResult(data["runtime"])
        if "sched_slice" in sql_lower and "min(ts)" in sql_lower:
            return QueryResult(pd.DataFrame([data["bounds"]]))
        if "sched_slice" in sql_lower:
            return QueryResult(data["slices"])
        return QueryResult(pd.DataFrame())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        pass
"""


def _build_analyze_run_data(root: Path, cell: str, reps: int) -> Path:
    """Write one cell's run-data dir (traces/pids/cgroup/metadata markers).

    The sanity gate passes: ratio 0.99 >= 0.95 for the 100m limit < 2000m
    demand cell, quota 10000 us; percentile monotonicity holds on the
    synthetic durations.
    """
    for rep in range(1, reps + 1):
        rep_dir = root / cell / f"replicate-{rep}"
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / "perfetto-trace.perfetto-trace").write_bytes(b"HPb\x00\x01")
        (rep_dir / "eevdf-stress-ng-pids.csv").write_text("pod,pid\nstress-ng,1001\n")
        (rep_dir / "cgroup-stress-ng.csv").write_text(
            "timestamp,pod,container,nr_periods,nr_throttled,throttled_usec,"
            "usage_usec,cpu_weight,cpu_max_quota,cpu_max_period\n"
            "2026-08-05T10:00:05Z,stress-ng,stress-ng,1000,990,5000,100000,"
            "17,10000,100000\n"
        )
        (rep_dir / "metadata.json").write_text(
            json.dumps({"replicate": rep, "pod_name": "stress-ng"}) + "\n"
        )
    return root


def _build_analyze_fake_env(root: Path, n_rows: int) -> tuple[dict[str, str], Path]:
    """Write the fake perfetto package + pickle dataset; return (env, data_dir).

    The dataset (slices + runtime DataFrames of ``n_rows`` each + trace
    bounds) is shared by all ``reps`` replicates — deterministic, and it
    exercises the current implementation's per-replicate materialization
    and 3-replicate accumulation hotspots.
    """
    pkg_root = root / "fake-perfetto"
    pkg = pkg_root / "perfetto"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "trace_processor.py").write_text(FAKE_PERFETTO_TP_SOURCE)

    pickle_path = root / "dist-mem-fake.pkl"
    data = {
        "slices": make_slice_frame(n_rows),
        "runtime": make_runtime_frame(n_rows),
        "bounds": {"first_ts_ns": 2_000_000_000, "last_ts_ns": 88_000_000_000},
    }
    with open(pickle_path, "wb") as f:
        pickle.dump(data, f, protocol=4)

    data_dir = _build_analyze_run_data(
        root / "run-data", "request=100m-limit=100m", ANALYZE_REPS
    )

    env = {
        "DIST_MEM_FAKE_DATA": str(pickle_path),
        "PYTHONPATH": str(pkg_root)
        + os.pathsep
        + str(ANALYSIS_DIR)
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
        "MPLBACKEND": "Agg",
    }
    return env, data_dir


# ---------------------------------------------------------------------------
# dist-analyze-OUTPUT tree fixtures (dist-plot / dist-steps / dist-gif)
# ---------------------------------------------------------------------------


def _write_cell_outputs(
    root: Path,
    spec_idx: int,
    n_rows: int,
    *,
    with_runtime: bool,
    with_replicates: int | None = None,
) -> Path:
    """Write one cell's dist-analyze OUTPUT files under ``root``."""
    cell, weight, quota, ratio = MEM_CELL_SPECS[spec_idx]
    cell_dir = root / "distribution" / FAMILY / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    make_slice_frame(n_rows).to_csv(cell_dir / "dist-slices.csv", index=False)
    make_summary_frame(cell, n_rows, weight, quota, ratio).to_csv(
        cell_dir / "dist-summary.csv", index=False
    )
    (cell_dir / "dist-percentiles.json").write_text(make_percentiles_json(n_rows))
    if with_runtime:
        make_runtime_frame(n_rows).to_csv(cell_dir / "dist-runtime.csv", index=False)
    if with_replicates is not None:
        slices = make_slice_frame(n_rows)
        for rep in range(1, with_replicates + 1):
            slices.to_csv(cell_dir / f"dist-slices-replicate-{rep}.csv", index=False)
    return cell_dir


def _build_plot_tree(root: Path) -> Path:
    """Six-cell tree; cells 0 and 3 carry PLOT_BIG_ROWS slices+runtime.

    The representative cell (highest throttle_ratio = request=100m-limit=100m,
    cell 1) stays at PLOT_SMALL_ROWS so the per-slice Gantt render does not
    dominate wall time; the memory blowup of the CURRENT implementation comes
    from load_family_data() materializing every cell's slices/runtime as
    Python dicts at load time.
    """
    for idx in range(len(MEM_CELL_SPECS)):
        n = PLOT_BIG_ROWS if idx in PLOT_BIG_CELLS else PLOT_SMALL_ROWS
        _write_cell_outputs(root, idx, n, with_runtime=True)
    return root


def _build_steps_tree(root: Path) -> Path:
    """Six-cell tree; step-3 (cell 0) and step-4 (cell 3) carry big slices.

    The CURRENT implementation renders step-3 with ``bins=len(slices)`` and
    step-4 with one ``ax.bar`` artist per slice — both explode on the big
    cells while the small cells keep steps 1/2/5/6 cheap.
    """
    for idx in range(len(MEM_CELL_SPECS)):
        n = STEPS_BIG_ROWS if idx in STEPS_BIG_CELLS else STEPS_SMALL_ROWS
        _write_cell_outputs(root, idx, n, with_runtime=False)
    return root


def _build_gif_tree(root: Path) -> Path:
    """Six-cell tree; the representative (sorted-first) cell carries 3 big
    per-replicate files — the current implementation holds ALL replicate
    DataFrames in ``reps`` simultaneously.
    """
    for idx in range(len(MEM_CELL_SPECS)):
        n = GIF_BIG_ROWS if idx == 0 else GIF_SMALL_ROWS
        _write_cell_outputs(
            root,
            idx,
            n,
            with_runtime=False,
            with_replicates=GIF_REPS if idx == 0 else None,
        )
    return root


# ---------------------------------------------------------------------------
# Session-scoped fixtures (generated once per pytest session; memory tests only)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mem_fixtures_root(tmp_path_factory) -> Path:
    """Shared root for all memory fixtures (built once per session)."""
    return tmp_path_factory.mktemp("mem-01")


@pytest.fixture(scope="session")
def analyze_env_and_data(mem_fixtures_root: Path) -> tuple[dict[str, str], Path]:
    """dist-analyze fixture: (env with fake perfetto, run-data dir)."""
    return _build_analyze_fake_env(mem_fixtures_root / "analyze", ANALYZE_ROWS_PER_REP)


@pytest.fixture(scope="session")
def plot_data_dir(mem_fixtures_root: Path) -> Path:
    """dist-plot fixture: analysis root with the six-cell tree."""
    return _build_plot_tree(mem_fixtures_root / "plot")


@pytest.fixture(scope="session")
def steps_data_dir(mem_fixtures_root: Path) -> Path:
    """dist-steps fixture: analysis root with the six-cell tree."""
    return _build_steps_tree(mem_fixtures_root / "steps")


@pytest.fixture(scope="session")
def gif_data_dir(mem_fixtures_root: Path) -> Path:
    """dist-gif fixture: analysis root with per-replicate files."""
    return _build_gif_tree(mem_fixtures_root / "gif")


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------


def run_stage_measured(
    script: Path,
    argv: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    timeout_s: int = 2400,
) -> tuple[int, int, float, str]:
    """Run *script* under the RSS wrapper; return (rc, peak_kb, wall_s, stderr).

    peak_kb is parsed from the wrapper's LAST ``PEAK_RSS_KB=<value>`` stdout
    line — the STAGE's own peak RSS (the wrapper's monitor measures the stage
    grandchild via ``getrusage(RUSAGE_CHILDREN).ru_maxrss``, Linux KiB).
    Raises AssertionError when the marker is missing (a wrapper/launch
    failure, never a silent 0).
    """
    env = dict(os.environ)
    env["MPLBACKEND"] = "Agg"
    env["PYTHONPATH"] = str(ANALYSIS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(WRAPPER_PATH), str(script), *argv],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    wall_s = time.monotonic() - t0

    peak_kb: int | None = None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(PEAK_MARKER):
            peak_kb = int(line.split("=", 1)[1])
            break
    if peak_kb is None:
        raise AssertionError(
            f"wrapper did not emit {PEAK_MARKER!r} for {script.name}; "
            f"rc={proc.returncode} stdout_tail={proc.stdout[-300:]!r} "
            f"stderr_tail={proc.stderr[-500:]!r}"
        )
    return proc.returncode, peak_kb, wall_s, proc.stderr


def _assert_within_budget(stage: str, peak_kb: int) -> None:
    """Assert a stage's measured peak RSS stays within its pinned budget."""
    budget_mib = BUDGETS_MIB[stage]
    assert_kb = int(budget_mib * 1024 * RSS_TOLERANCE)
    assert peak_kb <= assert_kb, (
        f"{stage} peak RSS {peak_kb / 1024:.0f} MiB exceeds budget "
        f"{budget_mib} MiB (assertion tolerance {RSS_TOLERANCE:.0%})"
    )


# ---------------------------------------------------------------------------
# Harness smoke tests (fast, run in the default suite)
# ---------------------------------------------------------------------------


def test_budget_constants_pinned() -> None:
    """Peak-RSS budgets are pinned exactly as specified."""
    assert BUDGETS_MIB == {
        "dist-analyze": 3 * 1024,
        "dist-plot": 2 * 1024,
        "dist-gif": 2 * 1024,
        "dist-steps": int(1.5 * 1024),
    }


def test_wrapper_reports_child_peak_rss() -> None:
    """The harness parses a real, positive PEAK_RSS_KB from a tiny stage run."""
    rc, peak_kb, wall_s, stderr = run_stage_measured(
        DIST_PLOT_SCRIPT,
        [
            "--data-dir",
            str(Path(__file__).resolve().parent / ".."),
            "--output-dir",
            str(ANALYSIS_DIR / "output"),
            "--family",
            "does-not-exist-mem-01-smoke",
            "--cells",
            "x",
        ],
        timeout_s=120,
    )
    assert peak_kb > 0, "harness must report a positive peak RSS"
    assert wall_s >= 0.0


def test_wrapper_propagates_stage_exit_code() -> None:
    """A stage failing loudly must surface its non-zero exit code."""
    rc, _peak_kb, _wall_s, stderr = run_stage_measured(
        DIST_PLOT_SCRIPT,
        [
            "--data-dir",
            str(Path(__file__).resolve().parent / ".."),
            "--output-dir",
            str(ANALYSIS_DIR / "output"),
            "--family",
            "does-not-exist-mem-01-smoke",
            "--cells",
            "x",
        ],
        timeout_s=120,
    )
    assert rc == 1, "missing family must fail loudly (exit 1)"


def test_fixture_tree_matches_pinned_schema(tmp_path: Path) -> None:
    """The synthetic tree builders emit the pinned dist-analyze OUTPUT schema."""
    root = _build_plot_tree(tmp_path / "tree")
    cell_dir = root / "distribution" / FAMILY / "request=500m-limit=500m"
    assert (cell_dir / "dist-slices.csv").is_file()
    assert (cell_dir / "dist-runtime.csv").is_file()
    assert (cell_dir / "dist-summary.csv").is_file()
    assert (cell_dir / "dist-percentiles.json").is_file()

    slices = pd.read_csv(cell_dir / "dist-slices.csv")
    assert list(slices.columns) == SLICES_COLUMNS
    assert len(slices) == PLOT_BIG_ROWS
    summary = pd.read_csv(cell_dir / "dist-summary.csv")
    assert list(summary.columns) == SUMMARY_COLUMNS
    # monotonic percentile stats hold on the synthetic data
    row = summary.iloc[0]
    assert row["p50_us"] <= row["p95_us"] <= row["p99_us"]


def test_fixture_sizes_are_pinned() -> None:
    """The fixture sizes are explicit constants."""
    assert ANALYZE_ROWS_PER_REP == 4_000_000
    assert ANALYZE_REPS == 3
    assert PLOT_BIG_ROWS == 2_000_000
    assert STEPS_BIG_ROWS == 150_000
    assert GIF_BIG_ROWS == 10_000_000
    assert GIF_REPS == 3


# ---------------------------------------------------------------------------
# Budget tests (marked ``memory``; RED against the current implementation)
# ---------------------------------------------------------------------------


@pytest.mark.memory
def test_dist_analyze_peak_rss_within_budget(analyze_env_and_data) -> None:
    """dist-analyze peak RSS <= 3 GB.  RED now; the memory-optimized rewrite makes it pass."""
    env, data_dir = analyze_env_and_data
    out_dir = data_dir.parent / "output-analyze"
    rc, peak_kb, wall_s, stderr = run_stage_measured(
        DIST_ANALYZE_SCRIPT,
        [
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(out_dir),
            "--family",
            FAMILY,
            "--workload",
            "stress-ng",
            "--duration",
            "90",
        ],
        env_extra=env,
    )
    assert rc == 0, f"dist-analyze failed: {stderr[-1000:]!r}"
    _assert_within_budget("dist-analyze", peak_kb)


@pytest.mark.memory
def test_dist_plot_peak_rss_within_budget(plot_data_dir, tmp_path: Path) -> None:
    """dist-plot peak RSS <= 2 GB.  RED now; the memory-optimized rewrite makes it pass."""
    rc, peak_kb, wall_s, stderr = run_stage_measured(
        DIST_PLOT_SCRIPT,
        [
            "--data-dir",
            str(plot_data_dir),
            "--output-dir",
            str(tmp_path / "out-plot"),
            "--family",
            FAMILY,
            "--cells",
            ",".join(MEM_CELLS),
        ],
    )
    assert rc == 0, f"dist-plot failed: {stderr[-1000:]!r}"
    _assert_within_budget("dist-plot", peak_kb)


@pytest.mark.memory
def test_dist_steps_peak_rss_within_budget(steps_data_dir, tmp_path: Path) -> None:
    """dist-steps peak RSS <= 1.5 GB.  RED now; the memory-optimized rewrite makes it pass."""
    rc, peak_kb, wall_s, stderr = run_stage_measured(
        DIST_STEPS_SCRIPT,
        [
            "--data-dir",
            str(steps_data_dir),
            "--output-dir",
            str(tmp_path / "out-steps"),
            "--family",
            FAMILY,
            "--cells",
            ",".join(MEM_CELLS),
        ],
    )
    assert rc == 0, f"dist-steps failed: {stderr[-1000:]!r}"
    _assert_within_budget("dist-steps", peak_kb)


@pytest.mark.memory
def test_dist_gif_peak_rss_within_budget(gif_data_dir, tmp_path: Path) -> None:
    """dist-gif peak RSS <= 2 GB.  The memory-optimized
    implementation passes (measured stage peak ~1.9 GiB after the
    grandchild-measurement fix); a memory regression in dist-gif fails it."""
    rc, peak_kb, wall_s, stderr = run_stage_measured(
        DIST_GIF_SCRIPT,
        [
            "--data-dir",
            str(gif_data_dir),
            "--output-dir",
            str(tmp_path / "out-gif"),
            "--family",
            FAMILY,
        ],
    )
    assert rc == 0, f"dist-gif failed: {stderr[-1000:]!r}"
    _assert_within_budget("dist-gif", peak_kb)
