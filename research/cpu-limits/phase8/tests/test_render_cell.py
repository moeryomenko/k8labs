"""Tests for the cell-manifest renderer.

The renderer is the gate between the experiment design and the cluster, so
these tests target the cases that would otherwise produce a silently wrong
measurement: unsubstituted placeholders, impossible resource values, and a
request above its limit.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MODULE_PATH = Path(__file__).resolve().parents[1] / "manifests" / "render-cell.py"


def load_module():
    spec = importlib.util.spec_from_file_location("render_cell", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_cell = load_module()

BASE_VALUES = {
    "CELL": "cpu-500m-balanced",
    "MIX": "balanced",
    "CPU_ITERATIONS": "20000",
    "POSTGRES_POOL_SIZE": "16",
    "REDIS_POOL_SIZE": "16",
    "TRACE_SAMPLE_RATIO": "0.1",
    "CPU_REQUEST": "250m",
    "CPU_LIMIT": "500m",
    "SIDECAR_CPU_REQUEST": "50m",
    "SIDECAR_CPU_LIMIT": "100m",
}


def test_renders_valid_yaml_with_both_containers():
    rendered = render_cell.render(dict(BASE_VALUES))
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]

    kinds = {doc["kind"] for doc in documents}
    assert kinds == {"Deployment", "Service"}

    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == ["app", "sidecar"]

    app = containers[0]
    assert app["resources"]["limits"]["cpu"] == "500m"
    assert app["resources"]["requests"]["cpu"] == "250m"


def test_no_placeholders_survive():
    rendered = render_cell.render(dict(BASE_VALUES))
    assert "{{" not in rendered


def test_cell_name_is_the_selector():
    rendered = render_cell.render(dict(BASE_VALUES))
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    selector = deployment["spec"]["selector"]["matchLabels"]
    assert selector["cpu-limit-autoscale/cell"] == BASE_VALUES["CELL"]


def test_request_above_limit_is_rejected():
    values = dict(BASE_VALUES, CPU_REQUEST="1000m", CPU_LIMIT="500m")
    with pytest.raises(render_cell.RenderError, match="exceeds CPU_LIMIT"):
        render_cell.render(values)


def test_sidecar_request_above_limit_is_rejected():
    values = dict(BASE_VALUES, SIDECAR_CPU_REQUEST="200m", SIDECAR_CPU_LIMIT="100m")
    with pytest.raises(render_cell.RenderError, match="SIDECAR_CPU_REQUEST"):
        render_cell.render(values)


def test_unknown_mix_is_rejected():
    values = dict(BASE_VALUES, MIX="overnight")
    with pytest.raises(render_cell.RenderError, match="MIX has invalid value"):
        render_cell.render(values)


def test_missing_value_is_rejected():
    values = dict(BASE_VALUES)
    del values["CPU_LIMIT"]
    with pytest.raises(render_cell.RenderError, match="missing placeholder"):
        render_cell.render(values)


def test_unknown_placeholder_is_rejected():
    values = dict(BASE_VALUES, CPU_LIMIT_EXTRA="1")
    with pytest.raises(render_cell.RenderError, match="unknown placeholder"):
        render_cell.render(values)


def test_cpu_quantity_accepts_cores_and_millicores():
    assert render_cell.parse_quantity_millicores("750m") == 750
    assert render_cell.parse_quantity_millicores("2") == 2000


def test_cpu_quantity_rejects_ambiguous_forms():
    # `0.5` is fractional, `500M` is not a Kubernetes suffix, and `500` is half
    # a core rather than 500 millicores. All are refused so the model cannot be
    # compared against a value whose meaning differs from the manifest.
    for value in ["0.5", "500M", "0500m", "m"]:
        with pytest.raises(render_cell.RenderError):
            render_cell.parse_quantity_millicores(value)


def test_cpu_quantity_accepts_small_millicore_values():
    # `2m` is genuinely two millicores; refusing it would be wrong.
    assert render_cell.parse_quantity_millicores("2m") == 2


def test_cli_writes_output(tmp_path):
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps(BASE_VALUES))
    output_path = tmp_path / "cell.yaml"

    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--values",
            str(values_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "cpu-limit-bench" in output_path.read_text()


def test_cli_fails_on_invalid_values(tmp_path):
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps(dict(BASE_VALUES, CPU_LIMIT="10m")))

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--values", str(values_path)],
        capture_output=True,
        text=True,
    )
    # A limit below the request must fail loudly rather than emit a manifest the
    # API server would reject mid-campaign.
    assert result.returncode == 1
    assert "exceeds CPU_LIMIT" in result.stderr
