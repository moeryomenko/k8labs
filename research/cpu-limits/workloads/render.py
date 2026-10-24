"""Render one CPU-limits workload manifest without changing its source file.

A hybrid workload applies the selected policy to both containers. Its Pod CPU
limit is the sum of the two container limits.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

WORKLOADS = ("cpu-bound", "io-bound", "memory-bound", "hybrid")
POLICIES = (
    "fit",
    "best-effort",
    "request-low",
    "request-high",
    "guaranteed-low",
    "burstable-low",
)
PLACEHOLDERS = (
    "__NAME__",
    "__NODE__",
    "__RUN_ID__",
    "__WORKLOAD__",
    "__RESOURCES__",
)
DEFAULT_MANIFEST_PATH = Path(__file__).with_name("manifests.yaml")
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
_RESOURCES_LINE = re.compile(r"(?m)^([ \t]*)__RESOURCES__:[ \t]*(?:\{\})?[ \t]*$")


def _pod_name(workload: str, policy: str, run_id: str) -> str:
    """Return a DNS-safe Pod name no longer than the 63-character limit."""

    normalized = re.sub(r"[^a-z0-9.-]+", "-", run_id.lower()).strip("-")
    name = f"cl-{workload}-{policy}-{normalized}"
    if len(name) <= 63:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:54]}-{digest}"


def pod_name(workload: str, policy: str, run_id: str) -> str:
    """Return the DNS-safe Pod name shared by the renderer and the runner."""

    return _pod_name(workload, policy, run_id)


def _cpu_value(value: str | int, name: str) -> str:
    """Return one positive Kubernetes CPU quantity."""

    if isinstance(value, bool):
        raise TypeError(f"{name} CPU must be a positive quantity")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"{name} CPU must be positive")
        return f"{value}m"
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} CPU must be a non-empty quantity")
    return value


def resources_for(
    policy: str, fit_cpu: str | int, low_cpu: str | int
) -> dict[str, dict[str, str]]:
    """Return the Kubernetes CPU resources for one valid policy."""

    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")

    fit = _cpu_value(fit_cpu, "fit")
    low = _cpu_value(low_cpu, "low")
    if policy == "best-effort":
        return {}
    if policy == "fit":
        return {"limits": {"cpu": fit}, "requests": {"cpu": fit}}
    if policy == "request-low":
        return {"requests": {"cpu": "100m"}}
    if policy == "request-high":
        return {"requests": {"cpu": fit}}
    if policy == "guaranteed-low":
        return {"limits": {"cpu": low}, "requests": {"cpu": low}}
    return {"limits": {"cpu": low}, "requests": {"cpu": "100m"}}


def _resources_yaml(resources: Mapping[str, Mapping[str, str]]) -> str:
    """Return a complete, stable YAML resources mapping."""

    if not resources:
        return "resources: {}"

    lines = ["resources:"]
    for section in ("limits", "requests"):
        values = resources.get(section)
        if values:
            lines.append(f"  {section}:")
            lines.append(f'    cpu: "{values["cpu"]}"')
    return "\n".join(lines)


def _read_template(path: Path, workload: str) -> str:
    """Read the one workload document from a multi-document manifest file."""

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read manifest: {path}") from error

    documents = re.split(r"(?m)^---\s*$", source)
    if len(documents) == 1:
        return source
    marker = f"app.kubernetes.io/name: {workload}"
    matches = [document for document in documents if marker in document]
    if len(matches) != 1:
        raise ValueError(f"manifest needs exactly one {workload} document")
    return matches[0]


def _validate_placeholders(template: str) -> None:
    """Reject placeholder names that are unknown, repeated, or unresolved."""

    unknown = sorted(set(_PLACEHOLDER.findall(template)) - set(PLACEHOLDERS))
    if unknown:
        raise ValueError(f"unknown placeholder: {unknown[0]}")
    for placeholder in PLACEHOLDERS:
        count = template.count(placeholder)
        if placeholder == "__RESOURCES__":
            if count < 1:
                raise ValueError("placeholder __RESOURCES__ must occur at least once")
        elif count != 1:
            raise ValueError(
                f"placeholder {placeholder} must occur exactly once, found {count}"
            )


def _replace_resources(template: str, resources: str) -> str:
    """Insert the same complete YAML resources mapping at every placeholder."""

    matches = list(_RESOURCES_LINE.finditer(template))
    if not matches:
        raise ValueError("__RESOURCES__ must occupy one indented YAML line")
    rendered = template
    for match in reversed(matches):
        indent = match.group(1)
        block = resources.replace("\n", f"\n{indent}")
        rendered = (
            rendered[: match.start()] + f"{indent}{block}" + rendered[match.end() :]
        )
    return rendered


def render(
    *,
    workload: str,
    policy: str,
    node: str,
    run_id: str,
    fit_cpu: str | int,
    low_cpu: str | int,
    manifest_path: Path | None = None,
) -> str:
    """Render one workload manifest from the source or a supplied fixture path."""

    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload}")
    if policy not in POLICIES:
        raise ValueError(f"unknown policy: {policy}")
    if not node:
        raise ValueError("node must not be empty")
    if not run_id:
        raise ValueError("run id must not be empty")

    template = _read_template(manifest_path or DEFAULT_MANIFEST_PATH, workload)
    _validate_placeholders(template)
    rendered = _replace_resources(
        template, _resources_yaml(resources_for(policy, fit_cpu, low_cpu))
    )
    replacements = {
        "__NAME__": _pod_name(workload, policy, run_id),
        "__NODE__": node,
        "__RUN_ID__": run_id,
        "__WORKLOAD__": workload,
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value, 1)

    leftover = _PLACEHOLDER.search(rendered)
    if leftover:
        raise ValueError(f"unresolved placeholder: {leftover.group(0)}")
    try:
        documents = list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as error:
        raise ValueError("rendered manifest is invalid YAML") from error
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("rendered manifest must contain exactly one mapping document")
    return rendered
