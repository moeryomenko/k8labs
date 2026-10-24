"""Contracts for CPU-limits workload manifest templates."""

from __future__ import annotations

from pathlib import Path

import yaml

MANIFESTS = Path(__file__).resolve().parents[1] / "workloads" / "manifests.yaml"
REQUIRED_PLACEHOLDERS = (
    "__NAME__",
    "__NODE__",
    "__RUN_ID__",
    "__WORKLOAD__",
    "__RESOURCES__",
)


def _manifest_text() -> str:
    return MANIFESTS.read_text(encoding="utf-8")


def _documents() -> list[dict[str, object]]:
    return list(yaml.safe_load_all(_manifest_text()))


def test_manifests_define_six_unique_workload_templates() -> None:
    documents = _documents()

    assert len(documents) == 6
    assert {document["metadata"]["name"] for document in documents} == {"__NAME__"}
    labels = [
        document["metadata"]["labels"]["app.kubernetes.io/name"]
        for document in documents
    ]
    assert len(labels) == len(set(labels))


def test_each_manifest_uses_every_required_placeholder_once() -> None:
    text = _manifest_text()

    for document in text.split("---\n"):
        for placeholder in REQUIRED_PLACEHOLDERS:
            count = document.count(placeholder)
            if placeholder == "__RESOURCES__":
                assert count >= 1, (placeholder, document)
            else:
                assert count == 1, (placeholder, document)


def test_every_container_image_is_pinned_by_digest() -> None:
    for document in _documents():
        for container in document["spec"]["containers"]:
            image = container["image"]
            assert "@sha256:" in image, image


def test_resource_settings_are_renderer_placeholders_not_cpu_values() -> None:
    for document in _documents():
        for container in document["spec"]["containers"]:
            assert "resources" not in container
            assert container["__RESOURCES__"] == {}

    assert "cpu: " not in _manifest_text()
