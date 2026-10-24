"""Tests for the CPU-limits workload renderer."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from workloads.render import POLICIES, WORKLOADS, render

FIXTURE_MANIFEST = """\
apiVersion: v1
kind: Pod
metadata:
  name: __NAME__
  labels:
    run: __RUN_ID__
    workload: __WORKLOAD__
spec:
  nodeName: __NODE__
  containers:
    - name: target
      image: example.invalid/workload:latest
      __RESOURCES__
"""


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    """Write one small workload manifest for a renderer test."""

    path = tmp_path / "manifest.yaml"
    path.write_text(FIXTURE_MANIFEST, encoding="utf-8")
    return path


@pytest.mark.parametrize("workload", WORKLOADS)
@pytest.mark.parametrize("policy", POLICIES)
def test_renders_each_policy_for_each_workload(
    manifest_path: Path, workload: str, policy: str
) -> None:
    rendered = render(
        workload=workload,
        policy=policy,
        node="worker-a",
        run_id="run-17",
        fit_cpu="1000m",
        low_cpu="250m",
        manifest_path=manifest_path,
    )

    document = yaml.safe_load(rendered)
    assert document["metadata"]["labels"]["workload"] == workload
    assert document["spec"]["nodeName"] == "worker-a"
    assert "__" not in rendered


def test_best_effort_emits_an_empty_resources_mapping(manifest_path: Path) -> None:
    document = yaml.safe_load(
        render(
            workload="cpu-bound",
            policy="best-effort",
            node="worker-a",
            run_id="run-17",
            fit_cpu="2000m",
            low_cpu="500m",
            manifest_path=manifest_path,
        )
    )

    assert document["spec"]["containers"][0]["resources"] == {}


def test_burstable_low_uses_a_low_request_and_limit(manifest_path: Path) -> None:
    document = yaml.safe_load(
        render(
            workload="hybrid",
            policy="burstable-low",
            node="worker-a",
            run_id="run-17",
            fit_cpu="1000m",
            low_cpu="500m",
            manifest_path=manifest_path,
        )
    )

    resources = document["spec"]["containers"][0]["resources"]
    assert resources["requests"]["cpu"] == "100m"
    assert resources["limits"]["cpu"] == "500m"


def test_rejects_a_leftover_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(FIXTURE_MANIFEST.replace("__NODE__", "__EXTRA__"), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown placeholder"):
        render(
            workload="cpu-bound",
            policy="fit",
            node="worker-a",
            run_id="run-17",
            fit_cpu="2000m",
            low_cpu="500m",
            manifest_path=path,
        )


def test_rejects_repeated_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "repeated.yaml"
    path.write_text(FIXTURE_MANIFEST + "# __NAME__\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must occur exactly once"):
        render(
            workload="cpu-bound",
            policy="fit",
            node="worker-a",
            run_id="run-17",
            fit_cpu="2000m",
            low_cpu="500m",
            manifest_path=path,
        )


@pytest.mark.parametrize(
    ("workload", "policy"),
    (("missing", "fit"), ("cpu-bound", "missing")),
)
def test_rejects_unknown_workload_or_policy(
    manifest_path: Path, workload: str, policy: str
) -> None:
    with pytest.raises(ValueError, match="unknown"):
        render(
            workload=workload,
            policy=policy,
            node="worker-a",
            run_id="run-17",
            fit_cpu="2000m",
            low_cpu="500m",
            manifest_path=manifest_path,
        )
