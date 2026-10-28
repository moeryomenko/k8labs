"""Tests for bounded, reproducible campaign planning."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "campaign"))

from campaign import (  # noqa: E402
    BudgetExceeded,
    CampaignConfig,
    Factor,
    plan_campaign,
)


def factors() -> tuple[Factor, ...]:
    return (
        Factor("cpu_limit_millicores", (100, 200)),
        Factor("cpu_request_millicores", (50, 100)),
        Factor("replicas", (1, 2)),
        Factor("request_rate_per_second", (10, 20)),
    )


def test_preview_reports_full_cost_without_running_cells() -> None:
    plan = plan_campaign(
        factors(), CampaignConfig(repetitions=3, seed=7, budget_cap=100)
    )

    assert plan.design == "full"
    assert plan.full_cells == 16
    assert plan.total_trials == 48
    assert plan.preview["full_trials"] == 48
    assert plan.preview["estimated_seconds"] == 48 * 600
    assert len(plan.cells) == 16
    assert (
        plan.cells
        == plan_campaign(
            factors(), CampaignConfig(repetitions=3, seed=7, budget_cap=100)
        ).cells
    )


def test_budget_requires_confirmation_before_expensive_full_run() -> None:
    with pytest.raises(BudgetExceeded, match="confirmation"):
        plan_campaign(factors(), CampaignConfig(repetitions=2, budget_cap=10))


def test_fractional_fallback_is_bounded_and_contains_baseline() -> None:
    plan = plan_campaign(
        factors(),
        CampaignConfig(
            repetitions=2,
            budget_cap=10,
            allow_fractional=True,
            confirmed=True,
            seed=3,
        ),
    )

    assert plan.design == "fractional"
    assert plan.total_trials <= 10
    assert all(cell[f.name] in f.levels for cell in plan.cells for f in factors())
    assert plan.cells[0] == {f.name: f.levels[0] for f in factors()}


def test_invalid_factor_and_budget_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        plan_campaign((Factor("x", (1, 1)),), CampaignConfig())
    with pytest.raises(ValueError, match="repetitions"):
        plan_campaign(factors(), CampaignConfig(repetitions=0))


def test_fractional_mode_preserves_all_factor_levels_when_possible() -> None:
    design = plan_campaign(
        factors(),
        CampaignConfig(
            repetitions=1,
            budget_cap=9,
            allow_fractional=True,
            confirmed=True,
        ),
    )
    for factor in factors():
        assert {cell[factor.name] for cell in design.cells} == set(factor.levels)
