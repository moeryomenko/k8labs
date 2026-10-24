"""Workload matrix and analytical share model for the cgroup-mode study.

The model follows the sched-core-2026-08-17 analysis. The kernel computes a
per-CPU group fraction ``F_g_n`` and gives the group entity the weight
``cpu.weight * F_g_n``:

- ``up``:     ``F_g_n = 1`` -- the group keeps its full weight on every CPU;
- ``smp``:    ``F_g_n`` distributes the group weight across the CPUs its tasks
  run on, so the group total is ``weight``;
- ``concur``: ``min(tasks, cpus) * weight``;
- ``max``:    ``cpus * weight`` (clipped at nice -20);
- ``tasks``:  ``tasks * weight``.

The share model below is deliberately the *idealized full-concurrency* model:
it assumes every group is runnable on every CPU. It is a reference for the
semantics of each mode, not a prediction of an unbalanced placement. The
measurement reports both the measured shares and the model delta, so a
placement effect shows up as a deviation instead of being hidden.

Expected shares are computed at the level the mode scales: the direct test
groups for flat scenarios, and the QoS parent for the nested scenario.
"""

from __future__ import annotations

MODES = ("up", "smp", "concur", "max", "tasks")

# The lab workers have two vCPUs. The runner reports the live value in the
# captured metadata; this constant is the default for the model and tests.
DEFAULT_CPUS = 2

MODE_SUMMARY = {
    "up": "full group weight on every CPU (F_g_n = 1)",
    "smp": "group weight distributed across the CPUs its tasks use",
    "concur": "min(tasks, cpus) x weight (per active CPU)",
    "max": "cpus x weight, clipped at nice -20",
    "tasks": "tasks x weight (weight per runnable task)",
}

SCENARIOS = {
    "weighted_threads": {
        "kind": "flat",
        "description": (
            "Three groups with deliberately inverse weight/thread pairs: "
            "400/1t, 200/2t, 100/4t. This separates every mode."
        ),
        "groups": [
            {"name": "w400-t1", "weight": 400, "threads": 1},
            {"name": "w200-t2", "weight": 200, "threads": 2},
            {"name": "w100-t4", "weight": 100, "threads": 4},
        ],
    },
    "qos_nested": {
        "kind": "nested",
        "description": (
            "Guaranteed-shaped parent (100/1t) versus a Burstable-shaped "
            "parent (100 with two 100/2t pod children). Shows QoS-level "
            "amplification under concur/tasks."
        ),
        "parents": [
            {
                "name": "qos-guaranteed",
                "weight": 100,
                "threads": 1,
                "children": [],
            },
            {
                "name": "qos-burstable",
                "weight": 100,
                "threads": 4,
                "children": [
                    {"name": "pod-a", "weight": 100, "threads": 2},
                    {"name": "pod-b", "weight": 100, "threads": 2},
                ],
            },
        ],
    },
    "quota": {
        "kind": "flat",
        "description": (
            "Equal-weight 100/2t groups; one has cpu.max=50000 100000 "
            "(0.5 CPU), the other is unconstrained. Quota enforcement is "
            "independent of cgroup_mode."
        ),
        "groups": [
            {
                "name": "limited",
                "weight": 100,
                "threads": 2,
                "cpu_max": "50000 100000",
            },
            {"name": "open", "weight": 100, "threads": 2},
        ],
    },
    "latency": {
        "kind": "flat",
        "description": (
            "Load group 400/2t plus a 1 ms wakeup probe in a 100/1t group. "
            "Measures p50/p99 wakeup delay and the probe's CPU share."
        ),
        "groups": [
            {"name": "load", "weight": 400, "threads": 2, "workload": "cpu"},
            {"name": "probe", "weight": 100, "threads": 1, "workload": "latency"},
        ],
    },
}


def scenario_names() -> list[str]:
    return list(SCENARIOS)


def scenario_units(scenario_name: str) -> list[dict[str, object]]:
    """Return the units at the level the mode scales.

    Flat scenarios expose their direct groups. The nested scenario exposes the
    QoS parents, because that is the level at which the Kubernetes QoS class
    competes; the pod children are measured separately by the runner.
    """

    scenario = SCENARIOS[scenario_name]
    if scenario["kind"] == "nested":
        return [
            {
                "name": parent["name"],
                "weight": int(parent["weight"]),
                "threads": int(parent["threads"]),
                "kind": "parent",
                "parent": "",
                "cpu_max": "",
                "workload": "cpu",
            }
            for parent in scenario["parents"]
        ]

    return [
        {
            "name": group["name"],
            "weight": int(group["weight"]),
            "threads": int(group["threads"]),
            "kind": "group",
            "parent": "",
            "cpu_max": str(group.get("cpu_max", "")),
            "workload": str(group.get("workload", "cpu")),
        }
        for group in scenario["groups"]
    ]


def mode_multiplier(mode: str, threads: int, cpus: int = DEFAULT_CPUS) -> int:
    """Return the documented per-group weight multiplier for one mode."""

    if mode == "up":
        # F_g_n = 1: the group keeps its full weight on every CPU, so the
        # idealized group total is the same as smp. The difference is visible
        # when a group cannot occupy every CPU.
        return 1
    if mode == "smp":
        return 1
    if mode == "concur":
        return min(threads, cpus)
    if mode == "max":
        return cpus
    if mode == "tasks":
        return threads
    raise ValueError(f"unknown cgroup mode: {mode}")


def effective_weights(
    mode: str, weights: list[int], threads: list[int], cpus: int
) -> list[float]:
    """Return the idealized aggregate group weight for one mode."""

    return [
        float(mode_multiplier(mode, thread_count, cpus) * weight)
        for weight, thread_count in zip(weights, threads, strict=True)
    ]


def expected_shares(
    scenario_name: str,
    mode: str,
    cpus: int = DEFAULT_CPUS,
) -> dict[str, float] | None:
    """Expected CPU-time shares for a scenario, or None when quota caps them.

    For the quota scenario the cap, not the mode, determines the split, so
    there is no mode-specific expectation.
    """

    if scenario_name == "quota":
        return None

    units = scenario_units(scenario_name)
    names = [str(unit["name"]) for unit in units]
    weights = [int(unit["weight"]) for unit in units]
    threads = [int(unit["threads"]) for unit in units]
    effective = effective_weights(mode, weights, threads, cpus)
    total = sum(effective)
    if total <= 0:
        return None
    return {name: value / total for name, value in zip(names, effective, strict=True)}


def expected_per_task_shares(
    scenario_name: str,
    mode: str,
    cpus: int = DEFAULT_CPUS,
) -> dict[str, float] | None:
    """Expected CPU-time share of one thread in each unit."""

    shares = expected_shares(scenario_name, mode, cpus)
    if shares is None:
        return None
    units = scenario_units(scenario_name)
    return {
        str(unit["name"]): shares[str(unit["name"])] / max(1, int(unit["threads"]))
        for unit in units
    }


def thread_amplification(
    scenario_name: str,
    mode: str,
    cpus: int = DEFAULT_CPUS,
) -> dict[str, float]:
    """Per-unit multiplier relative to ``smp`` (1.0 means no amplification)."""

    units = scenario_units(scenario_name)
    return {
        str(unit["name"]): float(mode_multiplier(mode, int(unit["threads"]), cpus))
        for unit in units
    }


def allowed_modes(mode: str) -> bool:
    return mode in MODES


def parse_mode_file(text: str) -> str:
    """Read the current mode from the debugfs line, e.g. ``up smp (concur)``."""

    for token in text.split():
        if token.startswith("(") and token.endswith(")"):
            return token[1:-1]
    raise ValueError(f"no active cgroup mode in {text!r}")
