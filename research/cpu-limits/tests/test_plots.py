"""Tests for deterministic CPU-limits SVG figures."""

from __future__ import annotations

import xml.etree.ElementTree as element_tree
from pathlib import Path

from analysis.plots import render_figures


def sample_result() -> dict[str, object]:
    return {
        "cells": [
            {
                "block_id": "1",
                "cell_id": "cpu-<&",
                "workload": "cpu-bound",
                "policy": "fit",
                "condition": "solo",
                "throughput": 10.0,
                "cpu_usage_seconds": 4.0,
                "throttled_period_ratio": 0.2,
                "throttled_time_seconds": 0.5,
                "cgroup_parameters": {"cpu.max.quota": "100000"},
            }
        ],
        "contrasts": [],
    }


def test_rendered_svgs_are_valid_escaped_and_deterministic(tmp_path: Path) -> None:
    result = sample_result()
    first = tmp_path / "first"
    second = tmp_path / "second"

    names = render_figures(result, first)
    render_figures(result, second)

    assert "performance-heatmap.svg" in names
    assert set(names) >= {
        "cpu-quota-scatter.svg",
        "throttling-bars.svg",
        "latency-cdf.svg",
        "runqueue-histogram.svg",
        "timeline.svg",
        "cgroup-parameter-map.svg",
    }
    for name in names:
        first_bytes = (first / name).read_bytes()
        assert first_bytes == (second / name).read_bytes()
        element_tree.fromstring(first_bytes)
    assert b"cpu-&lt;&amp;" in (first / "cgroup-parameter-map.svg").read_bytes()


def test_empty_result_creates_explicit_not_measured_figures(tmp_path: Path) -> None:
    names = render_figures({"cells": [], "contrasts": []}, tmp_path)

    assert names
    for name in names:
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert "NOT MEASURED" in text
        element_tree.fromstring(text)
