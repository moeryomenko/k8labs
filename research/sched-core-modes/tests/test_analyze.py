"""Contract tests for the matrix analyzer using a synthetic, exact matrix."""

from __future__ import annotations

import json
from pathlib import Path

import analyze
import pytest
from scenarios import MODES, SCENARIOS, expected_shares, scenario_units

CPUS = 2
DURATION = 10.0
TOTAL_USEC = CPUS * int(DURATION * 1_000_000)


def _row(
    *,
    scenario: str,
    mode: str,
    group: str,
    kind: str,
    parent: str,
    weight: int,
    threads: int,
    usage_usec: int,
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": "test-run",
        "node": "worker-test",
        "kernel": "7.3.0-k8labs",
        "cpus": CPUS,
        "mode": mode,
        "mode_order": ",".join(MODES),
        "scenario": scenario,
        "rep": 1,
        "group": group,
        "kind": kind,
        "parent": parent,
        "weight": weight,
        "threads": threads,
        "cpu_max": "",
        "cpu_weight_read": str(weight),
        "usage_usec": usage_usec,
        "user_usec": usage_usec,
        "system_usec": 0,
        "nr_periods": 100,
        "nr_throttled": 0,
        "throttled_usec": 0,
        "ctxt": 10_000,
        "elapsed_s": DURATION,
        "timestamp": "2026-10-24T00:00:00+00:00",
    }
    row.update(extra)
    return row


def synthetic_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for scenario in SCENARIOS:
        for mode in MODES:
            if scenario == "quota":
                shares = {"limited": 0.25, "open": 0.75}
            elif scenario == "latency":
                shares = {"load": 0.99, "probe": 0.01}
            else:
                model = expected_shares(scenario, mode, CPUS)
                assert model is not None
                shares = model

            for group, share in shares.items():
                usage = int(TOTAL_USEC * share)
                if scenario == "qos_nested":
                    rows.append(
                        _row(
                            scenario=scenario,
                            mode=mode,
                            group=group,
                            kind="parent",
                            parent="",
                            weight=100,
                            threads=1 if group == "qos-guaranteed" else 4,
                            usage_usec=usage,
                        )
                    )
                else:
                    unit = next(
                        unit
                        for unit in scenario_units(scenario)
                        if str(unit["name"]) == group
                    )
                    rows.append(
                        _row(
                            scenario=scenario,
                            mode=mode,
                            group=group,
                            kind="group",
                            parent="",
                            weight=int(unit["weight"]),
                            threads=int(unit["threads"]),
                            usage_usec=usage,
                            iterations=100_000 if group != "probe" else 0,
                            **(
                                {
                                    "p50_us": 120,
                                    "p99_us": 900,
                                    "p999_us": 1800,
                                    "max_us": 5000,
                                }
                                if group == "probe"
                                else {}
                            ),
                            **(
                                {
                                    "cpu_max": "50000 100000",
                                    "nr_periods": 100,
                                    "nr_throttled": 80,
                                    "throttled_usec": int(usage * 0.5),
                                }
                                if group == "limited"
                                else {}
                            ),
                        )
                    )

            if scenario == "qos_nested":
                burstable = int(TOTAL_USEC * shares["qos-burstable"])
                for child in ("qos-burstable-pod-a", "qos-burstable-pod-b"):
                    rows.append(
                        _row(
                            scenario=scenario,
                            mode=mode,
                            group=child,
                            kind="child",
                            parent="qos-burstable",
                            weight=100,
                            threads=2,
                            usage_usec=burstable // 2,
                            iterations=50_000,
                        )
                    )
    return rows


def synthetic_meta(
    row_list: list[dict[str, object]], **overrides: object
) -> dict[str, object]:
    meta: dict[str, object] = {
        "run_id": "test-run",
        "node": "worker-test",
        "kernel": "7.3.0-k8labs",
        "cpus": CPUS,
        "reps": 1,
        "duration_s": DURATION,
        "warmup_s": 2.0,
        "modes": list(MODES),
        "scenarios": list(SCENARIOS),
        "original_mode": "concur",
        "restored_mode": "concur",
        "rows": len(row_list),
    }
    meta.update(overrides)
    return meta


def test_analyze_builds_one_wide_row_per_scenario_mode_group() -> None:
    rows = synthetic_rows()
    wide, metrics, result = analyze.analyze(rows, synthetic_meta(rows))
    assert len(wide) == 45
    assert result["pods"]
    assert len(result["pods"]) == 10
    assert any(row["metric"] == "cpu_share_pct" for row in metrics)


def test_analyze_matches_the_model_and_relative_smp() -> None:
    rows = synthetic_rows()
    wide, _, _ = analyze.analyze(rows, synthetic_meta(rows))
    by_key = {(row["scenario"], row["mode"], row["group"]): row for row in wide}
    smp = by_key[("weighted_threads", "smp", "w400-t1")]
    assert smp["share_mean_pct"] == pytest.approx(400 / 7, abs=0.01)
    assert smp["expected_pct"] == pytest.approx(400 / 7, abs=0.01)
    assert smp["delta_pp"] == pytest.approx(0.0, abs=0.01)
    assert smp["relative_to_smp"] == pytest.approx(1.0)

    concur = by_key[("weighted_threads", "concur", "w100-t4")]
    assert concur["share_mean_pct"] == pytest.approx(20.0)
    assert concur["relative_to_smp"] == pytest.approx(1.4, abs=0.001)

    tasks = by_key[("weighted_threads", "tasks", "w100-t4")]
    assert tasks["share_mean_pct"] == pytest.approx(100 / 3, abs=0.01)


def test_analyze_aggregates_qos_pods_and_quota_throttle() -> None:
    rows = synthetic_rows()
    _, metrics, result = analyze.analyze(rows, synthetic_meta(rows))
    pods = result["pods"]
    assert {pod["child"] for pod in pods} == {
        "qos-burstable-pod-a",
        "qos-burstable-pod-b",
    }
    for pod in pods:
        assert pod["share_within_parent_pct"] == pytest.approx(50.0)

    throttle = [
        row
        for row in metrics
        if row["scenario"] == "quota"
        and row["mode"] == "smp"
        and row["metric"] == "throttled_periods_pct"
    ]
    assert throttle and throttle[0]["mean"] == pytest.approx(80.0)

    latency = [
        row
        for row in metrics
        if row["scenario"] == "latency"
        and row["mode"] == "smp"
        and row["metric"] == "wakeup_p99_us"
    ]
    assert latency and latency[0]["mean"] == pytest.approx(900.0)


def test_markdown_contains_the_scenario_sections_and_findings() -> None:
    rows = synthetic_rows()
    _, _, result = analyze.analyze(rows, synthetic_meta(rows))
    markdown = analyze.build_markdown(result)
    for heading in (
        "## weighted_threads",
        "## qos_nested",
        "## quota",
        "## latency",
        "### Wakeup latency",
        "## Findings",
    ):
        assert heading in markdown
    assert "1.40x" in markdown


def test_analyze_writes_all_outputs(tmp_path: Path) -> None:
    rows = synthetic_rows()
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(synthetic_meta(rows), indent=2) + "\n", encoding="utf-8")
    output = tmp_path / "analysis"
    assert (
        analyze.main(
            [
                "--matrix",
                str(matrix),
                "--meta",
                str(meta),
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    for name in ("shares.csv", "metrics.csv", "pods.csv", "summary.json", "summary.md"):
        assert (output / name).is_file(), name
    assert "cpu_share_pct" in (output / "metrics.csv").read_text(encoding="utf-8")


def test_truncated_matrix_is_rejected() -> None:
    rows = synthetic_rows()
    meta = synthetic_meta(rows, rows=len(rows) + 1)
    with pytest.raises(SystemExit):
        analyze.validate(rows, meta)


def test_missing_matrix_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        analyze.load_matrix(tmp_path / "missing.jsonl")
