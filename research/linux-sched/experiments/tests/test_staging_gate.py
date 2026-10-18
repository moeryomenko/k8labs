from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import staging_gate as sg  # noqa: E402


def test_staged_schedulers_are_three():
    assert set(sg.STAGED_SCHEDULERS) == {"scx_mavd", "scx_mlfq", "scx_nitosis"}


def test_staging_script_present():
    assert sg.staging_script_present() is True


def test_run_gate_has_expected_keys():
    res = sg.run_gate()
    assert "staging_script" in res
    assert "binaries" in res
    assert set(res["binaries"]) == set(sg.STAGED_SCHEDULERS)
