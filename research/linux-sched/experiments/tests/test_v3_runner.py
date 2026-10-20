"""The v3 runner must emit policy-tagged, paired quota data."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parents[1]
LIB = HERE / "lib.sh"
RUNNER = HERE / "run-block.sh"
CAPTURE = HERE.parent / "capture" / "capture-block.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_block_csv_carries_the_policy_column() -> None:
    lib = _read(LIB)
    assert "block_id,scheduler,workload,metric,value,direction,policy" in lib
    assert 'local policy="${8:-}"' in lib


def test_limited_policy_reads_the_frozen_quota_and_fails_closed() -> None:
    lib = _read(LIB)
    assert "frozen_quota_for()" in lib
    assert "frozen-quotas.yaml" in lib
    assert "die \"no frozen quota manifest" in lib
    runner = _read(RUNNER)
    assert 'LINUX_SCHED_CPU_LIMIT="$(frozen_quota_for "$scenario")"' in runner
    assert 'die "no frozen quota for ${scenario}"' in runner


def test_confirmatory_v3_runs_both_policies_per_arm() -> None:
    runner = _read(RUNNER)
    assert 'policies=(unlimited limited)' in runner
    assert 'for policy in "${policies[@]}"' in runner
    assert 'run_block "$SCENARIO_NAME" "$arm" "$b" "$raw_dir" "$policy"' in runner
    # Pairing requires the same block id under both policies.
    assert '"policies":"%s"' in runner


def test_capture_uses_the_policy_renderer() -> None:
    capture = _read(CAPTURE)
    assert "cpu_limit_policy.py" in capture
    assert "--policy" in capture
    assert 'LINUX_SCHED_CPU_POLICY' in capture


def test_quota_calibration_sweeps_eevdf_and_records_both_snapshots() -> None:
    runner = _read(RUNNER)
    assert "quota-calibrate)" in runner
    assert "cmd_quota_calibrate()" in runner
    assert "quota_calibration.py" in runner
    assert "calibration.csv" in runner
    capture = _read(CAPTURE)
    assert "--cpu-stat-before" in capture
    assert "--cpu-stat-after" in capture


def test_quota_calibration_writes_one_row_per_workload() -> None:
    runner = _read(RUNNER)
    # A re-run must replace the workload's row, not duplicate it: freeze_quota
    # rejects duplicates.
    assert 'r.get("workload") != workload' in runner
