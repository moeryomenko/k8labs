"""Tests for the bracketed CPU-limit search.

The pilot decides where the campaign spends its budget, so the stopping rules
are tested as pure functions with no cluster. The cases that matter are the
ones where a naive search would produce a bracket that is not actually
bracketed: unsatisfiable runs, a cap smaller than the growth series, and runs
where every cell failed to measure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "campaign"))

from bracket import (  # noqa: E402
    Bracket,
    Outcome,
    PilotError,
    StopReason,
    Trial,
    evaluate_trials,
    plan_limits,
)


class TestPlanLimits:
    def test_growth_is_strictly_ascending(self) -> None:
        limits = plan_limits(
            min_limit_millicores=50,
            max_limit_millicores=4000,
            growth=2.0,
            max_cells=8,
        )
        assert list(limits) == sorted(set(limits))
        assert limits[0] == 50
        assert limits[-1] == 4000

    def test_last_limit_is_the_node_cap(self) -> None:
        limits = plan_limits(
            min_limit_millicores=50,
            max_limit_millicores=300,
            growth=2.0,
            max_cells=8,
        )
        assert limits == (50, 100, 200, 300)

    def test_never_exceeds_the_cap(self) -> None:
        limits = plan_limits(
            min_limit_millicores=100,
            max_limit_millicores=250,
            growth=10.0,
            max_cells=4,
        )
        assert all(limit <= 250 for limit in limits)

    def test_max_cells_bounds_the_plan(self) -> None:
        limits = plan_limits(
            min_limit_millicores=50,
            max_limit_millicores=4000,
            growth=2.0,
            max_cells=3,
        )
        assert limits == (50, 100, 200)

    def test_cap_equal_to_min_yields_one_limit(self) -> None:
        assert plan_limits(
            min_limit_millicores=200,
            max_limit_millicores=200,
            growth=2.0,
            max_cells=5,
        ) == (200,)

    def test_small_limits_are_rounded_to_millicores(self) -> None:
        limits = plan_limits(
            min_limit_millicores=7,
            max_limit_millicores=1000,
            growth=1.5,
            max_cells=3,
        )
        assert limits == (7, 11, 16)

    def test_rejects_non_growth(self) -> None:
        with pytest.raises(ValueError, match="growth"):
            plan_limits(
                min_limit_millicores=50,
                max_limit_millicores=4000,
                growth=1.0,
                max_cells=4,
            )

    def test_rejects_cap_below_minimum(self) -> None:
        with pytest.raises(ValueError, match="max_limit_millicores"):
            plan_limits(
                min_limit_millicores=200,
                max_limit_millicores=100,
                growth=2.0,
                max_cells=4,
            )


def trial(limit: int, outcome: Outcome) -> Trial:
    return Trial(limit_millicores=limit, outcome=outcome, reason=outcome.value)


class TestEvaluateTrials:
    def test_bracket_holds_the_boundary(self) -> None:
        bracket = evaluate_trials(
            [
                trial(50, Outcome.BREACHES_SLO),
                trial(100, Outcome.BREACHES_SLO),
                trial(200, Outcome.MEETS_SLO),
                trial(400, Outcome.MEETS_SLO),
            ],
            node_capacity_millicores=4000,
        )
        assert isinstance(bracket, Bracket)
        assert bracket.passing_millicores == 200
        assert bracket.failing_below_millicores == 100
        assert bracket.stop is StopReason.MEETS_SLO

    def test_first_trial_passing_leaves_failure_unmeasured(self) -> None:
        bracket = evaluate_trials(
            [trial(50, Outcome.MEETS_SLO), trial(100, Outcome.MEETS_SLO)],
            node_capacity_millicores=4000,
        )
        assert bracket.passing_millicores == 50
        assert bracket.failing_below_millicores is None

    def test_invalid_cell_below_does_not_become_a_failure(self) -> None:
        bracket = evaluate_trials(
            [
                trial(50, Outcome.INVALID),
                trial(100, Outcome.BREACHES_SLO),
                trial(200, Outcome.MEETS_SLO),
            ],
            node_capacity_millicores=4000,
        )
        assert bracket.failing_below_millicores == 100

    def test_capacity_reached_is_reported(self) -> None:
        bracket = evaluate_trials(
            [
                trial(1000, Outcome.BREACHES_SLO),
                trial(4000, Outcome.MEETS_SLO),
            ],
            node_capacity_millicores=4000,
        )
        assert bracket.passing_millicores == 4000
        assert bracket.stop is StopReason.CAPACITY_REACHED

    def test_unsatisfiable_raises_instead_of_guessing(self) -> None:
        with pytest.raises(PilotError, match="unsatisfiable"):
            evaluate_trials(
                [
                    trial(1000, Outcome.BREACHES_SLO),
                    trial(4000, Outcome.BREACHES_SLO),
                ],
                node_capacity_millicores=4000,
            )

    def test_invalid_only_raises_a_distinct_error(self) -> None:
        with pytest.raises(PilotError, match="every trial was invalid"):
            evaluate_trials(
                [trial(100, Outcome.INVALID), trial(200, Outcome.INVALID)],
                node_capacity_millicores=4000,
            )

    def test_empty_trials_raise(self) -> None:
        with pytest.raises(PilotError, match="no trials"):
            evaluate_trials([], node_capacity_millicores=4000)

    def test_duplicate_limits_raise(self) -> None:
        with pytest.raises(PilotError, match="strictly increase"):
            evaluate_trials(
                [trial(100, Outcome.BREACHES_SLO), trial(100, Outcome.MEETS_SLO)],
                node_capacity_millicores=4000,
            )

    def test_limit_above_node_capacity_raises(self) -> None:
        with pytest.raises(PilotError, match="exceeds node capacity"):
            evaluate_trials(
                [trial(5000, Outcome.MEETS_SLO)],
                node_capacity_millicores=4000,
            )
