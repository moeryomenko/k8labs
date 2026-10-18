from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_gate as rg  # noqa: E402


def test_runtime_schedulers_are_the_three_new_ones():
    assert set(rg.RUNTIME_SCHEDULERS) == {"scx_mavd", "scx_mlfq", "scx_nitosis"}


def test_gates_are_five():
    assert rg.RUNTIME_GATES == ("build", "verifier", "attach", "progress", "detach")


def test_gate_results_have_all_five():
    for name in rg.RUNTIME_SCHEDULERS:
        res = rg.gate_results(name)
        assert set(res) == set(rg.RUNTIME_GATES)


def test_mavd_blocked_on_pinned_kernel():
    # Arena-scalar flag is unavailable on the pinned kernel.
    assert rg.all_gates_pass("scx_mavd") is False
    assert rg.gate_results("scx_mavd")["verifier"] is False


def test_mlfq_and_nitosis_not_blocked_by_verifier():
    for name in ("scx_mlfq", "scx_nitosis"):
        assert rg.gate_results(name)["verifier"] is True
        assert rg.gate_results(name)["attach"] is True
        assert rg.gate_results(name)["progress"] is True
        assert rg.gate_results(name)["detach"] is True


def test_eligible_excludes_mavd():
    assert "scx_mavd" not in rg.eligible_schedulers()
    assert "scx_mavd" in rg.blocked_schedulers()


def test_unknown_scheduler_raises():
    try:
        rg.gate_results("scx_bpfland")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
