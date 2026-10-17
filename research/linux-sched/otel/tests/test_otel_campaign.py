"""Tests for the OTel campaign's offline artefact validation."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

OTEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OTEL_DIR))

from validate_campaign import Problems, validate  # noqa: E402


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "otel"
    shutil.copytree(OTEL_DIR, root)
    return root


def test_the_real_artefacts_pass(tmp_path: Path) -> None:
    problems = Problems()
    validate(problems, root=OTEL_DIR, mk_path=Path(__file__).resolve().parents[2] / "linux-sched.mk")
    assert problems == []


def test_a_privileged_profiler_is_reported(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    daemonset = root / "k8s" / "alloy-daemonset.yaml"
    daemonset.write_text(
        daemonset.read_text(encoding="utf-8").replace(
            "          securityContext:", "          securityContext:\n            privileged: true"
        ),
        encoding="utf-8",
    )
    problems = Problems()
    validate(problems, root=root, mk_path=Path("/dev/null"))
    assert any("privileged" in problem for problem in problems)


def test_a_missing_capability_is_reported(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    daemonset = root / "k8s" / "alloy-daemonset.yaml"
    daemonset.write_text(
        daemonset.read_text(encoding="utf-8").replace("                - SYSLOG\n", ""),
        encoding="utf-8",
    )
    problems = Problems()
    validate(problems, root=root, mk_path=Path("/dev/null"))
    assert any("SYSLOG" in problem for problem in problems)


def test_a_missing_session_label_is_reported(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    template = root / "k8s" / "alloy-config.alloy.tmpl"
    template.write_text(
        template.read_text(encoding="utf-8").replace('target_label  = "run_id"', 'target_label  = "run"'),
        encoding="utf-8",
    )
    problems = Problems()
    validate(problems, root=root, mk_path=Path("/dev/null"))
    assert any("run_id" in problem for problem in problems)


def test_routing_profiles_through_alloys_otlp_receiver_is_reported(tmp_path: Path) -> None:
    # The design this replaced: Alloy cannot carry the profiles signal, verified
    # by running v1.20.1 and v1.21.0-rc.0.
    root = _copy(tmp_path)
    template = root / "k8s" / "alloy-config.alloy.tmpl"
    template.write_text(
        'otelcol.receiver.otlp "profiler" {}\n' + template.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    problems = Problems()
    validate(problems, root=root, mk_path=Path("/dev/null"))
    assert any("OTLP receiver" in problem for problem in problems)
