"""Tests for the frozen CPU-limits design."""

from __future__ import annotations

from experiments import design as DESIGN


def test_cells_are_unique_and_complete() -> None:
    assert len(DESIGN.CELLS) == 36
    assert len({cell.cell_id for cell in DESIGN.CELLS}) == 36
    assert sum(cell.condition == "solo" for cell in DESIGN.CELLS) == 16
    assert sum(cell.condition == "contention" for cell in DESIGN.CELLS) == 20


def test_cells_only_use_frozen_members() -> None:
    expected = {
        "solo": set(DESIGN.SOLO_POLICIES),
        "contention": set(DESIGN.CONTENTION_POLICIES),
    }
    for cell in DESIGN.CELLS:
        assert cell.workload in DESIGN.WORKLOADS
        assert cell.policy in DESIGN.POLICIES
        assert cell.condition in DESIGN.CONDITIONS
        assert cell.policy in expected[cell.condition]


def test_seeded_schedule_is_deterministic() -> None:
    first = DESIGN.schedule(blocks=3, seed=27)
    second = DESIGN.schedule(blocks=3, seed=27)
    changed = DESIGN.schedule(blocks=3, seed=28)
    assert first == second
    assert first != changed
    assert all(len(block) == 36 for block in first)


def test_resources_match_the_frozen_cpu_values() -> None:
    expected = {
        "cpu-bound": (2000, 500),
        "io-bound": (1000, 250),
        "memory-bound": (1000, 500),
        "hybrid": (1000, 500),
    }
    assert DESIGN.CPU_VALUES == {
        workload: {"fit": fit, "low": low} for workload, (fit, low) in expected.items()
    }
    for workload, (fit, low) in expected.items():
        assert DESIGN.resources_for(workload, "fit") == (fit, fit)
        assert DESIGN.resources_for(workload, "guaranteed-low") == (low, low)
        assert DESIGN.resources_for(workload, "burstable-low") == (100, low)


def test_resources_are_non_negative_and_consistent() -> None:
    assert DESIGN.validate_design() == []
    for cell in DESIGN.CELLS:
        for value in (cell.request_millicores, cell.limit_millicores):
            assert value is None or value >= 0
        if cell.request_millicores is not None and cell.limit_millicores is not None:
            assert cell.request_millicores <= cell.limit_millicores
