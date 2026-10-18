from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gate as og  # noqa: E402


def test_observer_manifest_present():
    assert og.observer_manifest_present() is True


def test_observer_validator_present():
    assert og.observer_validator_present() is True


def test_run_gate_passes():
    res = og.run_gate()
    assert all(res.values())
