"""Tests for generate-report.py — markdown practical guide from analysis outputs.

TASK-018 test-first design, extended by debt tasks TASK-D01 (data-driven
burst verdict) and TASK-D08 (QoS ordering for direct guaranteed pod slices).
The pinned contract lives in TEST-DESIGN.md; the module/function/CLI names
used here are the contract the engineer must build:

    research/analysis/generate-report.py  (module: generate_report)
      REPORT_FILENAME = "interaction-report.md"
      BURST_CSV = "burst-summary.csv"      (TASK-D01 pinned input)
      load_table(input_dir: Path, filename: str) -> pd.DataFrame | None
      build_report(input_dir: Path) -> str
      main(argv: list[str] | None = None) -> int

CLI: --input-dir <dir> --output-dir <dir>; writes
<output-dir>/interaction-report.md.

Input contract (all files live in the --input-dir, column schemas verbatim
from the analyzer outputs):
    weight-share-summary.csv  cell,pod,achieved_share,weight_share,ratio_error
    heatmap-throttling_ratio.csv  request + one column per limit
    qos-summary.csv  cell,qos_slice,pod,cpu_weight,achieved_share,throttled_usec
    latency-summary.csv  cell,p50,p95,p99,throttled_usec,usage_usec,throttling_ratio
    latency-correlation.csv  metric,correlation
    tunables-comparison.csv  tunable,mean_p99,std_p99,mean_slice_us,std_slice_us,n
    tunables-significance.csv  tunable,mean_p99,default_mean_p99,diff_p99,noise_threshold,significant
    burst-summary.csv (TASK-D01 pinned)  cell,replicate,nr_periods,nr_throttled,
      throttled_usec,usage_usec,cpu_max_burst,cpu_max_quota

Report structure (pinned section headers, in order):
    # Request/limit scheduler interaction
    ## Weight-share validation
    ## Request x limit interaction heatmap
    ## Throttling region thresholds
    ## QoS guidance
    ## Latency under throttling
    ## Tunables verdict
    ## Burst verdict

Section-presence rule (REQ-2, pinned): the six data-driven sections are NEVER
omitted; when their CSV is missing or has zero data rows they render the exact
marker line ``_no data_`` under the header. The burst verdict section is
two-branch (TASK-D01): when burst-summary.csv exists with data and at least
one row has cpu_max_burst > 0 it renders the measured verdict (the applied
burst value, per-cell mean nr_throttled / throttled_usec, and the kernel
constraint note); otherwise it renders the fallback static note ("No burst
experiment data. Burst is disabled: cpu.max.burst defaults to 0..."). Empty
input still produces a valid markdown file.

Number formatting (pinned, REQ-4): integers render as ints; floats render via
``format(v, "g")`` (deterministic, round-trippable); NaN renders as ``n/a``.
Tables are standard pipe tables with a ``---`` separator row.

TASK-018 covered requirements:
  REQ-1 (VC-REP-01) all sections present, in pinned order
  REQ-2 (VC-REP-02) missing/empty input -> "_no data_" marker, valid markdown
  REQ-3 (VC-REP-03) exact fixture values in the right sections
  REQ-4 (VC-REP-04) deterministic output (byte-identical, sorted rows)
  REQ-5 (VC-CLI-01) --input-dir/--output-dir contract and exit codes
  REQ-6 (VC-REP-05) string-contains / table parsing assertions, no network

TASK-D01/D08 debt requirements (the reason this file changes):
  REQ-1 (D01) burst data-present case renders measured numbers (nr_throttled
        105 -> 0, burst=25000, kernel constraint note)
  REQ-2 (D01) burst no-data fallback still renders the static note
  REQ-3 (D08) QoS ordering: kubepods-pod*.slice sorts as guaranteed (before
        burstable/besteffort); kubepods-guaranteed.slice still works
  REQ-4 (D01/D08) existing determinism/CLI/no-data tests unaffected
  REQ-5 (D01) burst input filename + schema pinned in TEST-DESIGN.md

Run from research/analysis:
    python3 -m pytest tests/test_report.py -q
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest

from tests.conftest import (
    BURST_COLUMNS,
    HEATMAP_COLUMNS,
    LATENCY_COLUMNS,
    QOS_COLUMNS,
    REPORT_INPUT_FILES,
    WEIGHT_SHARE_COLUMNS,
    write_analysis_csv,
)

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent
REPORT_SCRIPT = ANALYSIS_DIR / "generate-report.py"
REPORT_FILENAME = "interaction-report.md"

TITLE = "# Request/limit scheduler interaction"
SECTION_HEADERS = [
    "## Weight-share validation",
    "## Request x limit interaction heatmap",
    "## Throttling region thresholds",
    "## QoS guidance",
    "## Latency under throttling",
    "## Tunables verdict",
    "## Burst verdict",
]
DATA_SECTIONS = SECTION_HEADERS[:-1]  # burst is two-branch, not `_no data_`-driven
NO_DATA_MARKER = "_no data_"

# Expected rendered values (fixture numbers from conftest, formatted via "g").
WS_ROW_A_2POD = ["a=500m;b=500m", "a", "0.375", "0.371069", "0.00393082"]
WS_ROW_B_2POD = ["a=500m;b=500m", "b", "0.625", "0.628931", "-0.00393082"]
LAT_ROW_CELL1 = [
    "req=100m-lim=200m",
    "10.5",
    "19.05",
    "19.81",
    "18000000",
    "24000000",
    "0.9",
]
LAT_ROW_CELL2 = [
    "req=500m-lim=1000m",
    "5.5",
    "9.55",
    "9.91",
    "2000000",
    "16000000",
    "0.1",
]

# TASK-D01 burst contract — fixture values from conftest BURST_ROWS.
# The applied burst value (25000) comes from cpu_max_burst, NEVER from the
# cell label (the burst cell label is burst=100000, the matrix value the
# kernel rejected EINVAL).
BURST_CELL_NO_BURST = "request=-limit=250m-burst="
BURST_CELL_APPLIED = "request=-limit=250m-burst=100000"
BURST_ROW_NO_BURST = [BURST_CELL_NO_BURST, "105", "5280000"]
BURST_ROW_APPLIED = [BURST_CELL_APPLIED, "0", "0"]
BURST_VERDICT_MARKER = "Measured verdict"
BURST_FALLBACK_MARKER = "No burst experiment data"
BURST_KERNEL_CONSTRAINT = "burst <= quota"

# TASK-D08: the direct TRUE-Guaranteed pod slice qos-analyze.py emits.
DIRECT_GUARANTEED_SLICE = "kubepods-podg1.slice"


# =========================================================================
# Helpers
# =========================================================================


def load_report_module():
    """Import the not-yet-existing script so pinned function names are callable."""
    spec = importlib.util.spec_from_file_location("generate_report", REPORT_SCRIPT)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"script not found: {REPORT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_report(argv: list[str]) -> tuple[int, str, str]:
    """Run generate-report.py with the given argv via subprocess."""
    proc = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_ok(
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    extra: list[str] | None = None,
):
    """Run the script against a fixture and return (rc, stderr, report path)."""
    out_dir = tmp_path / "output"
    rc, _out, err = run_report(
        ["--input-dir", str(fixture_dir), "--output-dir", str(out_dir)] + (extra or [])
    )
    return rc, err, out_dir / REPORT_FILENAME


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


def parse_markdown_table(text: str) -> list[list[str]]:
    """Parse pipe-table rows from markdown, skipping the separator row."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue  # separator row
        rows.append(cells)
    return rows


# =========================================================================
# Module contract (pinned names)
# =========================================================================


class TestModuleContract:
    """The script file exists, is importable, and exposes the pinned API."""

    def test_module_loads_and_exposes_pinned_api(self):
        """generate-report.py exposes REPORT_FILENAME/load_table/build_report/main."""
        module = load_report_module()
        assert module.REPORT_FILENAME == REPORT_FILENAME
        for name in ("load_table", "build_report", "main"):
            assert callable(getattr(module, name, None)), (
                f"missing pinned function: {name}"
            )

    def test_module_exposes_burst_csv_name(self):
        """TASK-D01: the script pins the burst input filename."""
        module = load_report_module()
        assert module.BURST_CSV == "burst-summary.csv"


# =========================================================================
# load_table
# =========================================================================


class TestLoadTable:
    """Reading an analysis-output CSV from the input dir."""

    def test_reads_weight_share_csv(self, analysis_output_dir: pathlib.Path):
        """load_table returns rows with the pinned weight-share schema."""
        module = load_report_module()
        df = module.load_table(analysis_output_dir, "weight-share-summary.csv")
        assert list(df.columns) == WEIGHT_SHARE_COLUMNS
        assert len(df) == 5  # 2-pod cell x 2 + 3-pod cell x 3

    def test_reads_heatmap_csv(self, analysis_output_dir: pathlib.Path):
        """The heatmap CSV keeps its pivot shape (request + limit columns)."""
        module = load_report_module()
        df = module.load_table(analysis_output_dir, "heatmap-throttling_ratio.csv")
        assert list(df.columns) == HEATMAP_COLUMNS
        assert len(df) == 2

    def test_missing_file_returns_none(self, analysis_output_dir: pathlib.Path):
        """A CSV that does not exist yields None (section -> '_no data_')."""
        module = load_report_module()
        assert module.load_table(analysis_output_dir, "does-not-exist.csv") is None

    def test_reads_burst_csv(self, analysis_output_dir: pathlib.Path):
        """TASK-D01: load_table reads burst-summary.csv with the pinned schema."""
        module = load_report_module()
        df = module.load_table(analysis_output_dir, "burst-summary.csv")
        assert list(df.columns) == BURST_COLUMNS
        assert len(df) == 6  # 2 cells x 3 replicates


# =========================================================================
# VC-REP-01 — all sections present, pinned order (REQ-1)
# =========================================================================


class TestSectionPresence:
    """REQ-1: every required section appears in the pinned order."""

    def test_all_sections_present_in_order(self, analysis_output_dir: pathlib.Path):
        """All seven headers appear; their order matches the pinned list."""
        module = load_report_module()
        report = module.build_report(analysis_output_dir)
        positions = [report.index(h) for h in SECTION_HEADERS]
        assert positions == sorted(positions)
        assert report.index(TITLE) < positions[0]

    def test_happy_path_cli_report_has_all_sections(
        self, analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """End-to-end: the written file contains all sections (REQ-1 + REQ-5)."""
        rc, err, report_path = run_ok(analysis_output_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        report = report_path.read_text()
        for header in SECTION_HEADERS:
            assert header in report


# =========================================================================
# VC-REP-03 — exact fixture values (REQ-3)
# =========================================================================


class TestWeightShareSection:
    """Weight-share validation table carries exact ratio_error values."""

    def test_exact_ratio_errors_present(self, analysis_output_dir: pathlib.Path):
        """The pinned ratio_error values appear verbatim in the section."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[0]
        )
        assert "0.00393082" in body
        assert "-0.00393082" in body

    def test_table_rows_are_sorted(self, analysis_output_dir: pathlib.Path):
        """Rows sorted by (cell, pod): 300m cell before 500m cell, a before b."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[0]
        )
        rows = parse_markdown_table(body)
        assert rows[0] == [
            "cell",
            "pod",
            "achieved_share",
            "weight_share",
            "ratio_error",
        ]
        assert rows[1] == ["a=300m;b=600m;c=600m", "a", "0.3", "0.296482", "0.00351759"]
        assert rows[-2] == WS_ROW_A_2POD
        assert rows[-1] == WS_ROW_B_2POD
        cells = [(r[0], r[1]) for r in rows[1:]]
        assert cells == sorted(cells)


class TestHeatmapSection:
    """Interaction heatmap summary reports max throttling ratio and pivot."""

    def test_max_throttling_ratio_line(self, analysis_output_dir: pathlib.Path):
        """Max ratio 0.9 is reported with its (request, limit) location."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[1]
        )
        assert "Max throttling ratio: 0.9 (request=100m, limit=200m)" in body

    def test_pivot_table_values(self, analysis_output_dir: pathlib.Path):
        """Pivot values and NaN cells render deterministically (n/a)."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[1]
        )
        rows = parse_markdown_table(body)
        assert rows[0] == ["request", "200", "500", "1000", "2000"]
        assert rows[1] == ["100", "0.9", "0.5", "0.2", "n/a"]
        assert rows[2] == ["500", "n/a", "n/a", "0.8", "0.1"]


class TestRegionSection:
    """Throttling region thresholds classify cells by their ratio."""

    def test_thresholds_are_stated(self, analysis_output_dir: pathlib.Path):
        """The section names the safe/caution/throttled boundaries."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[2]
        )
        assert "safe" in body.lower()
        assert "caution" in body.lower()
        assert "throttled" in body.lower()
        assert "0.25" in body and "0.75" in body

    def test_cells_classified_exactly(self, analysis_output_dir: pathlib.Path):
        """Every heatmap cell maps to the pinned region."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[2]
        )
        rows = parse_markdown_table(body)
        classified = {(r[0], r[1]): r[2] for r in rows[1:] if len(r) >= 3}
        assert classified == {
            ("100", "200"): "throttled",
            ("100", "500"): "caution",
            ("100", "1000"): "safe",
            ("500", "1000"): "throttled",
            ("500", "2000"): "safe",
        }


class TestQosSection:
    """QoS guidance carries hierarchy weights and achieved shares."""

    def test_exact_shares_present(self, analysis_output_dir: pathlib.Path):
        """12/33, 20/33, 1/33 render as 0.363636 / 0.606061 / 0.030303."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[3]
        )
        assert "0.363636" in body
        assert "0.606061" in body
        assert "0.030303" in body
        assert "50000" in body  # burstable throttled_usec sum

    def test_qos_rows_ordered_by_priority(self, analysis_output_dir: pathlib.Path):
        """Rows ordered guaranteed, burstable, besteffort (pinned priority)."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[3]
        )
        rows = parse_markdown_table(body)
        assert rows[0] == QOS_COLUMNS
        assert rows[1][1] == "kubepods-guaranteed.slice"
        assert rows[1][3] == "59"
        assert rows[2][1] == "kubepods-burstable.slice"
        assert rows[2][3] == "100"
        assert rows[3][1] == "kubepods-besteffort.slice"
        assert rows[3][3] == "1"

    def test_direct_pod_slice_sorts_as_guaranteed(
        self, qos_direct_guaranteed_output_dir: pathlib.Path
    ):
        """TASK-D08: kubepods-podg1.slice sorts before burstable/besteffort.

        The direct TRUE-Guaranteed pod slice (qos-analyze.py output for a
        systemd layout without the kubepods-guaranteed.slice wrapper) must
        rank with guaranteed priority, not fall to the end of the table.
        """
        module = load_report_module()
        body = section_text(
            module.build_report(qos_direct_guaranteed_output_dir), SECTION_HEADERS[3]
        )
        rows = parse_markdown_table(body)
        assert rows[0] == QOS_COLUMNS
        assert [r[1] for r in rows[1:]] == [
            DIRECT_GUARANTEED_SLICE,
            "kubepods-burstable.slice",
            "kubepods-besteffort.slice",
        ]


class TestQosPriority:
    """_qos_priority mapping (TASK-D08): direct pod slice ranks as guaranteed."""

    def test_direct_pod_slice_is_guaranteed(self):
        """kubepods-pod*.slice (TRUE Guaranteed) ranks with guaranteed (0)."""
        module = load_report_module()
        assert module._qos_priority(DIRECT_GUARANTEED_SLICE) == 0

    @pytest.mark.parametrize(
        ("slice_name", "expected"),
        [
            ("kubepods-guaranteed.slice", 0),
            ("kubepods-burstable.slice", 1),
            ("kubepods-besteffort.slice", 2),
            ("kubepods-other.slice", 3),
        ],
        ids=["guaranteed", "burstable", "besteffort", "unknown"],
    )
    def test_class_slices_keep_priorities(self, slice_name: str, expected: int):
        """The existing QoS-class mapping is unchanged (REQ-4/REQ-3)."""
        module = load_report_module()
        assert module._qos_priority(slice_name) == expected


class TestLatencySection:
    """Latency table carries exact percentiles and throttling ratios."""

    def test_exact_p99_present(self, analysis_output_dir: pathlib.Path):
        """p99 19.81 (heavily throttled) and 9.91 (lightly throttled)."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[4]
        )
        assert "19.81" in body
        assert "9.91" in body

    def test_latency_rows_sorted_by_cell(self, analysis_output_dir: pathlib.Path):
        """Rows sorted by cell; exact per-cell values."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[4]
        )
        rows = parse_markdown_table(body)
        assert rows[0] == LATENCY_COLUMNS
        assert rows[1] == LAT_ROW_CELL1
        assert rows[2] == LAT_ROW_CELL2
        cells = [r[0] for r in rows[1:]]
        assert cells == sorted(cells)

    def test_correlation_metrics_present(self, analysis_output_dir: pathlib.Path):
        """Correlation rows (metric,correlation) appear with value 1."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[4]
        )
        assert "p50_vs_throttled_usec" in body
        assert "p99_vs_throttled_usec" in body
        assert "1" in body


class TestTunablesSection:
    """Tunables verdict reflects the significance table."""

    def test_significance_verdicts(self, analysis_output_dir: pathlib.Path):
        """base-slice-low/high significant; base-slice-mid NOT significant."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[5]
        )
        rows = parse_markdown_table(body)
        verdicts = {r[0]: r for r in rows[1:]}
        assert "significant" in verdicts["base-slice-low"][-1]
        assert "significant" in verdicts["base-slice-high"][-1]
        assert "not significant" in verdicts["base-slice-mid"][-1]

    def test_diff_and_noise_values(self, analysis_output_dir: pathlib.Path):
        """diff_p99 -6 / 0.5 / 6 and thresholds 0 / 0.5 render exactly."""
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[5]
        )
        assert "-6" in body
        assert "0.5" in body
        assert "6" in body


class TestBurstSection:
    """Burst verdict (TASK-D01): data-driven measured verdict vs fallback note."""

    def test_data_present_renders_measured_verdict(
        self, analysis_output_dir: pathlib.Path
    ):
        """burst-summary.csv present -> measured numbers, burst value, kernel note.

        REQ-1: the applied burst value (cpu.max.burst=25000), the per-cell
        mean nr_throttled / throttled_usec (105 -> 0, 5280000 -> 0) and the
        kernel constraint note (burst <= quota; 100000 EINVAL) all appear.
        """
        module = load_report_module()
        body = section_text(
            module.build_report(analysis_output_dir), SECTION_HEADERS[-1]
        )
        # Measured verdict line names the applied burst value and the means.
        assert BURST_VERDICT_MARKER in body
        assert "`cpu.max.burst=25000`" in body
        assert "mean nr_throttled 105 -> 0" in body
        assert "mean throttled_usec 5280000 -> 0" in body
        # The per-cell table carries the measured numbers, sorted by cell.
        rows = parse_markdown_table(body)
        assert rows[0] == ["cell", "mean_nr_throttled", "mean_throttled_usec"]
        assert rows[1] == BURST_ROW_NO_BURST
        assert rows[2] == BURST_ROW_APPLIED
        # Kernel constraint note.
        assert BURST_KERNEL_CONSTRAINT in body
        assert "100000 was rejected EINVAL" in body
        # The fallback note must NOT appear when measured data is present.
        assert "Burst is disabled" not in body
        assert BURST_FALLBACK_MARKER not in body

    def test_no_data_renders_static_fallback(
        self, empty_analysis_output_dir: pathlib.Path
    ):
        """No burst-summary.csv -> the static fallback note (REQ-2)."""
        module = load_report_module()
        body = section_text(
            module.build_report(empty_analysis_output_dir), SECTION_HEADERS[-1]
        )
        assert BURST_FALLBACK_MARKER in body
        assert "Burst is disabled" in body
        assert "cpu.max.burst" in body
        assert BURST_VERDICT_MARKER not in body

    def test_no_burst_cell_renders_fallback(self, tmp_path: pathlib.Path):
        """burst data with cpu_max_burst == 0 everywhere -> fallback note.

        A burst-summary.csv whose rows never enabled burst (all
        cpu_max_burst == 0) carries no measured burst verdict; the section
        falls back to the static note instead of inventing one.
        """
        module = load_report_module()
        d = tmp_path / "burst-zero"
        write_analysis_csv(
            d / "burst-summary.csv",
            BURST_COLUMNS,
            [("request=-limit=250m-burst=", 1, 124, 105, 5200000, 2750000, 0, 25000)],
        )
        body = section_text(module.build_report(d), SECTION_HEADERS[-1])
        assert "Burst is disabled" in body
        assert BURST_VERDICT_MARKER not in body


# =========================================================================
# VC-REP-02 — missing/empty input (REQ-2)
# =========================================================================


class TestNoData:
    """Data-driven sections never vanish: missing data -> '_no data_'."""

    def test_empty_input_is_valid_markdown(
        self, empty_analysis_output_dir: pathlib.Path
    ):
        """REQ-2: empty input still yields a valid markdown file with all headers."""
        module = load_report_module()
        report = module.build_report(empty_analysis_output_dir)
        assert report.strip()
        assert report.lstrip().startswith("#")
        for header in SECTION_HEADERS:
            assert header in report
        for header in DATA_SECTIONS:
            assert NO_DATA_MARKER in section_text(report, header)
        # Burst verdict (TASK-D01): no burst data -> the static fallback note,
        # never the no-data marker (the fallback explains the absence).
        burst_body = section_text(report, SECTION_HEADERS[-1])
        assert NO_DATA_MARKER not in burst_body
        assert "Burst is disabled" in burst_body

    def test_empty_input_cli_exit_zero(
        self, empty_analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """CLI on empty input: exit 0 and a valid file (REQ-2 + REQ-5)."""
        rc, err, report_path = run_ok(empty_analysis_output_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert report_path.exists()
        assert NO_DATA_MARKER in report_path.read_text()

    def test_partial_input_marks_missing_sections(
        self, partial_analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Only weight-share present: it renders data, others get '_no data_'."""
        module = load_report_module()
        report = module.build_report(partial_analysis_output_dir)
        ws_body = section_text(report, SECTION_HEADERS[0])
        assert parse_markdown_table(ws_body)[0] == WEIGHT_SHARE_COLUMNS
        assert NO_DATA_MARKER not in ws_body
        for header in DATA_SECTIONS[1:]:
            assert NO_DATA_MARKER in section_text(report, header)

    def test_header_only_csv_marks_no_data(self, tmp_path: pathlib.Path):
        """A CSV with headers but zero rows counts as 'no data' (empty df)."""
        module = load_report_module()
        d = tmp_path / "header-only"
        d.mkdir()
        (d / "weight-share-summary.csv").write_text(
            ",".join(WEIGHT_SHARE_COLUMNS) + "\n"
        )
        report = module.build_report(d)
        assert NO_DATA_MARKER in section_text(report, SECTION_HEADERS[0])

    def test_header_only_burst_csv_renders_fallback(self, tmp_path: pathlib.Path):
        """TASK-D01: header-only burst-summary.csv -> fallback note, not verdict."""
        module = load_report_module()
        d = tmp_path / "burst-header-only"
        d.mkdir()
        (d / "burst-summary.csv").write_text(",".join(BURST_COLUMNS) + "\n")
        body = section_text(module.build_report(d), SECTION_HEADERS[-1])
        assert "Burst is disabled" in body
        assert BURST_VERDICT_MARKER not in body


# =========================================================================
# VC-REP-04 — deterministic output (REQ-4)
# =========================================================================


class TestDeterminism:
    """Same input -> byte-identical output; rows are sorted."""

    def test_repeat_build_identical(self, analysis_output_dir: pathlib.Path):
        """Two build_report calls on the same dir are byte-identical."""
        module = load_report_module()
        first = module.build_report(analysis_output_dir)
        second = module.build_report(analysis_output_dir)
        assert first == second

    def test_shuffled_input_identical(
        self,
        analysis_output_dir: pathlib.Path,
        shuffled_analysis_output_dir: pathlib.Path,
    ):
        """Row order in the CSVs does not change the output (sorted rows)."""
        module = load_report_module()
        canonical = module.build_report(analysis_output_dir)
        shuffled = module.build_report(shuffled_analysis_output_dir)
        assert canonical == shuffled

    def test_cli_runs_byte_identical(
        self, analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """Two CLI runs produce byte-identical report files."""
        rc1, err1, path1 = run_ok(analysis_output_dir, tmp_path / "run1")
        rc2, err2, path2 = run_ok(analysis_output_dir, tmp_path / "run2")
        assert rc1 == 0 and rc2 == 0, f"stderr: {err1} / {err2}"
        assert path1.read_bytes() == path2.read_bytes()


# =========================================================================
# VC-CLI-01 — CLI contract (REQ-5)
# =========================================================================


class TestCli:
    """--input-dir/--output-dir contract and exit codes."""

    def test_help_flag_prints_usage_and_exits_zero(self):
        rc, out, err = run_report(["--help"])
        assert rc == 0, f"stderr: {err}"
        combined = out + err
        assert "usage:" in combined.lower()
        assert "--input-dir" in combined
        assert "--output-dir" in combined

    def test_missing_required_flags_exits_nonzero(self):
        """No arguments -> argparse error, non-zero exit."""
        rc, _out, err = run_report([])
        assert rc != 0
        assert "error" in err.lower() or "usage" in err.lower()

    def test_missing_input_dir_exits_nonzero_with_message(self, tmp_path: pathlib.Path):
        """Nonexistent --input-dir -> non-zero exit and a clear message.

        "missing" is the nonexistent dir name: the real script must name the
        path it could not find. The Python "can't open file" fallback for the
        missing script does not contain it, so this stays red in the red phase.
        """
        out_dir = tmp_path / "output"
        rc, _out, err = run_report(
            ["--input-dir", str(tmp_path / "missing"), "--output-dir", str(out_dir)]
        )
        assert rc != 0
        assert "missing" in err


# =========================================================================
# End-to-end: fixture data -> markdown report (REQ-1/3/5/6)
# =========================================================================


class TestEndToEnd:
    """Running the script on fixture data produces the contract report."""

    def test_happy_path_report_exists_with_content(
        self, analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """VC-REP-01 + VC-CLI-01: exit 0, report file with every section."""
        rc, err, report_path = run_ok(analysis_output_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        assert report_path.exists()
        report = report_path.read_text()
        for header in SECTION_HEADERS:
            assert header in report
        assert "0.00393082" in report
        assert "Max throttling ratio: 0.9" in report
        assert "19.81" in report
        assert "`cpu.max.burst=25000`" in report  # TASK-D01 measured verdict

    def test_report_is_markdown_tables(
        self, analysis_output_dir: pathlib.Path, tmp_path: pathlib.Path
    ):
        """REQ-6: structure assertions via pipe-table parsing, no network."""
        rc, err, report_path = run_ok(analysis_output_dir, tmp_path)
        assert rc == 0, f"stderr: {err}"
        report = report_path.read_text()
        for header in SECTION_HEADERS:
            body = section_text(report, header)
            assert "|" in body or "Burst is disabled" in body
        # Separator rows exist under the data sections.
        assert "|--" in report

    def test_input_files_contract(self, analysis_output_dir: pathlib.Path):
        """REQ-1: the report reads exactly the pinned input file names."""
        module = load_report_module()
        report = module.build_report(analysis_output_dir)
        for filename in REPORT_INPUT_FILES:
            df = module.load_table(analysis_output_dir, filename)
            assert df is not None
        assert report  # non-empty
