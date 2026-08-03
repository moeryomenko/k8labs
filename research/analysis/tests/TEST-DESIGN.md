# TASK-D01 + TASK-D08 — Test Design: generate-report.py debt fixes

Status: RED phase (tests written; the two target behaviors are not yet
implemented in `research/analysis/generate-report.py`). This doc is copied to
`.plans/requests-limits-scheduler-interaction-debt/tasks/TASK-D01/` (primary)
and `.plans/requests-limits-scheduler-interaction-debt/tasks/TASK-D08/`
(combined-work copy).

Run: `python3 -m pytest tests/test_report.py -q` from `research/analysis`.

## 1. Scope

Test-first design (no production changes) for two defects in the report
generator. The engineer implements `generate-report.py`; this package pins the
contract it must build.

| Defect | File | Symptom |
|---|---|---|
| TASK-D01 | `research/analysis/generate-report.py` | `_burst_section()` hardcodes "Burst is disabled" even though the live cpu-burst experiment (Family E, `research/experiments/data/cpu-burst/summary.csv`) proves burst works: with `cpu.max.burst=25000` (== quota) mean nr_throttled dropped 105 -> 0 and throttled_usec 5.28M -> 0; burst=100000 was rejected EINVAL because burst > quota |
| TASK-D08 | `research/analysis/generate-report.py` | `_qos_priority()` maps a slice to guaranteed only when its name contains "guaranteed"; a TRUE Guaranteed pod (systemd driver) lives at `kubepods-pod<uid>.slice` (no "guaranteed" in the name) and sorts last in the QoS table |

Deliverables: updated `tests/test_report.py`, extended `tests/conftest.py`,
this document (copied to `.plans/.../TASK-D01/` and `.plans/.../TASK-D08/`).

## 2. Requirements (debt plan)

| REQ | Requirement | Pinned by |
|---|---|---|
| REQ-1 (D01) | Burst data-present case renders the measured verdict: applied burst value (`cpu.max.burst=25000`), mean nr_throttled 105 -> 0, mean throttled_usec 5280000 -> 0, and the kernel constraint note (burst <= quota; 100000 EINVAL) | `TestBurstSection::test_data_present_renders_measured_verdict`, `TestEndToEnd::test_happy_path_report_exists_with_content` |
| REQ-2 (D01) | Burst no-data fallback still renders the static note ("No burst experiment data. Burst is disabled: cpu.max.burst defaults to 0...") | `TestBurstSection::test_no_data_renders_static_fallback`, `test_header_only_burst_csv_renders_fallback`, `test_no_burst_cell_renders_fallback`, `TestNoData` |
| REQ-3 (D08) | QoS table ordering: `kubepods-pod*.slice` sorts as guaranteed (before burstable/besteffort); `kubepods-guaranteed.slice` still sorts as guaranteed | `TestQosSection::test_direct_pod_slice_sorts_as_guaranteed`, `TestQosPriority` |
| REQ-4 (D01/D08) | Existing determinism/CLI/no-data tests unaffected | full suite: 38/38 non-target tests pass in red phase |
| REQ-5 (D01) | Pin the burst input filename + schema so the engineer builds exactly to it | section 4 below |

## 3. Input data contract (additions to the TASK-018 contract)

All files live in the `--input-dir`. Only one file is added and one input shape
is clarified:

| File | Columns |
|---|---|
| `burst-summary.csv` (NEW, TASK-D01) | `cell,replicate,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_max_burst,cpu_max_quota` |
| `qos-summary.csv` (existing) | `cell,qos_slice,pod,cpu_weight,achieved_share,throttled_usec` — `qos_slice` MAY be `kubepods-pod<uid>.slice` (direct TRUE-Guaranteed pod slice, TASK-D08) |

### 3.1 `burst-summary.csv` — pinned schema (REQ-5)

```
cell,replicate,nr_periods,nr_throttled,throttled_usec,usage_usec,cpu_max_burst,cpu_max_quota
```

- `cell` — experiment matrix label. Copied from the real
  `research/experiments/data/cpu-burst/summary.csv` (e.g.
  `request=-limit=250m-burst=` for the no-burst cell,
  `request=-limit=250m-burst=100000` for the burst cell).
- `replicate` — 1..N.
- `nr_periods`, `nr_throttled`, `throttled_usec`, `usage_usec` — measured
  cgroup stats (same semantics as the runner's summary.csv).
- `cpu_max_burst` — the value **actually written to `cpu.max.burst`** during
  the cell (kernel-validated): `0` for the no-burst baseline, `25000` for the
  burst cell. This is the applied value the generator must render.
- `cpu_max_quota` — the CFS quota in microseconds (`25000` == 250m).

**Critical rule: the generator MUST read the applied burst value from
`cpu_max_burst` and NEVER parse the `cell` label.** The matrix label of the
burst cell is `burst=100000` (the value the matrix requested), but the kernel
rejected 100000 (EINVAL, burst > quota) and the helper applied 25000 == quota.
The analyzer normalizes the applied value into `cpu_max_burst`; the label is
just an identifier.

## 4. Pinned contract: generate-report.py (what the engineer must build)

### 4.1 New public constant (TASK-D01)

- `BURST_CSV = "burst-summary.csv"` — module-level, alongside the existing
  `*_CSV` constants.

### 4.2 `_burst_section()` — two branches (TASK-D01)

Signature change: `_burst_section(input_dir: pathlib.Path) -> str` (it must
read `burst-summary.csv`; tests exercise it only through `build_report`).

Branch A — data present: `burst-summary.csv` exists AND has at least one data
row AND at least one row with `cpu_max_burst > 0`. Renders (exact phrases the
tests assert):

```
Measured verdict: `cpu.max.burst=25000` eliminated CFS throttling (mean nr_throttled 105 -> 0, mean throttled_usec 5280000 -> 0).

| cell | mean_nr_throttled | mean_throttled_usec |
|---|---|---|
| request=-limit=250m-burst= | 105 | 5280000 |
| request=-limit=250m-burst=100000 | 0 | 0 |

Kernel constraint: `cpu.max.burst` cannot exceed the CPU quota (burst <= quota); burst=100000 was rejected EINVAL.
```

Derivations (all deterministic, REQ-4):
- applied burst value = `max(cpu_max_burst)` across all rows -> 25000.
- baseline group = rows with `cpu_max_burst == 0`; burst group = rows with
  `cpu_max_burst > 0`.
- `mean nr_throttled 105 -> 0`: baseline mean nr_throttled -> burst mean
  nr_throttled; same for throttled_usec (5280000 -> 0).
- Table: one row per distinct `cell`, mean over that cell's replicates, sorted
  by `cell` ascending; floats via `format(v, "g")` (105.0 -> `105`,
  5280000.0 -> `5280000`).
- Kernel constraint line is pinned prose (the EINVAL rejection is an
  experimental finding, not per-run data).
- The fallback note must NOT appear in this branch.

Branch B — data absent: `burst-summary.csv` missing, OR zero data rows
(header-only), OR no row with `cpu_max_burst > 0`. Renders the fallback note:

```
No burst experiment data. Burst is disabled: `cpu.max.burst` defaults to 0 on this cluster, so no burst credit is available and throttled workloads cannot absorb latency spikes with burst capacity.
```

This keeps the TASK-018 substrings (`Burst is disabled`, `cpu.max.burst`) and
adds the no-data explanation the debt plan asks for.

### 4.3 `_qos_priority()` — direct pod slice is guaranteed (TASK-D08)

Same signature `_qos_priority(slice_name: str) -> int`. Add one rule, keep the
rest:

- `kubepods-pod<uid>.slice` (segment after `kubepods-` starts with `pod`,
  mirroring `qos-analyze.py::_slice_by_qos`) -> `0` (guaranteed), the same as
  `kubepods-guaranteed.slice`.
- `kubepods-burstable.slice` -> 1, `kubepods-besteffort.slice` -> 2,
  anything unrecognized -> 3 (unchanged).

Note the rule is a prefix check (`kubepods-pod`), so
`kubepods-burstable-pod<uid>.slice` still maps to burstable (1).

## 5. Fixtures (tests/conftest.py)

- `BURST_COLUMNS` / `BURST_ROWS` — the pinned `burst-summary.csv` schema and
  6 rows (2 cells x 3 replicates). No-burst cell: nr_throttled 105,
  throttled_usec 5200000/5300000/5340000 (mean 5280000), cpu_max_burst 0,
  cpu_max_quota 25000. Burst cell: nr_throttled 0, throttled_usec 0,
  cpu_max_burst 25000, cpu_max_quota 25000. Cell labels are the real matrix
  labels (burst cell says `burst=100000`).
- `QOS_ROWS_DIRECT_GUARANTEED` — qos-summary rows for the TRUE-Guaranteed
  layout: `kubepods-podg1.slice` (qos_slice == pod, self-representing) plus
  burstable/besteffort; no `kubepods-guaranteed.slice` wrapper. Mirrors what
  `qos-analyze.py` emits for a direct-slice snapshot.
- `analysis_output_dir` — now writes all EIGHT CSVs including
  `burst-summary.csv` (REPORT_INPUT_FILES extended).
- `shuffled_analysis_output_dir` — burst rows reversed too (determinism).
- `qos_direct_guaranteed_output_dir` — `build_analysis_output_dir` with
  `qos_rows=QOS_ROWS_DIRECT_GUARANTEED`.

## 6. RED phase expectations (verified)

`python3 -m pytest tests/test_report.py -q` against the current
`generate-report.py`: **6 failed, 38 passed**. The 6 failures are exactly the
target-behavior tests:

| Test | Why RED |
|---|---|
| TestModuleContract::test_module_exposes_burst_csv_name | no `BURST_CSV` constant |
| TestQosSection::test_direct_pod_slice_sorts_as_guaranteed | pod slice sorts last (priority 3) |
| TestQosPriority::test_direct_pod_slice_is_guaranteed | `_qos_priority` returns 3 |
| TestBurstSection::test_data_present_renders_measured_verdict | burst file ignored |
| TestBurstSection::test_no_data_renders_static_fallback | fallback text lacks the no-data phrase |
| TestEndToEnd::test_happy_path_report_exists_with_content | burst section renders fallback |

All other tests (including determinism, CLI, no-data, and the untouched
test_qos/test_latency/test_tunables suites) pass — REQ-4 holds. The full
suite is 235 passed / 6 failed with no collateral damage.

## 7. Decisions / assumptions (grilled and resolved)

1. **Filename `burst-summary.csv`, not `summary.csv`.** The report generator
   consumes analyzer *outputs*; the runner's raw `summary.csv` would collide
   with the other families and its `cpu_max` column (quota) cannot carry the
   applied burst value. `burst-summary.csv` follows the `*-summary.csv` naming
   convention.
2. **Applied burst value is data, not label text.** The real summary labels
   the burst cell `burst=100000` while the kernel-validated applied value was
   25000. Rendering from `cpu_max_burst` keeps the report truthful and
   deterministic; parsing the label would print a value the kernel rejected.
3. **Data-present requires at least one row with `cpu_max_burst > 0`.**
   Otherwise the measured verdict ("eliminated throttling") would be fabricated.
   Empty/header-only/all-zero inputs fall back to the static note.
4. **QoS priority rule is a prefix match** (`kubepods-pod*`), mirroring
   `qos-analyze.py::_slice_by_qos` (`qos.startswith("pod")`). Burstable pod
   slices (`kubepods-burstable-pod*.slice`) are unaffected.
5. **Main fixture gains burst data; QoS main fixture unchanged.** The happy
   path now renders the measured verdict (that is the point of the debt fix);
   the TRUE-Guaranteed QoS case gets a dedicated fixture so the wrapper-layout
   tests are untouched (REQ-4).
6. **Tests run without network/cluster**: all fixtures are CSV files written
   to tmp dirs; no cluster, no ansible, no perfetto.

## 8. Test inventory (test_report.py — 44 tests, 6 RED)

TestModuleContract (2), TestLoadTable (4), TestSectionPresence (2),
TestWeightShareSection (2), TestHeatmapSection (2), TestRegionSection (2),
TestQosSection (3), TestQosPriority (2), TestLatencySection (3),
TestTunablesSection (2), TestBurstSection (3), TestNoData (5),
TestDeterminism (3), TestCli (3), TestEndToEnd (3).
