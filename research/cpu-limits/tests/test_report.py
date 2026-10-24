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


def test_pilot_report_uses_descriptive_labels_without_confirmatory_decisions() -> None:
    report = compose_report(
        {
            "phase": "pilot",
            "cells": [{"block_id": "1", "workload": "cpu-bound"}],
            "contrasts": [
                {
                    "family": "limit_effect",
                    "workload": "cpu-bound",
                    "metric": "throughput",
                    "decision": "pilot descriptive",
                    "estimate": 1.1,
                    "ci_low": 1.0,
                    "ci_high": 1.2,
                    "paired_blocks": ["1"],
                }
            ],
        },
        summary_markdown="# Summary\n",
    )

    assert "# CPU-limits pilot descriptive results" in report
    assert "pilot descriptive, not confirmatory" in report
    assert "| family | workload | metric | decision |" not in report
    assert "complete confirmatory rows" not in report


def test_pilot_trace_result_is_labelled_descriptive() -> None:
    report = compose_report(
        {
            "phase": "pilot",
            "cells": [{"block_id": "1", "workload": "cpu-bound"}],
            "contrasts": [],
            "trace_rows": [
                {
                    "cell_id": "solo-cpu-bound-fit",
                    "event": "runqueue_delay",
                    "latency_ns": 3_000,
                }
            ],
        },
        summary_markdown="# Summary\n",
    )

    assert "Trace evidence is pilot descriptive" in report
    assert "Run-queue delay p50/p99: 0.003 / 0.003 ms" in report
