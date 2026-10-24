"""Contract tests for the predeclared scenario matrix and primary metrics."""

from __future__ import annotations

from scenarios import (
    ALL_SCENARIOS,
    CONTRASTS,
    DEFAULT_SCENARIOS,
    PARENT_WEIGHT,
    PRIMARY_METRICS,
    SCENARIOS,
    contrast_for,
    contrast_name,
    primary_metrics,
    scenario_groups,
    scenario_names,
)


def test_default_scenarios_exclude_the_governor_only_steal_case() -> None:
    assert "steal" not in DEFAULT_SCENARIOS
    assert "steal" in ALL_SCENARIOS
    assert set(DEFAULT_SCENARIOS).issubset(set(scenario_names()))


def test_every_scenario_has_groups_and_workloads() -> None:
    for scenario in scenario_names():
        groups = scenario_groups(scenario)
        assert groups, scenario
        for group in groups:
            assert group["weight"] > 0
            assert group["workloads"], f"{scenario}/{group['name']}"
            for workload in group["workloads"]:
                assert workload["kind"] in {
                    "cpu",
                    "wakeup",
                    "periodic",
                    "churn",
                    "pingpong",
                }


def test_primary_metric_references_an_existing_group() -> None:
    for metric in PRIMARY_METRICS:
        groups = {group["name"] for group in scenario_groups(str(metric["scenario"]))}
        assert metric["group"] in groups, metric
        assert metric["metric"]
        assert metric["direction"] in {"lower", "higher", "equivalent"}
        assert metric["expected"] in {"improve", "equivalent", "regress"}
        assert metric["feature"]


def test_quota_keeps_a_null_control() -> None:
    quota = primary_metrics("quota")
    assert quota
    assert any(metric["expected"] == "equivalent" for metric in quota)
    assert {
        "delivered_cpu_pct",
        "throttled_period_pct",
    }.issubset({metric["metric"] for metric in quota})


def test_steal_is_a_governor_contrast_and_the_rest_is_kernel() -> None:
    assert contrast_name("wakeup") == "kernel"
    assert contrast_for("wakeup") == CONTRASTS["kernel"]
    assert contrast_name("steal") == "governor"
    assert contrast_for("steal") == CONTRASTS["governor"]


def test_steal_metric_declares_its_own_contrast() -> None:
    steal = [metric for metric in PRIMARY_METRICS if metric["scenario"] == "steal"]
    assert steal
    assert all(metric.get("contrast") == "governor" for metric in steal)


def test_parent_weight_is_the_maximum() -> None:
    # The parent must dominate root-level system slices so a kernel comparison
    # is not diluted by unrelated kubelet activity.
    assert PARENT_WEIGHT == 10000
    assert SCENARIOS["wakeup"]["groups"]
