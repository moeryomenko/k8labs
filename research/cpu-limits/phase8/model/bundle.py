"""Build and validate deterministic CPU-limit model bundles."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


class BundleError(ValueError):
    """A model bundle cannot be safely consumed."""


REQUIRED_FIELDS = (
    "schema_version",
    "model_id",
    "model_hash",
    "created_at",
    "provenance",
    "workload",
    "telemetry",
    "factors",
    "slo",
    "policy_defaults",
    "response",
    "exclusions",
)
TOP_LEVEL_FIELDS = set(REQUIRED_FIELDS)


def canonical_json(document: Mapping[str, Any]) -> str:
    """Serialize a document with stable ordering and no insignificant space."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _hash_input(document: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    result.pop("model_hash", None)
    return result


def _expected_hash(document: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(_hash_input(document)).encode()).hexdigest()
    return f"sha256:{digest}"


def verify_model_hash(document: Mapping[str, Any]) -> bool:
    return document.get("model_hash") == _expected_hash(document)


def build_bundle(
    *,
    model_id: str,
    created_at: str,
    provenance: Mapping[str, Any],
    workload: Mapping[str, Any],
    telemetry: Mapping[str, Any],
    factors: Mapping[str, Any],
    slo: Mapping[str, Any],
    policy_defaults: Mapping[str, Any],
    response: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "cpu-limit-model/v1",
        "model_id": model_id,
        "model_hash": "",
        "created_at": created_at,
        "provenance": copy.deepcopy(dict(provenance)),
        "workload": copy.deepcopy(dict(workload)),
        "telemetry": copy.deepcopy(dict(telemetry)),
        "factors": copy.deepcopy(dict(factors)),
        "slo": copy.deepcopy(dict(slo)),
        "policy_defaults": copy.deepcopy(dict(policy_defaults)),
        "response": [copy.deepcopy(dict(row)) for row in response],
        "exclusions": [copy.deepcopy(dict(row)) for row in exclusions],
    }
    document["model_hash"] = _expected_hash(document)
    validate_bundle(document)
    return document


def _require_mapping(document: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = document.get(field)
    if not isinstance(value, Mapping):
        raise BundleError(f"{field} must be an object")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise BundleError(f"{field} contains unsupported fields: {', '.join(unknown)}")


def _finite_number(value: object, field: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BundleError(f"{field} must be a number")
    if not math.isfinite(float(value)):
        raise BundleError(f"{field} must be finite")


def validate_bundle(document: Mapping[str, Any]) -> None:
    """Validate safety-critical invariants without silently repairing input."""
    missing = [field for field in REQUIRED_FIELDS if field not in document]
    if missing:
        raise BundleError(f"missing required fields: {', '.join(missing)}")
    _reject_unknown(document, TOP_LEVEL_FIELDS, "bundle")
    if document["schema_version"] != "cpu-limit-model/v1":
        raise BundleError("unsupported schema_version")
    model_id = document["model_id"]
    if not isinstance(model_id, str) or not model_id or len(model_id) > 128:
        raise BundleError(
            "model_id must be a non-empty string of at most 128 characters"
        )
    if not isinstance(document["model_hash"], str) or not verify_model_hash(document):
        raise BundleError("model_hash does not match canonical bundle content")

    provenance = _require_mapping(document, "provenance")
    _reject_unknown(
        provenance,
        {
            "generator",
            "campaign_id",
            "protocol_version",
            "git_revision",
            "trials_total",
            "trials_usable",
            "cluster_baseline",
        },
        "provenance",
    )
    for field in ("generator", "campaign_id", "protocol_version"):
        if not isinstance(provenance.get(field), str) or not provenance[field]:
            raise BundleError(f"provenance.{field} is required")
    for field in ("trials_total", "trials_usable"):
        value = provenance.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BundleError(f"provenance.{field} must be a non-negative integer")
    if provenance["trials_usable"] > provenance["trials_total"]:
        raise BundleError("provenance.trials_usable cannot exceed trials_total")
    if "cluster_baseline" in provenance:
        baseline = _require_mapping(provenance, "cluster_baseline")
        _reject_unknown(
            baseline,
            {"kubernetes_version", "container_runtime"},
            "provenance.cluster_baseline",
        )
        for field in ("kubernetes_version", "container_runtime"):
            if not isinstance(baseline.get(field), str):
                raise BundleError(f"provenance.cluster_baseline.{field} is required")

    workload = _require_mapping(document, "workload")
    _reject_unknown(workload, {"profile", "contract_version", "containers"}, "workload")
    for field in ("profile", "contract_version", "containers"):
        if field not in workload:
            raise BundleError(f"workload.{field} is required")
    if workload["contract_version"] != "db-cache-mixed/v1":
        raise BundleError("unsupported workload.contract_version")
    if not isinstance(workload["containers"], list) or not workload["containers"]:
        raise BundleError("workload.containers must be non-empty")
    names: set[str] = set()
    for container in workload["containers"]:
        if not isinstance(container, Mapping):
            raise BundleError("workload.containers entries must be objects")
        _reject_unknown(container, {"name", "role"}, "workload.containers")
        if not isinstance(container.get("name"), str) or not container["name"]:
            raise BundleError("workload.containers.name is required")
        if container.get("role") not in {"app", "sidecar"}:
            raise BundleError("workload.containers.role is invalid")
        if container["name"] in names:
            raise BundleError("workload container names must be unique")
        names.add(container["name"])

    telemetry = _require_mapping(document, "telemetry")
    _reject_unknown(telemetry, {"contract_version", "required_signals"}, "telemetry")
    if telemetry.get("contract_version") != "cpu-limit-telemetry/v1":
        raise BundleError("unsupported telemetry.contract_version")
    if (
        not isinstance(telemetry.get("required_signals"), list)
        or not telemetry["required_signals"]
    ):
        raise BundleError("telemetry.required_signals must be non-empty")
    for signal in telemetry["required_signals"]:
        if not isinstance(signal, str) or not signal.split(".", 1)[0] in {
            "request",
            "dependency",
            "resource",
            "hpa",
        }:
            raise BundleError("telemetry.required_signals contains an invalid signal")

    factors = _require_mapping(document, "factors")
    _reject_unknown(
        factors,
        {
            "cpu_limit_millicores",
            "replicas",
            "request_rate_per_second",
            "mix",
            "contention",
            "cpu_request_millicores",
            "node_capacity_millicores",
            "postgres_capacity",
            "redis_capacity",
        },
        "factors",
    )
    for field in (
        "cpu_limit_millicores",
        "replicas",
        "request_rate_per_second",
        "cpu_request_millicores",
        "node_capacity_millicores",
    ):
        factor_range = _require_mapping(factors, field)
        _reject_unknown(factor_range, {"min", "max"}, f"factors.{field}")
        _finite_number(factor_range.get("min"), f"factors.{field}.min")
        _finite_number(factor_range.get("max"), f"factors.{field}.max")
        if factor_range["min"] > factor_range["max"]:
            raise BundleError(f"factors.{field} min exceeds max")
    for field in ("mix", "contention"):
        levels = factors.get(field)
        if (
            not isinstance(levels, list)
            or not levels
            or any(not isinstance(level, str) or not level for level in levels)
        ):
            raise BundleError(f"factors.{field} must contain non-empty levels")
    slo = _require_mapping(document, "slo")
    _reject_unknown(
        slo,
        {
            "max_p99_latency_ms",
            "min_throughput_rps",
            "max_error_ratio",
            "min_availability_ratio",
        },
        "slo",
    )
    for field in (
        "max_p99_latency_ms",
        "min_throughput_rps",
        "max_error_ratio",
        "min_availability_ratio",
    ):
        _finite_number(slo.get(field), f"slo.{field}")
    if (
        slo["max_p99_latency_ms"] <= 0
        or slo["min_throughput_rps"] < 0
        or not 0 <= slo["max_error_ratio"] <= 1
        or not 0 <= slo["min_availability_ratio"] <= 1
    ):
        raise BundleError("slo values are outside allowed ranges")
    policy = _require_mapping(document, "policy_defaults")
    _reject_unknown(
        policy,
        {
            "confidence_level",
            "latency_margin_ratio",
            "headroom_ratio",
            "min_replicas",
            "max_replicas",
        },
        "policy_defaults",
    )
    confidence = policy.get("confidence_level")
    _finite_number(confidence, "policy_defaults.confidence_level")
    if not 0.5 < float(confidence) <= 0.999:
        raise BundleError("policy_defaults.confidence_level must be above 0.5")
    for field in ("latency_margin_ratio", "headroom_ratio"):
        _finite_number(policy.get(field), f"policy_defaults.{field}")
        if not 0 <= policy[field] <= 1:
            raise BundleError(f"policy_defaults.{field} is outside [0, 1]")
    if (
        not all(
            isinstance(policy.get(field), int) and policy[field] >= 1
            for field in ("min_replicas", "max_replicas")
        )
        or policy["min_replicas"] > policy["max_replicas"]
    ):
        raise BundleError("policy replica bounds are invalid")

    response = document["response"]
    if not isinstance(response, list) or not response:
        raise BundleError("response must be non-empty")
    for index, row in enumerate(response):
        if not isinstance(row, Mapping):
            raise BundleError(f"response[{index}] must be an object")
        _reject_unknown(
            row,
            {
                "conditions",
                "cpu_limit_millicores",
                "cpu_request_millicores",
                "replicas",
                "request_rate_per_second",
                "contention",
                "p50_latency_ms",
                "p99_latency_ms",
                "p99_upper_ms",
                "throughput_rps",
                "error_ratio",
                "availability_ratio",
                "cpu_usage_millicores",
                "throttle_ratio",
                "throttled_ms_per_second",
                "dependency_bound",
                "samples",
                "valid",
                "invalid_reason",
            },
            f"response[{index}]",
        )
        for field in ("conditions", "samples", "valid"):
            if field not in row:
                raise BundleError(f"response[{index}].{field} is required")
        if not isinstance(row["samples"], int) or row["samples"] < 1:
            raise BundleError(f"response[{index}].samples must be positive")
        if not isinstance(row["valid"], bool):
            raise BundleError(f"response[{index}].valid must be boolean")
        if row.get("dependency_bound", "no") not in {
            "no",
            "postgres",
            "redis",
            "load_generator",
            "unknown",
        }:
            raise BundleError(f"response[{index}].dependency_bound is invalid")
        conditions = row["conditions"]
        if not isinstance(conditions, Mapping):
            raise BundleError(f"response[{index}].conditions must be an object")
        _reject_unknown(
            conditions,
            {"mix", "postgres_capacity", "redis_capacity", "node_capacity_millicores"},
            f"response[{index}].conditions",
        )
        for field in ("mix", "postgres_capacity", "redis_capacity"):
            if not isinstance(conditions.get(field), str) or not conditions[field]:
                raise BundleError(f"response[{index}].conditions.{field} is required")
        if (
            not isinstance(conditions.get("node_capacity_millicores"), int)
            or conditions["node_capacity_millicores"] < 1
        ):
            raise BundleError(
                f"response[{index}].conditions.node_capacity_millicores is invalid"
            )
        for field in (
            "cpu_limit_millicores",
            "cpu_request_millicores",
            "replicas",
            "request_rate_per_second",
            "p99_latency_ms",
            "p99_upper_ms",
            "throughput_rps",
            "error_ratio",
            "availability_ratio",
        ):
            if field not in row:
                raise BundleError(f"response[{index}].{field} is required")
            _finite_number(row[field], f"response[{index}].{field}")
        for field in ("cpu_limit_millicores", "cpu_request_millicores", "replicas"):
            if not isinstance(row[field], int) or row[field] < 1:
                raise BundleError(
                    f"response[{index}].{field} must be a positive integer"
                )
        for field in ("error_ratio", "availability_ratio"):
            if not 0 <= row[field] <= 1:
                raise BundleError(f"response[{index}].{field} is outside [0, 1]")


def dump_bundle(document: Mapping[str, Any]) -> str:
    validate_bundle(document)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
