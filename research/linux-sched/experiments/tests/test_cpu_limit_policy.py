from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cpu_limit_policy as clp  # noqa: E402


MANIFEST = """      resources:
        requests:
          cpu: "1"
        limits: {}
"""


def test_render_applies_limit() -> None:
    out = clp.render_cpu_limit_policy(MANIFEST, policy="limited", cpu_limit="500m")
    assert "cpu: 500m" in out
    assert "requests:" in out


def test_unlimited_removes_limit() -> None:
    src = MANIFEST.replace("limits: {}", 'limits:\n          cpu: "__CPU_LIMIT__"')
    out = clp.render_cpu_limit_policy(src, policy="unlimited")
    assert "limits: {}" in out


def test_requests_remain_fixed() -> None:
    out = clp.render_cpu_limit_policy(MANIFEST, policy="limited", cpu_limit="250m")
    assert 'cpu: "1"' in out


def test_cli_renders_both_policies(tmp_path: Path) -> None:
    src = MANIFEST.replace("limits: {}", 'limits:\n          cpu: "__CPU_LIMIT__"')
    manifest = tmp_path / "workload.yaml"
    manifest.write_text(src, encoding="utf-8")

    unlimited = tmp_path / "unlimited.yaml"
    assert clp.main(["--manifest", str(manifest), "--policy", "unlimited",
                     "--output", str(unlimited)]) == 0
    assert "limits: {}" in unlimited.read_text(encoding="utf-8")

    limited = tmp_path / "limited.yaml"
    assert clp.main(["--manifest", str(manifest), "--policy", "limited",
                     "--cpu-limit", "750m", "--output", str(limited)]) == 0
    assert 'cpu: "750m"' in limited.read_text(encoding="utf-8")
