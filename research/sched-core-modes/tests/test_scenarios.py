"""Contract tests for the workload matrix and the share model."""

from __future__ import annotations

import pytest
from scenarios import (
    MODES,
    SCENARIOS,
    allowed_modes,
    effective_weights,
    expected_per_task_shares,
    expected_shares,
    mode_multiplier,
    parse_mode_file,
    scenario_names,
    scenario_units,
    thread_amplification,
)


def test_modes_are_the_five_supported_values() -> None:
    assert MODES == ("up", "smp", "concur", "max", "tasks")
    assert all(allowed_modes(mode) for mode in MODES)
    assert not allowed_modes("fifo")


def test_scenarios_have_positive_weights_threads_and_descriptions() -> None:
    assert scenario_names() == [
        "weighted_threads",
        "qos_nested",
        "quota",
        "latency",
    ]
    for name, scenario in SCENARIOS.items():
        assert scenario["description"], name
        for unit in scenario_units(name):
            assert int(unit["weight"]) > 0
            assert int(unit["threads"]) > 0


def test_weighted_threads_units_are_inverse_pairs() -> None:
    units = scenario_units("weighted_threads")
    assert [(unit["name"], unit["weight"], unit["threads"]) for unit in units] == [
        ("w400-t1", 400, 1),
        ("w200-t2", 200, 2),
        ("w100-t4", 100, 4),
    ]


def test_nested_units_are_the_qos_parents() -> None:
    units = scenario_units("qos_nested")
    assert [(unit["name"], unit["weight"], unit["threads"]) for unit in units] == [
        ("qos-guaranteed", 100, 1),
        ("qos-burstable", 100, 4),
    ]
    assert all(unit["kind"] == "parent" for unit in units)


def test_mode_multipliers_follow_the_documented_semantics() -> None:
    assert mode_multiplier("up", 4, 2) == 1
    assert mode_multiplier("smp", 4, 2) == 1
    assert mode_multiplier("concur", 4, 2) == 2
    assert mode_multiplier("concur", 1, 2) == 1
    assert mode_multiplier("max", 1, 2) == 2
    assert mode_multiplier("tasks", 4, 2) == 4


def test_effective_weights_separate_every_mode() -> None:
    weights = [400, 200, 100]
    threads = [1, 2, 4]
    assert effective_weights("up", weights, threads, 2) == [400.0, 200.0, 100.0]
    assert effective_weights("smp", weights, threads, 2) == [400.0, 200.0, 100.0]
    assert effective_weights("concur", weights, threads, 2) == [400.0, 400.0, 200.0]
    assert effective_weights("max", weights, threads, 2) == [800.0, 400.0, 200.0]
    assert effective_weights("tasks", weights, threads, 2) == [400.0, 400.0, 400.0]


def test_expected_shares_sum_to_one_and_match_the_model() -> None:
    cases = {
        "up": {"w400-t1": 4 / 7, "w200-t2": 2 / 7, "w100-t4": 1 / 7},
        "smp": {"w400-t1": 4 / 7, "w200-t2": 2 / 7, "w100-t4": 1 / 7},
        "concur": {"w400-t1": 0.4, "w200-t2": 0.4, "w100-t4": 0.2},
        "max": {"w400-t1": 4 / 7, "w200-t2": 2 / 7, "w100-t4": 1 / 7},
        "tasks": {"w400-t1": 1 / 3, "w200-t2": 1 / 3, "w100-t4": 1 / 3},
    }
    for mode, expected in cases.items():
        shares = expected_shares("weighted_threads", mode, 2)
        assert shares is not None
        assert set(shares) == set(expected)
        assert sum(shares.values()) == pytest.approx(1.0)
        for name, value in expected.items():
            assert shares[name] == pytest.approx(value)


def test_expected_shares_for_nested_qos_amplify_burstable() -> None:
    assert expected_shares("qos_nested", "smp", 2) == pytest.approx(
        {"qos-guaranteed": 0.5, "qos-burstable": 0.5}
    )
    assert expected_shares("qos_nested", "concur", 2) == pytest.approx(
        {"qos-guaranteed": 1 / 3, "qos-burstable": 2 / 3}
    )
    assert expected_shares("qos_nested", "tasks", 2) == pytest.approx(
        {"qos-guaranteed": 0.2, "qos-burstable": 0.8}
    )


def test_quota_has_no_mode_specific_expectation() -> None:
    assert expected_shares("quota", "smp", 2) is None
    assert expected_per_task_shares("quota", "smp", 2) is None


def test_per_task_shares_show_the_smp_to_tasks_shift() -> None:
    smp = expected_per_task_shares("weighted_threads", "smp", 2)
    concur = expected_per_task_shares("weighted_threads", "concur", 2)
    tasks = expected_per_task_shares("weighted_threads", "tasks", 2)
    assert smp is not None and concur is not None and tasks is not None
    assert smp["w400-t1"] > smp["w200-t2"] > smp["w100-t4"]
    assert concur["w400-t1"] > concur["w200-t2"] > concur["w100-t4"]
    assert tasks["w400-t1"] > tasks["w200-t2"] > tasks["w100-t4"]
    assert tasks["w100-t4"] / tasks["w400-t1"] == pytest.approx(0.25)
    assert concur["w100-t4"] / concur["w400-t1"] == pytest.approx(0.125)


def test_thread_amplification_is_one_for_up_and_smp() -> None:
    assert thread_amplification("weighted_threads", "up", 2) == {
        "w400-t1": 1.0,
        "w200-t2": 1.0,
        "w100-t4": 1.0,
    }
    assert thread_amplification("weighted_threads", "concur", 2) == {
        "w400-t1": 1.0,
        "w200-t2": 2.0,
        "w100-t4": 2.0,
    }
    assert thread_amplification("weighted_threads", "max", 2) == {
        "w400-t1": 2.0,
        "w200-t2": 2.0,
        "w100-t4": 2.0,
    }
    assert thread_amplification("weighted_threads", "tasks", 2) == {
        "w400-t1": 1.0,
        "w200-t2": 2.0,
        "w100-t4": 4.0,
    }


def test_parse_mode_file_reads_the_parenthesised_mode() -> None:
    assert parse_mode_file("up smp (concur) max tasks \n") == "concur"
    assert parse_mode_file("(up) smp concur max tasks") == "up"
    with pytest.raises(ValueError):
        parse_mode_file("up smp concur max tasks")
