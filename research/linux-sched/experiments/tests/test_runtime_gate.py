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


def test_mavd_blocked_only_by_its_unbuilt_binary():
    # The arena-scalar flag exists in the pinned source, so the interface is no
    # longer the blocker: scx_mavd is blocked here because no binary is built.
    assert rg.ARENA_SCALAR_AVAILABLE is True
    assert rg.gate_results("scx_mavd")["verifier"] is True
    assert rg.gate_results("scx_mavd")["build"] is False
    assert rg.all_gates_pass("scx_mavd") is False


def test_build_gate_is_false_until_binaries_are_staged():
    for name in rg.RUNTIME_SCHEDULERS:
        assert rg.gate_results(name)["build"] is False


def test_uncached_interface_checks_do_not_hardcode_blocked():
    for name in rg.RUNTIME_SCHEDULERS:
        results = rg.gate_results(name)
        for gate in ("verifier", "attach", "progress", "detach"):
            assert results[gate] is True


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
