"""Contract tests pinning the CAPI group-version migration (TEST-FIRST).

These tests are written TEST-FIRST for TASK-G2 (the manifest flip): every
test in this module FAILS against the current ``capi/`` tree, which still
carries ``cluster.x-k8s.io/v1beta1`` and
``addons.cluster.x-k8s.io/v1beta1``. They turn GREEN only when TASK-G2
rewrites the manifests to the v1beta2 group-versions.

Contracts pinned here:

no-v1beta1-pin
    Neither ``cluster.x-k8s.io/v1beta1`` nor
    ``addons.cluster.x-k8s.io/v1beta1`` may appear anywhere under
    ``capi/`` -- recursive raw-text scan over every ``*.yaml`` file,
    comments included. Exact full group-version strings are matched;
    the shared ``cluster.x-k8s.io`` group prefix means the core string
    also occurs as a substring of the addons string, so both are scanned
    separately purely for precise failure attribution. Unrelated groups
    (``infrastructure|controlplane|bootstrap.cluster.x-k8s.io/v1alpha1``
    and the ``addons.cluster.x-k8s.io/resource-set`` Secret type) do not
    contain either pinned string and stay untouched.

v1beta2-positive-pins
    The migrated documents must carry the NEW group-versions as their
    actual ``apiVersion`` field values (parsed YAML, exact equality --
    not substring presence):

    - ``capi/cluster.yaml``: the ClusterClass document AND the Cluster
      document carry ``cluster.x-k8s.io/v1beta2``.
    - ``capi/cluster-lab2.yaml``: the Cluster document carries
      ``cluster.x-k8s.io/v1beta2``.
    - ``capi/addons/{cilium,coredns,rbac}-crs.yaml``: every
      ClusterResourceSet document carries
      ``addons.cluster.x-k8s.io/v1beta2``.

Running: ``uv run pytest tests/test_capi_gv_migration.py`` (PyYAML comes
from the dev dependency group).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPI_DIR = REPO_ROOT / "capi"
CLUSTER_YAML = CAPI_DIR / "cluster.yaml"
CLUSTER_LAB2_YAML = CAPI_DIR / "cluster-lab2.yaml"
ADDONS_DIR = CAPI_DIR / "addons"

CORE_GROUP = "cluster.x-k8s.io"
ADDONS_GROUP = "addons.cluster.x-k8s.io"

# Requirement 1: these two exact strings must not appear anywhere under
# capi/. Note the addons string CONTAINS the core string as a substring;
# scanning both keeps failure output attributable per API group.
FORBIDDEN_GVS = (
    f"{CORE_GROUP}/v1beta1",
    f"{ADDONS_GROUP}/v1beta1",
)

# Requirement 2: the group-versions the manifests MUST carry after TASK-G2.
EXPECTED_CORE_GV = f"{CORE_GROUP}/v1beta2"
EXPECTED_ADDONS_GV = f"{ADDONS_GROUP}/v1beta2"

CRS_FILENAMES = ("cilium-crs.yaml", "coredns-crs.yaml", "rbac-crs.yaml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def capi_yaml_files() -> list[Path]:
    """Every ``*.yaml`` file under capi/, recursively."""
    assert CAPI_DIR.is_dir(), f"{CAPI_DIR} does not exist yet"
    files = sorted(CAPI_DIR.rglob("*.yaml"))
    assert files, f"{CAPI_DIR} contains no YAML files"
    return files


def load_docs(path: Path) -> list[dict[str, object]]:
    """Parse a YAML file into its non-null mapping documents."""
    text = path.read_text(encoding="utf-8")
    raw_docs = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    rel = str(path.relative_to(REPO_ROOT))
    assert raw_docs, f"{rel}: parsed to zero documents"
    docs: list[dict[str, object]] = []
    for doc in raw_docs:
        assert isinstance(doc, dict), f"{rel}: expected a mapping document"
        docs.append(cast(dict[str, object], doc))
    return docs


def docs_of_kind(
    docs: list[dict[str, object]],
    kind: str,
) -> list[dict[str, object]]:
    return [doc for doc in docs if doc.get("kind") == kind]


# ---------------------------------------------------------------------------
# Requirement 1: no-v1beta1-pin (recursive grep-style scan)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_gv",
    FORBIDDEN_GVS,
    ids=["core-group", "addons-group"],
)
def test_no_v1beta1_gv_string_remains_anywhere_under_capi(
    forbidden_gv: str,
) -> None:
    """Raw-text scan: the forbidden GV must not appear in any capi/*.yaml.

    Raw text (not parsed YAML) is deliberate: a stale occurrence inside a
    comment is exactly the kind of drift this pin exists to catch.
    """
    violations: list[str] = []
    for path in capi_yaml_files():
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if forbidden_gv in line:
                violations.append(f"{rel}:{lineno}")
    assert not violations, (
        f"forbidden group-version {forbidden_gv!r} still present under "
        f"capi/ at:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Requirement 2: v1beta2-positive-pins (parsed apiVersion equality)
# ---------------------------------------------------------------------------


def test_cluster_yaml_clusterclass_doc_carries_core_v1beta2() -> None:
    docs = load_docs(CLUSTER_YAML)
    clusterclasses = docs_of_kind(docs, "ClusterClass")
    assert len(clusterclasses) == 1, (
        f"expected exactly one ClusterClass in cluster.yaml, found "
        f"{len(clusterclasses)}"
    )
    assert clusterclasses[0].get("apiVersion") == EXPECTED_CORE_GV, (
        f"cluster.yaml ClusterClass apiVersion "
        f"{clusterclasses[0].get('apiVersion')!r} != {EXPECTED_CORE_GV!r}"
    )


def test_cluster_yaml_cluster_doc_carries_core_v1beta2() -> None:
    docs = load_docs(CLUSTER_YAML)
    clusters = docs_of_kind(docs, "Cluster")
    assert len(clusters) == 1, (
        f"expected exactly one Cluster in cluster.yaml, found {len(clusters)}"
    )
    assert clusters[0].get("apiVersion") == EXPECTED_CORE_GV, (
        f"cluster.yaml Cluster apiVersion "
        f"{clusters[0].get('apiVersion')!r} != {EXPECTED_CORE_GV!r}"
    )


def test_cluster_lab2_yaml_cluster_doc_carries_core_v1beta2() -> None:
    docs = load_docs(CLUSTER_LAB2_YAML)
    clusters = docs_of_kind(docs, "Cluster")
    assert len(clusters) == 1, (
        f"expected exactly one Cluster in cluster-lab2.yaml, found {len(clusters)}"
    )
    assert clusters[0].get("apiVersion") == EXPECTED_CORE_GV, (
        f"cluster-lab2.yaml Cluster apiVersion "
        f"{clusters[0].get('apiVersion')!r} != {EXPECTED_CORE_GV!r}"
    )


@pytest.mark.parametrize("filename", CRS_FILENAMES)
def test_crs_file_clusterresourceset_carries_addons_v1beta2(
    filename: str,
) -> None:
    path = ADDONS_DIR / filename
    crs_docs = docs_of_kind(load_docs(path), "ClusterResourceSet")
    assert crs_docs, f"{path.relative_to(REPO_ROOT)}: no ClusterResourceSet doc"
    for crs in crs_docs:
        name = crs.get("metadata")
        label = (
            name.get("name")  # type: ignore[union-attr]
            if isinstance(name, dict)
            else "?"
        )
        assert crs.get("apiVersion") == EXPECTED_ADDONS_GV, (
            f"{path.relative_to(REPO_ROOT)}: ClusterResourceSet {label!r} "
            f"apiVersion {crs.get('apiVersion')!r} != {EXPECTED_ADDONS_GV!r}"
        )
