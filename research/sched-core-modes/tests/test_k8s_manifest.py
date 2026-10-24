"""Contract tests for the optional Kubernetes QoS sanity manifest."""

from __future__ import annotations

from pathlib import Path

import yaml

SERIES = Path(__file__).parents[1]
MANIFEST = SERIES / "workloads" / "k8s-qos.yaml"
PINNED_IMAGE = (
    "docker.io/alexeiled/stress-ng@"
    "sha256:91f1313a67e3c8c23f3c75855019d01c4a43e2f872b099f720047f03309d8ad4"
)


def load_docs() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def test_manifest_has_a_namespace_and_four_qos_jobs() -> None:
    docs = load_docs()
    kinds = [doc["kind"] for doc in docs]
    assert kinds.count("Namespace") == 1
    assert kinds.count("Job") == 4
    jobs = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Job"}
    assert set(jobs) == {
        "qos-guaranteed",
        "qos-burstable",
        "qos-besteffort",
        "qos-quota",
    }


def test_every_job_is_pinned_to_the_placeholder_worker_and_the_same_image() -> None:
    for doc in load_docs():
        if doc["kind"] != "Job":
            continue
        pod = doc["spec"]["template"]["spec"]
        assert pod["nodeName"] == "__NODE_NAME__"
        assert pod["restartPolicy"] == "Never"
        container = pod["containers"][0]
        assert container["image"] == PINNED_IMAGE
        assert container["args"][:2] == [
            "--cpu",
            "2" if doc["metadata"]["name"] != "qos-burstable" else "4",
        ]


def test_qos_resources_match_the_intended_shapes() -> None:
    jobs = {doc["metadata"]["name"]: doc for doc in load_docs() if doc["kind"] == "Job"}
    guaranteed = jobs["qos-guaranteed"]["spec"]["template"]["spec"]["containers"][0]
    assert guaranteed["resources"]["requests"]["cpu"] == "500m"
    assert guaranteed["resources"]["limits"]["cpu"] == "500m"

    burstable = jobs["qos-burstable"]["spec"]["template"]["spec"]["containers"][0]
    assert burstable["resources"]["requests"]["cpu"] == "250m"
    assert burstable["resources"]["limits"]["cpu"] == "1"

    besteffort = jobs["qos-besteffort"]["spec"]["template"]["spec"]["containers"][0]
    assert "resources" not in besteffort

    quota = jobs["qos-quota"]["spec"]["template"]["spec"]["containers"][0]
    assert quota["resources"]["requests"]["cpu"] == "250m"
    assert quota["resources"]["limits"]["cpu"] == "500m"
