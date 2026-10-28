"""Tests for versioned, hashed CPU-limit model bundles."""

from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path

import pytest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

from bundle import (  # noqa: E402
    BundleError,
    build_bundle,
    canonical_json,
    validate_bundle,
    verify_model_hash,
)


def response_cell(*, valid: bool = True, dependency_bound: str = "no") -> dict:
    return {
        "conditions": {
            "mix": "balanced",
            "postgres_capacity": "medium",
            "redis_capacity": "medium",
            "node_capacity_millicores": 4000,
        },
        "cpu_limit_millicores": 200,
        "cpu_request_millicores": 100,
        "replicas": 1,
        "request_rate_per_second": 50.0,
        "contention": "none",
        "p50_latency_ms": 20.0,
        "p99_latency_ms": 80.0,
        "p99_upper_ms": 90.0,
        "throughput_rps": 50.0,
        "error_ratio": 0.0,
        "availability_ratio": 1.0,
        "cpu_usage_millicores": 180.0,
        "throttle_ratio": 0.01,
        "throttled_ms_per_second": 2.0,
        "samples": 3,
        "valid": valid,
        "dependency_bound": dependency_bound,
    }


def bundle() -> dict:
    return build_bundle(
        model_id="mixed-http-v1",
        created_at="2026-10-27T18:00:00Z",
        provenance={
            "generator": "phase8",
            "campaign_id": "campaign-1",
            "protocol_version": "phase8/v1",
            "trials_total": 1,
            "trials_usable": 1,
        },
        workload={
            "profile": "mixed-http",
            "contract_version": "db-cache-mixed/v1",
            "containers": [{"name": "app", "role": "app"}],
        },
        telemetry={
            "contract_version": "cpu-limit-telemetry/v1",
            "required_signals": ["request.p99_latency_ms"],
        },
        factors={
            "cpu_limit_millicores": {"min": 100, "max": 400},
            "replicas": {"min": 1, "max": 2},
            "request_rate_per_second": {"min": 10, "max": 50},
            "mix": ["balanced"],
            "contention": ["none"],
            "cpu_request_millicores": {"min": 50, "max": 200},
            "node_capacity_millicores": {"min": 4000, "max": 4000},
        },
        slo={
            "max_p99_latency_ms": 100,
            "min_throughput_rps": 45,
            "max_error_ratio": 0,
            "min_availability_ratio": 0.99,
        },
        policy_defaults={
            "confidence_level": 0.95,
            "latency_margin_ratio": 0.1,
            "headroom_ratio": 0.1,
            "min_replicas": 1,
            "max_replicas": 3,
        },
        response=[response_cell()],
        exclusions=[{"reason": "dependency bound", "trials": 1}],
    )


def test_build_bundle_hash_is_canonical_and_verifiable() -> None:
    model = bundle()

    assert model["schema_version"] == "cpu-limit-model/v1"
    assert model["model_hash"].startswith("sha256:")
    assert verify_model_hash(model)
    assert canonical_json(model) == canonical_json(dict(reversed(model.items())))


def test_shared_fixture_is_deterministic() -> None:
    model = bundle()
    fixture = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "fixtures"
        / "model-bundle-valid.json"
    )
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps(model, sort_keys=True, indent=2) + "\n")

    from bundle import dump_bundle

    assert json.loads(dump_bundle(json.loads(fixture.read_text()))) == model


def test_validation_rejects_tampering_and_invalid_response_cells() -> None:
    model = bundle()
    validate_bundle(model)

    tampered = {**model, "model_id": "other-model"}
    with pytest.raises(BundleError, match="model_hash"):
        validate_bundle(tampered)

    invalid = {**model, "response": [{**model["response"][0], "samples": 0}]}
    hash_input = {key: value for key, value in invalid.items() if key != "model_hash"}
    invalid["model_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with pytest.raises(BundleError, match="samples"):
        validate_bundle(invalid)


def test_validation_rejects_missing_identity_and_extrapolation_metadata() -> None:
    model = bundle()
    missing = {key: value for key, value in model.items() if key != "workload"}

    with pytest.raises(BundleError, match="workload"):
        validate_bundle(missing)


def test_validation_rejects_schema_unknown_fields_and_inconsistent_provenance() -> None:
    model = bundle()
    extra = {**model, "unexpected": True}
    _rehash(extra)
    with pytest.raises(BundleError, match="unsupported fields"):
        validate_bundle(extra)

    inconsistent = {**model, "provenance": {**model["provenance"], "trials_usable": 2}}
    _rehash(inconsistent)
    with pytest.raises(BundleError, match="cannot exceed"):
        validate_bundle(inconsistent)


def _rehash(document: dict) -> None:
    hash_input = {key: value for key, value in document.items() if key != "model_hash"}
    document["model_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(hash_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
