"""Contract tests for the paired delta analysis."""

from __future__ import annotations

import json
import math
from pathlib import Path

import analyze
import pytest
from scenarios import CONTRASTS


def test_bootstrap_returns_the_point_for_constant_logs() -> None:
    low, high = analyze.bootstrap_ci([math.log(0.9)] * 5, 500, 7)
    assert low == pytest.approx(0.9)
    assert high == pytest.approx(0.9)


def test_classify_lower_and_higher_directions() -> None:
    assert (
        analyze.classify(
            direction="lower",
            expected="improve",
            point=1.10,
            ci_low=1.05,
            ci_high=1.15,
            blocks=5,
        )
        == "improved"
    )
    assert (
        analyze.classify(
            direction="lower",
            expected="improve",
            point=0.90,
            ci_low=0.85,
            ci_high=0.95,
            blocks=5,
        )
        == "regressed"
    )
    assert (
        analyze.classify(
            direction="higher",
            expected="improve",
            point=1.10,
            ci_low=1.05,
            ci_high=1.15,
            blocks=5,
        )
        == "improved"
    )
    assert (
        analyze.classify(
            direction="higher",
            expected="regress",
            point=0.80,
            ci_low=0.75,
            ci_high=0.85,
            blocks=5,
        )
        == "regressed"
    )


def test_classify_equivalence_requires_the_whole_interval_inside_the_band() -> None:
    assert (
        analyze.classify(
            direction="equivalent",
            expected="equivalent",
            point=1.0,
            ci_low=0.98,
            ci_high=1.02,
            blocks=5,
        )
        == "equivalent"
    )
    assert (
        analyze.classify(
            direction="equivalent",
            expected="equivalent",
            point=1.0,
            ci_low=0.90,
            ci_high=1.10,
            blocks=5,
        )
        == "inconclusive"
    )
    assert (
        analyze.classify(
            direction="equivalent",
            expected="equivalent",
            point=1.10,
            ci_low=1.06,
            ci_high=1.14,
            blocks=5,
        )
        == "different"
    )


def test_fewer_than_three_blocks_never_gets_a_verdict() -> None:
    assert (
        analyze.classify(
            direction="lower",
            expected="improve",
            point=1.50,
            ci_low=1.40,
            ci_high=1.60,
            blocks=2,
        )
        == "inconclusive"
    )


def test_status_matches_the_declared_expectation() -> None:
    assert analyze.status_for("improve", "improved", 5) == "PASS"
    assert analyze.status_for("improve", "equivalent", 5) == "FAIL"
    assert analyze.status_for("equivalent", "equivalent", 5) == "PASS"
    assert analyze.status_for("equivalent", "different", 5) == "FAIL"
    assert analyze.status_for("regress", "regressed", 5) == "PASS"
    assert analyze.status_for("regress", "improved", 5) == "FAIL"
    assert analyze.status_for("descriptive", "descriptive", 5) == "descriptive"


def _write_block(
    root: Path,
    block: str,
    arm: str,
    rows: list[dict[str, object]],
    *,
    node: str | None = None,
    profile: str = "full",
) -> None:
    directory = root / f"block-{block}" / arm
    directory.mkdir(parents=True)
    run = {
        "run_id": "synthetic",
        "arm": arm,
        "block": block,
        "node": node or f"{arm}-node",
        "kernel": "7.3.0-65.fc46.x86_64" if arm == "stock" else "7.3.0-k8labs",
        "profile": profile,
    }
    (directory / "run.json").write_text(json.dumps(run), encoding="utf-8")
    with (directory / "matrix.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _synthetic_campaign(root: Path) -> None:
    for block in ("01", "02", "03"):
        for arm in ("stock", "custom"):
            factor = 0.90 if arm == "custom" else 1.0
            row = {
                "block": block,
                "arm": arm,
                "scenario": "wakeup",
                "group": "probe",
                "kind": "group",
                "rep": 1,
                "p999_us": 1000.0 * factor,
                "p99_us": 500.0 * factor,
                "max_us": 2000.0 * factor,
            }
            quota = {
                "block": block,
                "arm": arm,
                "scenario": "quota",
                "group": "limited",
                "kind": "group",
                "rep": 1,
                "delivered_cpu_pct": 50.0,
                "throttled_period_pct": 100.0,
                "p99_us": 5000.0,
            }
            system = {
                "block": block,
                "arm": arm,
                "scenario": "wakeup",
                "group": "system",
                "kind": "system",
                "rep": 1,
                "steal_pct_total": 0.1,
                "preferred_min": 2,
                "preferred_changes": 0,
                "ctxt_per_s": 3000.0,
            }
            _write_block(root, block, arm, [row, quota, system])
    (root / "campaign.json").write_text(
        json.dumps({"run_id": "synthetic", "blocks": 3}), encoding="utf-8"
    )


def test_main_writes_summary_and_marks_the_declared_improvement(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    _synthetic_campaign(run)
    assert analyze.main(["--run-dir", str(run), "--resamples", "500"]) == 0
    summary = (run / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert "wakeup | p999_us" in summary
    assert "improved" in summary
    assert "PASS" in summary
    csv = (run / "analysis" / "summary.csv").read_text(encoding="utf-8")
    assert "wakeup,probe,p999_us" in csv


def test_cross_node_campaign_is_flagged(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _synthetic_campaign(run)
    # Rename the two arms' nodes so the warning fires.
    for block in ("01", "02", "03"):
        stock = run / f"block-{block}" / "stock" / "run.json"
        value = json.loads(stock.read_text(encoding="utf-8"))
        value["node"] = "node-a"
        stock.write_text(json.dumps(value), encoding="utf-8")
        custom = run / f"block-{block}" / "custom" / "run.json"
        value = json.loads(custom.read_text(encoding="utf-8"))
        value["node"] = "node-b"
        custom.write_text(json.dumps(value), encoding="utf-8")
        for arm, node in (("stock", "node-a"), ("custom", "node-b")):
            matrix = run / f"block-{block}" / arm / "matrix.jsonl"
            rows = [json.loads(line) for line in matrix.read_text().splitlines()]
            for row in rows:
                row["node"] = node
            matrix.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
    analyze.main(["--run-dir", str(run), "--resamples", "200"])
    summary = (run / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert "Cross-node campaign" in summary


def test_missing_contrast_arm_is_inconclusive_not_a_crash(tmp_path: Path) -> None:
    run = tmp_path / "run"
    for block in ("01", "02", "03"):
        _write_block(
            run,
            block,
            "stock",
            [
                {
                    "block": block,
                    "arm": "stock",
                    "scenario": "wakeup",
                    "group": "probe",
                    "kind": "group",
                    "rep": 1,
                    "p999_us": 1000.0,
                }
            ],
        )
    analyze.main(["--run-dir", str(run), "--resamples", "200"])
    summary = (run / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert "missing" in summary
    assert "INCONCLUSIVE" in summary


def test_governor_contrast_uses_the_custom_variants(tmp_path: Path) -> None:
    root = tmp_path / "run"
    for block in ("01", "02", "03"):
        for arm, ratio in (("custom-nogov", 1.0), ("custom-gov", 0.80)):
            _write_block(
                root,
                block,
                arm,
                [
                    {
                        "block": block,
                        "arm": arm,
                        "scenario": "steal",
                        "group": "pair",
                        "kind": "group",
                        "rep": 1,
                        "p99_us": 1000.0 * ratio,
                        "round_trips_per_s": 1000.0 / ratio,
                    },
                    {
                        "block": block,
                        "arm": arm,
                        "scenario": "steal",
                        "group": "burn",
                        "kind": "group",
                        "rep": 1,
                        "iterations_per_s": 1000.0 if arm == "custom-nogov" else 700.0,
                    },
                    {
                        "block": block,
                        "arm": arm,
                        "scenario": "steal",
                        "group": "system",
                        "kind": "system",
                        "rep": 1,
                        "steal_pct_total": 50.0,
                        "preferred_min": 1,
                        "preferred_changes": 2,
                        "ctxt_per_s": 4000.0,
                        "pingpong_non_preferred_migrations": 10,
                    },
                ],
            )
    analyze.main(["--run-dir", str(root), "--resamples", "200"])
    summary = (root / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert CONTRASTS["governor"][0] in summary
    assert "custom-gov" in summary
    assert "improved" in summary
    assert "regressed" in summary


def test_system_table_does_not_report_an_unsupported_preferred_mask() -> None:
    rows = [
        {
            "arm": "stock",
            "scenario": "wakeup",
            "block": "01",
            "preferred_first": "",
            "preferred_min": 0,
            "preferred_changes": 0,
            "steal_pct_total": 4.2,
            "steal_pct_busy": 4.2,
            "ctxt_per_s": 3000.0,
        },
        {
            "arm": "custom",
            "scenario": "wakeup",
            "block": "01",
            "preferred_first": "0-1",
            "preferred_min": 2,
            "preferred_changes": 0,
            "steal_pct_total": 4.1,
            "steal_pct_busy": 4.1,
            "ctxt_per_s": 3100.0,
        },
    ]
    table = analyze.system_table(rows, ["01"])
    stock = next(row for row in table if row["arm"] == "stock")
    custom = next(row for row in table if row["arm"] == "custom")
    assert math.isnan(stock["preferred_min"])
    assert custom["preferred_min"] == 2.0


def test_pilot_profile_is_labelled_and_never_gets_a_verdict(tmp_path: Path) -> None:
    run = tmp_path / "pilot"
    for block in ("01", "02", "03"):
        for arm in ("stock", "custom"):
            _write_block(
                run,
                block,
                arm,
                [
                    {
                        "block": block,
                        "arm": arm,
                        "scenario": "wakeup",
                        "group": "probe",
                        "kind": "group",
                        "rep": 1,
                        "p999_us": 1000.0 if arm == "stock" else 800.0,
                    }
                ],
                profile="pilot",
            )
    analyze.main(["--run-dir", str(run), "--resamples", "200"])
    summary = (run / "analysis" / "summary.md").read_text(encoding="utf-8")
    assert "Pilot profile" in summary
    assert "| wakeup | p999_us | 1000.00 | 800.00 | 0.800x |" in summary
    assert "| inconclusive | INCONCLUSIVE |" in summary
