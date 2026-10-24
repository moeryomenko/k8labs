"""Scenario matrix for the stock-7.3 versus custom-kernel scheduler delta.

The custom kernel is built from the local linux-next tree that contains the
``sched-core-2026-10-18`` merge tag. The stock kernel is Fedora's
``7.3.0-65.fc46``. The scheduler-relevant configuration is identical between
the two kernels; the custom kernel adds ``CONFIG_PREFERRED_CPU`` and
``CONFIG_STEAL_GOVERNOR``.

Every scenario exercises a mechanism named in the series analysis:

- ``wakeup``:  EEVDF wakeup and slice-protection changes under CPU pressure.
- ``slice``:   short periodic bursts against full-time CPU load.
- ``fairness``: equal-weight periodic tasks; candidate-selection and timer
  changes show up as per-thread share dispersion.
- ``quota``:   CFS bandwidth control; the net series is expected to preserve
  throttling behaviour, so this is the null control.
- ``churn``:   task creation and exit under CPU pressure; scheduler constant
  overhead.
- ``steal``:   two vCPUs executing an interacting producer/consumer pair plus
  independent burners while the host steals CPU time. It is run only with the
  steal-governor treatment on the custom kernel.

The module also owns the predeclared primary metrics and contrasts, so the
analysis cannot choose a favourable metric after seeing the data.
"""

from __future__ import annotations

from typing import Any

DEFAULT_CPUS = 2
PARENT_WEIGHT = 10000

ARMS = ("stock", "custom")
STEAL_ARMS = ("custom-nogov", "custom-gov")
ALL_ARMS = ARMS + STEAL_ARMS

# contrast name -> (control arm, treatment arm)
CONTRASTS = {
    "kernel": ("stock", "custom"),
    "governor": ("custom-nogov", "custom-gov"),
}

DEFAULT_SCENARIOS = ("wakeup", "slice", "fairness", "quota", "churn")
ALL_SCENARIOS = DEFAULT_SCENARIOS + ("steal",)

SCENARIOS: dict[str, dict[str, Any]] = {
    "wakeup": {
        "description": (
            "Two full-time CPU hogs at weight 400 plus a 1 ms timer probe at "
            "weight 100. Measures the wakeup delay distribution of the probe "
            "while both vCPUs are busy."
        ),
        "groups": [
            {
                "name": "load",
                "weight": 400,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
            {
                "name": "probe",
                "weight": 100,
                "workloads": [{"kind": "wakeup", "interval_us": 1000}],
            },
        ],
    },
    "slice": {
        "description": (
            "Two full-time CPU hogs at weight 400 plus a 100-weight task that "
            "burns 500 us every 5 ms. Measures how often the short burst is "
            "delayed or misses its next period."
        ),
        "groups": [
            {
                "name": "load",
                "weight": 400,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
            {
                "name": "burst",
                "weight": 100,
                "workloads": [
                    {
                        "kind": "periodic",
                        "threads": 1,
                        "period_us": 5000,
                        "burn_us": 500,
                    }
                ],
            },
        ],
    },
    "fairness": {
        "description": (
            "Two CPU hogs at weight 400 plus four equal-weight periodic tasks "
            "at weight 100. Measures tail wakeup delay and the dispersion of "
            "per-thread completion counts."
        ),
        "groups": [
            {
                "name": "load",
                "weight": 400,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
            {
                "name": "fair",
                "weight": 100,
                "workloads": [
                    {
                        "kind": "periodic",
                        "threads": 4,
                        "period_us": 2000,
                        "burn_us": 200,
                    }
                ],
            },
        ],
    },
    "quota": {
        "description": (
            "A 100-weight group capped at 0.5 CPU with two hogs and a 1 ms "
            "timer probe, next to an uncapped 100-weight group with two hogs. "
            "The null control: the net series preserves CFS bandwidth "
            "enforcement, so delivered CPU and throttle ratios must not move."
        ),
        "groups": [
            {
                "name": "limited",
                "weight": 100,
                "cpu_max": "50000 100000",
                "workloads": [
                    {"kind": "cpu", "threads": 2},
                    {"kind": "wakeup", "interval_us": 1000},
                ],
            },
            {
                "name": "open",
                "weight": 100,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
        ],
    },
    "churn": {
        "description": (
            "Two CPU hogs at weight 400 plus short-lived tasks created at "
            "200 per second at weight 100. Measures task completion latency "
            "and scheduler context-switch overhead."
        ),
        "groups": [
            {
                "name": "load",
                "weight": 400,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
            {
                "name": "churn",
                "weight": 100,
                "workloads": [{"kind": "churn", "rate": 200, "work_us": 20}],
            },
        ],
    },
    "steal": {
        "description": (
            "An interacting producer/consumer pair hard-pinned one thread per "
            "vCPU plus two unpinned independent burners, while the host steals "
            "CPU time. Run with the steal governor off and on. The governor "
            "contracts the preferred mask and can move only the unpinned "
            "burners; the pinned responder keeps its non-preferred vCPU. This "
            "tests the affinity-preserving branch of the preferred-CPU "
            "contract, not pair consolidation."
        ),
        "requires_host_steal": True,
        "groups": [
            {
                "name": "pair",
                "weight": 400,
                "workloads": [{"kind": "pingpong", "burn_us": 20}],
            },
            {
                "name": "burn",
                "weight": 100,
                "workloads": [{"kind": "cpu", "threads": 2}],
            },
        ],
    },
}


PRIMARY_METRICS: list[dict[str, Any]] = [
    {
        "scenario": "wakeup",
        "group": "probe",
        "metric": "p999_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "EEVDF slice protection and timer restart",
        "rationale": (
            "The series bounds how long the current entity may keep the CPU "
            "when a shorter-slice entity becomes runnable. The 99.9th wakeup "
            "delay is the predeclared tail metric."
        ),
    },
    {
        "scenario": "wakeup",
        "group": "probe",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "EEVDF slice protection and timer restart",
        "rationale": "The same wakeup mechanism, at the 99th percentile.",
    },
    {
        "scenario": "wakeup",
        "group": "load",
        "metric": "iterations_per_s",
        "direction": "higher",
        "expected": "equivalent",
        "feature": "EEVDF throughput",
        "rationale": (
            "A latency improvement must not be bought with a throughput loss. "
            "The null band is the same 5% used by the sibling series."
        ),
    },
    {
        "scenario": "slice",
        "group": "burst",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "EEVDF slice protection",
        "rationale": (
            "A short burst is the entity that the protection changes target; "
            "its 99th start delay should fall if the bound is effective."
        ),
    },
    {
        "scenario": "fairness",
        "group": "fair",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "EEVDF candidate selection",
        "rationale": (
            "Keeping the best equal-slice candidate on wakeup should not "
            "increase the tail; this checks the fix does not regress it."
        ),
    },
    {
        "scenario": "fairness",
        "group": "fair",
        "metric": "thread_cv",
        "direction": "lower",
        "expected": "improve",
        "feature": "EEVDF candidate selection",
        "rationale": (
            "Four equal-weight threads should complete a similar number of "
            "periods. The coefficient of variation measures that fairness."
        ),
    },
    {
        "scenario": "quota",
        "group": "limited",
        "metric": "delivered_cpu_pct",
        "direction": "equivalent",
        "expected": "equivalent",
        "feature": "CFS bandwidth (commits 21 and 63)",
        "rationale": (
            "The custom kernel restores the queueing branch inside the same "
            "series. It must not change the quota contract or grant extra CPU."
        ),
    },
    {
        "scenario": "quota",
        "group": "limited",
        "metric": "throttled_period_pct",
        "direction": "equivalent",
        "expected": "equivalent",
        "feature": "CFS bandwidth (commits 21 and 63)",
        "rationale": (
            "The limited group exhausts its 0.5 CPU cap in both kernels. A "
            "different throttle ratio would mean the series changed CFS "
            "bandwidth enforcement."
        ),
    },
    {
        "scenario": "quota",
        "group": "limited",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "equivalent",
        "feature": "CFS bandwidth tail",
        "rationale": (
            "The timer probe inside the capped group is a descriptive check. "
            "The series is not expected to remove quota-induced delay."
        ),
    },
    {
        "scenario": "churn",
        "group": "churn",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "scheduler constant overhead",
        "rationale": (
            "The augmented-rbtree and static-key changes reduce fixed work per "
            "enqueue. Short-lived task completion is the closest userspace "
            "proxy for that cost."
        ),
    },
    {
        "scenario": "churn",
        "group": "churn",
        "metric": "tasks_per_s",
        "direction": "higher",
        "expected": "equivalent",
        "feature": "scheduler constant overhead",
        "rationale": (
            "The series does not change the algorithmic complexity, so the "
            "completion rate is a null control for a large throughput gain."
        ),
    },
    {
        "scenario": "steal",
        "group": "pair",
        "metric": "p99_us",
        "direction": "lower",
        "expected": "improve",
        "feature": "steal governor",
        "contrast": "governor",
        "rationale": (
            "Moving the unpinned burners onto the preferred CPU can free the "
            "pinned responder's vCPU. The pair itself cannot migrate, so this "
            "is not a pair-consolidation effect."
        ),
    },
    {
        "scenario": "steal",
        "group": "pair",
        "metric": "round_trips_per_s",
        "direction": "higher",
        "expected": "improve",
        "feature": "steal governor",
        "contrast": "governor",
        "rationale": "The pair's completed round trips are the throughput view.",
    },
    {
        "scenario": "steal",
        "group": "burn",
        "metric": "iterations_per_s",
        "direction": "higher",
        "expected": "regress",
        "feature": "steal governor trade-off",
        "contrast": "governor",
        "rationale": (
            "The unpinned independent burners lose useful parallelism when "
            "the governor consolidates them onto the preferred CPU. A "
            "measured regression is the expected trade-off, not a defect in "
            "the series."
        ),
    },
]


DESCRIPTIVE_METRICS: list[dict[str, Any]] = [
    {
        "scenario": "wakeup",
        "group": "probe",
        "metric": "max_us",
        "direction": "lower",
        "feature": "EEVDF tail",
        "rationale": "A single worst-case sample is reported but never decided.",
    },
    {
        "scenario": "slice",
        "group": "burst",
        "metric": "missed_periods",
        "direction": "lower",
        "feature": "EEVDF slice protection",
        "rationale": (
            "A missed period is a stronger failure than a late start, but the "
            "count is often zero on a short window, so it is reported without "
            "a ratio verdict."
        ),
    },
    {
        "scenario": "slice",
        "group": "burst",
        "metric": "missed_period_rate_pct",
        "direction": "lower",
        "feature": "EEVDF slice protection",
        "rationale": "The rate normalises for the number of periods.",
    },
    {
        "scenario": "quota",
        "group": "open",
        "metric": "iterations_per_s",
        "direction": "higher",
        "feature": "quota neighbour",
        "rationale": "The uncapped neighbour shows the cost of the capped group.",
    },
    {
        "scenario": "steal",
        "group": "pair",
        "metric": "p999_us",
        "direction": "lower",
        "feature": "steal governor",
        "rationale": "The 99.9th percentile is descriptive on a small block count.",
    },
]


def scenario_names() -> tuple[str, ...]:
    return tuple(SCENARIOS)


def scenario_groups(scenario: str) -> list[dict[str, Any]]:
    """Return normalised cgroup specifications for a scenario."""

    spec = SCENARIOS[scenario]
    groups: list[dict[str, Any]] = []
    for group in spec["groups"]:
        normalised = {
            "name": str(group["name"]),
            "weight": int(group["weight"]),
            "cpu_max": str(group.get("cpu_max", "")),
            "workloads": [dict(workload) for workload in group["workloads"]],
        }
        groups.append(normalised)
    return groups


def primary_metrics(scenario: str | None = None) -> list[dict[str, Any]]:
    metrics = [dict(metric) for metric in PRIMARY_METRICS]
    if scenario is None:
        return metrics
    return [metric for metric in metrics if metric["scenario"] == scenario]


def contrast_for(scenario: str) -> tuple[str, str]:
    """Return the (control, treatment) arms for a scenario's contrast."""

    if scenario == "steal":
        return CONTRASTS["governor"]
    return CONTRASTS["kernel"]


def contrast_name(scenario: str) -> str:
    return "governor" if scenario == "steal" else "kernel"


def expected_for(metric: dict[str, Any]) -> str:
    return str(metric.get("expected", "descriptive"))


def direction_for(metric: dict[str, Any]) -> str:
    return str(metric.get("direction", "higher"))


def validate() -> None:
    """Fail closed on an inconsistent predeclared matrix."""

    if set(DEFAULT_SCENARIOS) - set(SCENARIOS):
        raise ValueError("DEFAULT_SCENARIOS references an unknown scenario")
    for scenario, spec in SCENARIOS.items():
        groups = scenario_groups(scenario)
        if not groups:
            raise ValueError(f"{scenario}: no groups")
        names = {group["name"] for group in groups}
        if len(names) != len(groups):
            raise ValueError(f"{scenario}: duplicate group names")
        for group in groups:
            if group["weight"] <= 0 or group["weight"] > 10000:
                raise ValueError(f"{scenario}/{group['name']}: invalid cpu.weight")
            if not group["workloads"]:
                raise ValueError(f"{scenario}/{group['name']}: no workloads")
        if spec.get("requires_host_steal") and scenario != "steal":
            raise ValueError(f"{scenario}: host-steal marker is only valid for steal")

    for metric in PRIMARY_METRICS:
        scenario = str(metric["scenario"])
        if scenario not in SCENARIOS:
            raise ValueError(f"primary metric references unknown scenario {scenario}")
        groups = {group["name"] for group in scenario_groups(scenario)}
        if str(metric["group"]) not in groups and metric["group"] != "system":
            raise ValueError(
                f"primary metric references unknown group {scenario}/{metric['group']}"
            )
        if metric["direction"] not in {"lower", "higher", "equivalent"}:
            raise ValueError(f"{scenario}: invalid direction {metric['direction']}")
        if expected_for(metric) not in {
            "improve",
            "equivalent",
            "regress",
            "descriptive",
        }:
            raise ValueError(f"{scenario}: invalid expectation {metric['expected']}")
        contrast = str(metric.get("contrast", contrast_name(scenario)))
        if contrast not in CONTRASTS:
            raise ValueError(f"{scenario}: unknown contrast {contrast}")

    if (
        any(expected_for(metric) == "equivalent" for metric in primary_metrics("quota"))
        is False
    ):
        raise ValueError("quota must keep at least one equivalence metric")


validate()
