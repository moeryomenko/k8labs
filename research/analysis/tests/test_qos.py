"""Tests for qos-analyze.py — QoS hierarchy competition analysis.

TASK-016 test-first design, red until the script is implemented.
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract the implementation must build:

    research/analysis/qos-analyze.py  (module: qos_analyze)
      load_summary(data_dir: Path) -> pd.DataFrame
      discover_hierarchy_files(data_dir: Path, summary_df: pd.DataFrame)
          -> dict[str, Path]
      load_hierarchy(path: Path) -> dict
      build_qos_table(summary_df: pd.DataFrame, hierarchy: dict) -> pd.DataFrame
      qos_achieved_shares(summary_df: pd.DataFrame) -> pd.DataFrame
      verify_hierarchy_weights(summary_df: pd.DataFrame, hierarchy: dict)
          -> list[str]
      main(argv: list[str] | None = None) -> int

CLI: --data-dir <dir> --output-dir <dir>; writes qos-summary.csv with columns
cell,qos_slice,pod,cpu_weight,achieved_share,throttled_usec and a lazy,
non-fatal qos-share.png.

Covered requirements:
  REQ-1 (VC-QOS-01) exact hierarchy+summary table math
  REQ-2 (VC-QOS-02) missing hierarchy JSON -> skip cell + warn, no crash
  REQ-6 (VC-CLI-01) --data-dir/--output-dir contract and exit codes
  REQ-6 (VC-EMPTY-01) empty input -> header-only output, no crash
  REQ-7 (VC-MPL-01) matplotlib lazy import, headless/non-fatal
  REQ-2 (FIX-3, VC-QOS-G-01) direct kubepods-pod*.slice (TRUE Guaranteed pod,
      systemd driver) attributed to qos class 'guaranteed'; build_qos_table /
      verify_hierarchy_weights use the self-representing entry's own weight
      (TestGuaranteedDirectSlice)

Run from research/analysis:
    python3 -m pytest tests/test_qos.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from tests.conftest import (
    FAMILY_SUMMARY_COLUMNS,
    family_c_hierarchy,
    write_summary_csv,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
QOS_SCRIPT = ANALYSIS_DIR / "qos-analyze.py"
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

# Family C fixture: usage 60000/100000/5000 per replicate, 2 replicates.
TOTAL_USAGE = 2 * (60000 + 100000 + 5000)  # 330000
GUARANTEED_SHARE = 2 * 60000 / TOTAL_USAGE  # 12/33
BURSTABLE_SHARE = 2 * 100000 / TOTAL_USAGE  # 20/33
BESTEFFORT_SHARE = 2 * 5000 / TOTAL_USAGE  # 1/33

AGG_ENV = {**os.environ, "MPLBACKEND": "Agg"}


# =========================================================================
# Helpers
# =========================================================================


def load_qos_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("qos_analyze", QOS_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {QOS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_qos(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run qos-analyze.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(QOS_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
        env=env or AGG_ENV,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
):
    """Run the script against a fixture and return (rc, stderr, output dir)."""
    out_dir = tmp_path / "output"
    rc, _out, err = run_qos(
        ["--data-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or []),
        env=env,
    )
    return rc, err, out_dir


def family_c_summary_df(fixture_dir: pathlib.Path) -> pd.DataFrame:
    """The summary.csv of a Family C fixture as a DataFrame."""
    return pd.read_csv(fixture_dir / "summary.csv")


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_functions(self):
        """qos-analyze.py exposes the pinned public functions."""
        module = load_qos_module()
        for name in (
            "load_summary",
            "discover_hierarchy_files",
            "load_hierarchy",
            "build_qos_table",
            "qos_achieved_shares",
            "verify_hierarchy_weights",
            "main",
        ):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )


# =========================================================================
# load_summary / load_hierarchy
# =========================================================================


class TestLoadSummary:
    """Reading summary.csv from a data dir."""

    def test_reads_summary_rows(self, family_c_data_dir: pathlib.Path):
        """load_summary returns every (cell, qos, replicate) row, 8-column schema."""
        module = load_qos_module()
        df = module.load_summary(family_c_data_dir)
        assert list(df.columns) == FAMILY_SUMMARY_COLUMNS
        assert len(df) == 6  # 3 QoS classes x 2 replicates

    def test_missing_summary_raises(self, tmp_path: pathlib.Path):
        """Missing summary.csv raises FileNotFoundError with a clear message."""
        module = load_qos_module()
        with pytest.raises(FileNotFoundError):
            module.load_summary(tmp_path / "does-not-exist")


class TestLoadHierarchy:
    """Parsing the cgroup-hierarchy-<node>.json snapshot schema."""

    def test_parses_hierarchy(self, family_c_data_dir: pathlib.Path):
        """Exposes node, kubepods_slice_weight, 3 QoS slices with pod entries."""
        module = load_qos_module()
        (hierarchy_path,) = module.discover_hierarchy_files(
            family_c_data_dir, module.load_summary(family_c_data_dir)
        ).values()
        hierarchy = module.load_hierarchy(hierarchy_path)
        assert hierarchy["node"] == "cp1"
        assert str(hierarchy["kubepods_slice_weight"]) == "100"
        names = {slice_["name"] for slice_ in hierarchy["slices"]}
        assert names == {
            "kubepods-guaranteed.slice",
            "kubepods-burstable.slice",
            "kubepods-besteffort.slice",
        }
        burstable = next(
            s for s in hierarchy["slices"] if s["name"] == "kubepods-burstable.slice"
        )
        assert burstable["pods"][0]["cpu_weight"] == "100"

    def test_malformed_json_raises(self, tmp_path: pathlib.Path):
        """Broken JSON raises ValueError, never a silent partial parse."""
        module = load_qos_module()
        bad = tmp_path / "cgroup-hierarchy-bad.json"
        bad.write_text("{ not json")
        with pytest.raises(ValueError):
            module.load_hierarchy(bad)


# =========================================================================
# VC-QOS-02 — hierarchy JSON discovery (missing -> cell absent)
# =========================================================================


class TestDiscoverHierarchyFiles:
    """Locating one cgroup-hierarchy-*.json per cell."""

    def test_finds_per_cell_json(self, family_c_data_dir: pathlib.Path):
        """Complete cell maps to its hierarchy JSON path."""
        module = load_qos_module()
        summary = module.load_summary(family_c_data_dir)
        found = module.discover_hierarchy_files(family_c_data_dir, summary)
        assert list(found) == ["qos-compete"]
        assert found["qos-compete"].name == "cgroup-hierarchy-cp1.json"

    def test_missing_hierarchy_cell_absent(
        self, incomplete_hierarchy_data_dir: pathlib.Path
    ):
        """A cell without any hierarchy JSON is not in the discovery result."""
        module = load_qos_module()
        summary = module.load_summary(incomplete_hierarchy_data_dir)
        found = module.discover_hierarchy_files(incomplete_hierarchy_data_dir, summary)
        assert "qos-compete" in found
        assert "qos-broken" not in found


# =========================================================================
# VC-QOS-01 — exact table math (pure function)
# =========================================================================


class TestBuildQosTable:
    """One row per (cell, QoS class) with exact hierarchy/summary math."""

    @pytest.fixture
    def summary_df(self, family_c_data_dir: pathlib.Path) -> pd.DataFrame:
        return family_c_summary_df(family_c_data_dir)

    def _table(self, summary_df: pd.DataFrame) -> pd.DataFrame:
        module = load_qos_module()
        hierarchy = family_c_hierarchy()
        table = module.build_qos_table(summary_df, hierarchy)
        return table.set_index("qos_slice")

    def test_exact_achieved_shares(self, summary_df: pd.DataFrame):
        """Shares are aggregate-then-divide usage per class: 12/33, 20/33, 1/33."""
        table = self._table(summary_df)
        assert table.loc[
            "kubepods-guaranteed.slice", "achieved_share"
        ] == pytest.approx(GUARANTEED_SHARE, abs=1e-9)
        assert table.loc["kubepods-burstable.slice", "achieved_share"] == pytest.approx(
            BURSTABLE_SHARE, abs=1e-9
        )
        assert table.loc[
            "kubepods-besteffort.slice", "achieved_share"
        ] == pytest.approx(BESTEFFORT_SHARE, abs=1e-9)

    def test_weights_come_from_hierarchy(self, summary_df: pd.DataFrame):
        """cpu_weight column carries the hierarchy JSON pod weights 59/100/1."""
        table = self._table(summary_df)
        assert table.loc["kubepods-guaranteed.slice", "cpu_weight"] == 59
        assert table.loc["kubepods-burstable.slice", "cpu_weight"] == 100
        assert table.loc["kubepods-besteffort.slice", "cpu_weight"] == 1

    def test_pod_names_from_hierarchy(self, summary_df: pd.DataFrame):
        """pod column carries the hierarchy pod slice names."""
        table = self._table(summary_df)
        assert (
            table.loc["kubepods-burstable.slice", "pod"]
            == "kubepods-burstable-podb1.slice"
        )

    def test_throttled_usec_sums_across_replicates(self, summary_df: pd.DataFrame):
        """throttled_usec is the per-class sum: 0 / 50000 / 0."""
        table = self._table(summary_df)
        assert table.loc["kubepods-guaranteed.slice", "throttled_usec"] == 0
        assert table.loc["kubepods-burstable.slice", "throttled_usec"] == 50000
        assert table.loc["kubepods-besteffort.slice", "throttled_usec"] == 0

    def test_output_columns_match_contract(self, summary_df: pd.DataFrame):
        """Result DataFrame has exactly the pinned output columns."""
        module = load_qos_module()
        table = module.build_qos_table(summary_df, family_c_hierarchy())
        assert list(table.columns) == OUTPUT_COLUMNS

    def test_aggregation_sums_replicates_then_divides(self):
        """Aggregate-then-divide across replicates, not mean of per-replicate shares.

        Replicate 1 usage 60000/100000/5000 and replicate 2 usage
        90000/110000/5000 give guaranteed 150000/370000 = 0.405405..., NOT the
        mean of per-replicate shares (~0.4013). Pins VC-QOS-01 math.
        """
        module = load_qos_module()
        df = pd.DataFrame(
            [
                ("guaranteed-qc", 1, 0, 0, 0, 60000, 59, 50000),
                ("burstable-qc", 1, 0, 0, 0, 100000, 100, 50000),
                ("besteffort-qc", 1, 0, 0, 0, 5000, 1, 100000),
                ("guaranteed-qc", 2, 0, 0, 0, 90000, 59, 50000),
                ("burstable-qc", 2, 0, 0, 0, 110000, 100, 50000),
                ("besteffort-qc", 2, 0, 0, 0, 5000, 1, 100000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        table = module.build_qos_table(df, family_c_hierarchy()).set_index("qos_slice")
        assert table.loc[
            "kubepods-guaranteed.slice", "achieved_share"
        ] == pytest.approx(150000 / 370000, abs=1e-9)
        assert table.loc[
            "kubepods-guaranteed.slice", "achieved_share"
        ] != pytest.approx(0.4013, abs=1e-3)

    def test_missing_class_slice_omits_row(self):
        """A hierarchy without a besteffort slice emits no besteffort row."""
        module = load_qos_module()
        df = pd.DataFrame(
            [
                ("guaranteed-qc", 1, 0, 0, 0, 60000, 59, 50000),
                ("burstable-qc", 1, 0, 0, 0, 100000, 100, 50000),
                ("besteffort-qc", 1, 0, 0, 0, 5000, 1, 100000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        hierarchy = family_c_hierarchy()
        hierarchy["slices"] = [
            s for s in hierarchy["slices"] if s["name"] != "kubepods-besteffort.slice"
        ]
        table = module.build_qos_table(df, hierarchy)
        assert "kubepods-besteffort.slice" not in set(table["qos_slice"])
        assert len(table) == 2

    def test_rows_in_qos_priority_order(self, summary_df: pd.DataFrame):
        """Output is deterministic: guaranteed, burstable, then besteffort."""
        module = load_qos_module()
        table = module.build_qos_table(summary_df, family_c_hierarchy())
        assert list(table["qos_slice"]) == [
            "kubepods-guaranteed.slice",
            "kubepods-burstable.slice",
            "kubepods-besteffort.slice",
        ]


class TestQosAchievedShares:
    """Per-QoS-class achieved share, one row per (cell, class)."""

    def test_exact_class_shares(self, family_c_data_dir: pathlib.Path):
        """guaranteed/burstable/besteffort shares 12/33, 20/33, 1/33."""
        module = load_qos_module()
        shares = module.qos_achieved_shares(
            module.load_summary(family_c_data_dir)
        ).set_index("qos")
        assert shares.loc["guaranteed", "achieved_share"] == pytest.approx(
            GUARANTEED_SHARE, abs=1e-9
        )
        assert shares.loc["burstable", "achieved_share"] == pytest.approx(
            BURSTABLE_SHARE, abs=1e-9
        )
        assert shares.loc["besteffort", "achieved_share"] == pytest.approx(
            BESTEFFORT_SHARE, abs=1e-9
        )

    def test_output_columns(self, family_c_data_dir: pathlib.Path):
        """Columns are cell,qos,achieved_share."""
        module = load_qos_module()
        shares = module.qos_achieved_shares(module.load_summary(family_c_data_dir))
        assert list(shares.columns) == ["cell", "qos", "achieved_share"]


# =========================================================================
# Hierarchy weight model verification
# =========================================================================


class TestVerifyHierarchyWeights:
    """Per-pod weight from hierarchy JSON matches summary cpu_weight."""

    def test_matching_weights_no_warnings(self, family_c_data_dir: pathlib.Path):
        """The complete fixture has zero weight mismatches."""
        module = load_qos_module()
        summary = module.load_summary(family_c_data_dir)
        warnings = module.verify_hierarchy_weights(summary, family_c_hierarchy())
        assert warnings == []

    def test_weight_mismatch_reported(self):
        """A summary cpu_weight that disagrees with the JSON weight warns."""
        module = load_qos_module()
        df = pd.DataFrame(
            [
                ("guaranteed-qc", 1, 0, 0, 0, 60000, 99, 50000),  # JSON says 59
                ("burstable-qc", 1, 0, 0, 0, 100000, 100, 50000),
                ("besteffort-qc", 1, 0, 0, 0, 5000, 1, 100000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        warnings = module.verify_hierarchy_weights(df, family_c_hierarchy())
        assert len(warnings) == 1
        assert "guaranteed" in warnings[0]


# =========================================================================
# VC-CLI-01 — CLI contract
# =========================================================================


class TestCli:
    """--data-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_qos(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--data-dir" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_qos([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_data_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --data-dir -> non-zero exit and a clear message."""
        out_dir = tmp_path / "output"
        rc, _out, err = run_qos(
            ["--data-dir", str(tmp_path / "missing"), "--output-dir", str(out_dir)]
        )
        assert rc != 0
        # "missing" is the nonexistent dir name: the real script must name the
        # path it could not find. The Python "can't open file" fallback for the
        # missing script does not contain it, so this stays red in the red phase.
        assert "missing" in err


# =========================================================================
# End-to-end: fixture data -> CSV output
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract CSV."""

    def test_happy_path_outputs_exact_csv(
        self, family_c_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-QOS-01 + VC-CLI-01: exit 0, CSV with exact shares/weights/throttling."""
        rc, err, out_dir = run_ok(family_c_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / OUTPUT_CSV
        assert csv_path.exists(), f"missing output: {csv_path}"
        result = pd.read_csv(csv_path)
        assert list(result.columns) == OUTPUT_COLUMNS
        assert len(result) == 3
        rows = result.set_index("qos_slice")
        assert rows.loc["kubepods-guaranteed.slice", "achieved_share"] == pytest.approx(
            GUARANTEED_SHARE, abs=1e-9
        )
        assert rows.loc["kubepods-burstable.slice", "cpu_weight"] == 100
        assert rows.loc["kubepods-burstable.slice", "throttled_usec"] == 50000

    def test_missing_hierarchy_skips_cell_with_warning(
        self, incomplete_hierarchy_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-QOS-02: exit 0, broken cell skipped, warning names the cell."""
        rc, err, out_dir = run_ok(incomplete_hierarchy_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / OUTPUT_CSV
        assert csv_path.exists()
        result = pd.read_csv(csv_path)
        assert set(result["cell"]) == {"qos-compete"}
        assert "qos-broken" in err
        assert "warn" in err.lower() or "skip" in err.lower()

    def test_empty_summary_outputs_header_only(
        self, empty_summary_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-EMPTY-01: exit 0, header-only output, no crash."""
        rc, err, out_dir = run_ok(empty_summary_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        csv_path = out_dir / OUTPUT_CSV
        assert csv_path.exists()
        lines = csv_path.read_text().splitlines()
        assert lines == [",".join(OUTPUT_COLUMNS)]

    @pytest.mark.skipif(
        not importlib.util.find_spec("matplotlib"), reason="matplotlib not installed"
    )
    def test_png_rendered(
        self, family_c_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: real matplotlib (Agg) emits a valid PNG."""
        rc, err, out_dir = run_ok(family_c_data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        png_path = out_dir / OUTPUT_PNG
        assert png_path.exists(), f"missing plot: {png_path}"
        data = png_path.read_bytes()
        assert data[:4] == b"\x89PNG"

    def test_matplotlib_import_failure_is_nonfatal(
        self, family_c_data_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-MPL-01: broken matplotlib must not block the CSV output.

        A stub matplotlib that raises on import is injected via PYTHONPATH.
        The script must write qos-summary.csv and exit 0 with a warning,
        proving matplotlib is imported lazily and rendering is non-fatal.
        """
        stub_dir = tmp_path / "stub-matplotlib"
        stub_dir.mkdir()
        (stub_dir / "matplotlib.py").write_text(
            'raise ImportError("stubbed out for tests")\n'
        )
        env = {**os.environ, "PYTHONPATH": str(stub_dir), "MPLBACKEND": "Agg"}
        rc, err, out_dir = run_ok(family_c_data_dir, tmp_path, env=env)
        assert rc == 0, f"stderr: {err}"
        assert (out_dir / OUTPUT_CSV).exists(), (
            "CSV must be written even without matplotlib"
        )
        assert "matplotlib" in err.lower() or "warn" in err.lower()


# =========================================================================
# Sanity check of the fixture itself (expected values hand-verifiable)
# =========================================================================


class TestFixtureMath:
    """The conftest fixture encodes the numbers the tests assert."""

    def test_fixture_encodes_exact_values(self, family_c_data_dir: pathlib.Path):
        summary = family_c_summary_df(family_c_data_dir)
        assert summary["cpu_weight"].tolist() == [59, 100, 1, 59, 100, 1]
        assert summary["usage_usec"].tolist() == [60000, 100000, 5000] * 2
        assert summary["throttled_usec"].tolist() == [0, 25000, 0, 0, 25000, 0]
        assert (
            summary["cell_label"].tolist()
            == [
                "guaranteed-qos-compete",
                "burstable-qos-compete",
                "besteffort-qos-compete",
            ]
            * 2
        )
        hierarchy = json.loads(
            (
                family_c_data_dir / "qos-compete" / "cgroup-hierarchy-cp1.json"
            ).read_text()
        )
        assert hierarchy["node"] == "cp1"


# =========================================================================
# FIX-3 (REQ-2): TRUE Guaranteed pod — direct kubepods-pod*.slice
# =========================================================================


def direct_guaranteed_hierarchy() -> dict:
    """Family C hierarchy whose guaranteed pod is a DIRECT kubepods-pod*.slice.

    With the systemd cgroup driver a TRUE Guaranteed pod (memory
    request==limit) has NO kubepods-guaranteed.slice wrapper: the snapshot
    emits kubepods-pod<uid>.slice directly under kubepods.slice as a slice
    entry whose pods[] holds ONE self-representing entry mirroring the slice
    itself. Slice weights stay 59/100/1 (matching the summary cpu_weight).
    """
    h = family_c_hierarchy()
    h["slices"] = [
        {
            "name": "kubepods-podg1.slice",
            "cpu_weight": "59",
            "pods": [
                {
                    "name": "kubepods-podg1.slice",
                    "cpu_weight": "59",
                    "cpu_max": "50000 100000",
                }
            ],
        },
        next(s for s in h["slices"] if s["name"] == "kubepods-burstable.slice"),
        next(s for s in h["slices"] if s["name"] == "kubepods-besteffort.slice"),
    ]
    return h


def build_direct_guaranteed_data_dir(root: pathlib.Path) -> pathlib.Path:
    """Family C fixture whose hierarchy JSON has the direct guaranteed slice.

    Same summary numbers as ``family_c_data_dir`` (guaranteed 60000 weight 59,
    burstable 100000 weight 100, besteffort 5000 weight 1, 2 replicates) but
    the hierarchy JSON places the guaranteed pod at kubepods-podg1.slice
    (self-representing) instead of kubepods-guaranteed.slice.
    """
    cell = "qos-compete"
    rows: list[tuple] = []
    for rep in (1, 2):
        rows.append((f"guaranteed-{cell}", rep, 1000, 0, 0, 60000, 59, 50000))
        rows.append((f"burstable-{cell}", rep, 1000, 500, 25000, 100000, 100, 50000))
        rows.append((f"besteffort-{cell}", rep, 1000, 0, 0, 5000, 1, 100000))
    write_summary_csv(root / "summary.csv", rows)
    (root / cell).mkdir(parents=True, exist_ok=True)
    (root / cell / "cgroup-hierarchy-cp1.json").write_text(
        json.dumps(direct_guaranteed_hierarchy(), indent=2) + "\n"
    )
    return root


class TestGuaranteedDirectSlice:
    """FIX-3 (REQ-2): a direct kubepods-pod*.slice maps to QoS 'guaranteed'.

    A TRUE Guaranteed pod has no kubepods-guaranteed.slice wrapper. The
    snapshot emits kubepods-pod<uid>.slice as a slice entry with one
    self-representing pod entry; _slice_by_qos must attribute it to the
    'guaranteed' class and build_qos_table / verify_hierarchy_weights must
    use the self-representing entry's own weight.
    """

    @pytest.fixture
    def summary_df(self, family_c_data_dir: pathlib.Path) -> pd.DataFrame:
        return family_c_summary_df(family_c_data_dir)

    def test_slice_by_qos_attributes_direct_pod_slice_to_guaranteed(self):
        """_slice_by_qos maps kubepods-podXYZ.slice -> 'guaranteed'."""
        module = load_qos_module()
        mapped = module._slice_by_qos(direct_guaranteed_hierarchy())
        assert mapped["guaranteed"]["name"] == "kubepods-podg1.slice"

    def test_build_qos_table_guaranteed_row_uses_direct_slice(
        self, summary_df: pd.DataFrame
    ):
        """build_qos_table emits the direct slice row with its own weight."""
        module = load_qos_module()
        table = module.build_qos_table(summary_df, direct_guaranteed_hierarchy())
        row = table[table["qos_slice"] == "kubepods-podg1.slice"]
        assert len(row) == 1
        assert row.iloc[0]["pod"] == "kubepods-podg1.slice"
        assert row.iloc[0]["cpu_weight"] == 59
        assert row.iloc[0]["achieved_share"] == pytest.approx(
            GUARANTEED_SHARE, abs=1e-9
        )

    def test_build_qos_table_keeps_other_classes(self, summary_df: pd.DataFrame):
        """Burstable/besteffort rows are unaffected by the direct slice."""
        module = load_qos_module()
        table = module.build_qos_table(summary_df, direct_guaranteed_hierarchy())
        assert set(table["qos_slice"]) == {
            "kubepods-podg1.slice",
            "kubepods-burstable.slice",
            "kubepods-besteffort.slice",
        }
        burstable = table[table["qos_slice"] == "kubepods-burstable.slice"]
        assert burstable.iloc[0]["cpu_weight"] == 100

    def test_verify_hierarchy_weights_no_mismatch_for_direct_slice(
        self, summary_df: pd.DataFrame
    ):
        """Matching self-representing weight emits no mismatch warning."""
        module = load_qos_module()
        warnings = module.verify_hierarchy_weights(
            summary_df, direct_guaranteed_hierarchy()
        )
        assert warnings == []

    def test_verify_hierarchy_weights_mismatch_warns_for_direct_slice(self):
        """A summary weight disagreeing with the direct slice weight warns."""
        module = load_qos_module()
        df = pd.DataFrame(
            [
                ("guaranteed-qc", 1, 0, 0, 0, 60000, 99, 50000),  # JSON says 59
                ("burstable-qc", 1, 0, 0, 0, 100000, 100, 50000),
                ("besteffort-qc", 1, 0, 0, 0, 5000, 1, 100000),
            ],
            columns=FAMILY_SUMMARY_COLUMNS,  # type: ignore
        )
        warnings = module.verify_hierarchy_weights(df, direct_guaranteed_hierarchy())
        assert len(warnings) == 1
        assert "guaranteed" in warnings[0]

    def test_end_to_end_direct_slice_writes_csv(self, tmp_path: pathlib.Path):
        """CLI run over a direct-slice fixture emits the guaranteed row."""
        data_dir = build_direct_guaranteed_data_dir(tmp_path / "family-c-direct")
        rc, err, out_dir = run_ok(data_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        result = pd.read_csv(out_dir / OUTPUT_CSV)
        row = result[result["qos_slice"] == "kubepods-podg1.slice"]
        assert len(row) == 1
        assert row.iloc[0]["pod"] == "kubepods-podg1.slice"
        assert row.iloc[0]["cpu_weight"] == 59
        assert row.iloc[0]["achieved_share"] == pytest.approx(
            GUARANTEED_SHARE, abs=1e-9
        )
