from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cgroup_manifest as cm  # noqa: E402


def test_default_nitosis_parent_is_kubernetes_workload_cgroup():
    assert cm.nitosis_cell_parent() == "/kubepods.slice"
    assert "kubepods" in cm.KUBERNETES_WORKLOAD_CGROUP_PARENT


def test_allowed_parent_accepted():
    assert cm.validate_cgroup_parent("/kubepods.slice") is True
    assert cm.cgroup_manifest_access("/kubepods.slice") is True


def test_arbitrary_path_rejected():
    assert cm.validate_cgroup_parent("/etc") is False
    assert cm.validate_cgroup_parent("/sys/fs/cgroup") is False
    assert cm.validate_cgroup_parent("../etc") is False


def test_resolve_rejects_bad_path():
    try:
        cm.resolve_nitosis_cgroup_parent("/etc/passwd")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_read_only_access_returns_path():
    assert cm.read_cgroup_manifest("/kubepods.slice") == "/kubepods.slice"


def test_invalid_cgroup_path_rejected():
    assert cm.is_valid_cgroup_path("") is False
    assert cm.is_valid_cgroup_path("kubepods") is False
    assert cm.is_valid_cgroup_path("/a/../b") is False
