"""End-to-end dry run of the factorial campaign analysis."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parents[1]
SCRIPT = HERE / "dry_run.py"

_spec = importlib.util.spec_from_file_location("dry_run", SCRIPT)
assert _spec and _spec.loader
dry = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dry
_spec.loader.exec_module(dry)


def test_dry_run_passes() -> None:
    assert dry.check() == []


def test_null_data_has_no_effect_verdicts() -> None:
    result = dry.analyse_rows(dry.synthetic_rows(inject_defect=False))
    assert dry.effect_verdicts(result) == []
    assert result.verdicts


def test_injected_defect_is_detected_exactly_once_per_workload() -> None:
    result = dry.analyse_rows(dry.synthetic_rows(inject_defect=True))
    detected = dry.effect_verdicts(result)
    assert {(v.workload, v.scheduler) for v in detected} == {
        (workload, dry.AFFECTED_SCHEDULER) for workload in dry.WORKLOADS
    }
    assert all(v.decision == "regression" for v in detected)
