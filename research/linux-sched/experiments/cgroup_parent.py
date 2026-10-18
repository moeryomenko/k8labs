"""Narrow, read-only access to the Kubernetes workload cgroup manifest.

Nitosis isolates workloads into cells, one per direct child of a parent cgroup.
In this lab the parent is the Kubernetes workload cgroup (cri-o/kubelet), so
nitosis has to be told where that parent lives. Rather than handing the
scheduler the whole host cgroup tree, this module exposes only that one path,
validated and read-only.

The path is resolved from a small manifest-like declaration and is checked
against a narrow allow-list before it is handed to nitosis. Nothing here writes
to the cgroup filesystem: the access is read-only and limited to the declared
parent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Kubernetes (cri-o) workload cgroup parent on a cgroup-v2 host. This is the
# parent whose direct children are the pod scopes nitosis turns into cells.
KUBERNETES_WORKLOAD_CGROUP_PARENT = "/kubepods.slice"

# Narrow allow-list: only the Kubernetes workload cgroup roots may be used as a
# nitosis cell parent. No arbitrary host cgroup path is accepted.
ALLOWED_CGROUP_PARENTS = {
    "/kubepods",
    "/kubepods.slice",
    "/sys/fs/cgroup/kubepods",
    "/sys/fs/cgroup/kubepods.slice",
}

# A cgroup path must be absolute, contain no traversal, and stay inside the
# allowed set above.
_CGROUP_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


def is_valid_cgroup_path(path: str) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if ".." in path.split("/"):
        return False
    if not _CGROUP_PATH_RE.match(path):
        return False
    return True


def validate_cgroup_parent(path: str) -> bool:
    """Whether *path* is an allowed, narrow Kubernetes workload cgroup parent."""
    if not is_valid_cgroup_path(path):
        return False
    return path in ALLOWED_CGROUP_PARENTS


def resolve_nitosis_cgroup_parent(path: str | None = None) -> str:
    """Resolve the cgroup parent nitosis receives.

    Falls back to the Kubernetes workload cgroup parent when none is given, and
    raises ValueError when the supplied value is not on the allow-list.
    """
    candidate = path if path is not None else os.environ.get(
        "LINUX_SCHED_NITOSIS_CGROUP_PARENT", KUBERNETES_WORKLOAD_CGROUP_PARENT
    )
    if not validate_cgroup_parent(candidate):
        raise ValueError(
            f"narrow read-only cgroup manifest access rejected path: {candidate!r}"
        )
    return candidate


def nitosis_cell_parent() -> str:
    return resolve_nitosis_cgroup_parent()


def read_cgroup_manifest(path: str) -> str:
    """Read-only access to a cgroup manifest entry.

    Returns the validated path; never writes to the cgroup filesystem.
    """
    if not validate_cgroup_parent(path):
        raise ValueError(f"read-only cgroup manifest access rejected: {path!r}")
    return path


def cgroup_manifest_access(path: str) -> bool:
    return validate_cgroup_parent(path)


def main() -> int:
    import sys

    parent = nitosis_cell_parent()
    print(f"nitosis workload cgroup parent: {parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
