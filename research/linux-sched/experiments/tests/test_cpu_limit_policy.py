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
