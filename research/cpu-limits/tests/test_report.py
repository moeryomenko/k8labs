"""Tests for fail-closed CPU-limits report output."""

from __future__ import annotations

from analysis.report import compose_report


def test_empty_input_produces_not_measured_report_without_claims() -> None:
    report = compose_report(
        {"cells": [], "contrasts": []},
        summary_markdown="# Summary\n\nComplete cells: 0\n",
        gates_markdown="# Gates\n\n| gate | status |\n|---|---|\n| cluster | PENDING |",
    )

    assert "# CPU-limits results: NOT MEASURED" in report
    assert "GATES.md" in report
    assert "measured improvement" not in report.lower()
    assert "measured regression" not in report.lower()


def test_measured_input_lists_contrasts_and_escapes_table_values() -> None:
    report = compose_report(
        {
            "cells": [{"block_id": "1", "workload": "cpu|bound"}],
            "contrasts": [
                {
                    "family": "limit_effect",
                    "workload": "cpu|bound",
                    "metric": "throughput",
                    "decision": "inconclusive",
                    "estimate": 1.1,
                    "ci_low": 1.0,
                    "ci_high": 1.2,
                    "paired_blocks": ["1"],
                }
            ],
        },
        summary_markdown="# Summary\n",
        sample_size={"scenarios": [{"required_blocks": 3}]},
    )

    assert "# CPU-limits results" in report
    assert r"cpu\|bound" in report
    assert "1.100000" in report
    assert "## Limitations" in report
