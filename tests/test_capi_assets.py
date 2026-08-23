"""Contract tests for the declarative CAPI assets under ``capi/`` (TASK-002).

These tests are written TEST-FIRST: the ``capi/`` tree and the
``scripts/fetch-kubeconfig`` helper do not exist yet, so every test that
touches them FAILS or ERRORs at red phase. Two tests are exempt and pass
legitimately before implementation; each says so explicitly in its own
docstring:

- ``test_label_selector_match_expressions_semantics`` (pure selector logic)
- ``test_extensions_pin_source_self_consistent`` (reads extensions/, which
  predates capi/)

Contracts pinned here (each traces to a TASK-002 requirement):

cluster-manifest
    ``capi/cluster.yaml`` parses as YAML and contains exactly one Cluster
    object with concrete committed values (no ${VAR} markers): name k8labs,
    namespace default, ClusterClass hypervisor-cluster-template,
    control-plane replicas 1, worker machine deployment md-0 of class
    default-worker with replicas 3 -- the literal defaults documented by
    cluster-api-hypervisor/templates/cluster-template.yaml lines 81-85.

crs-selector-match
    Every ClusterResourceSet in ``capi/addons/`` carries a non-empty
    clusterSelector that MATCHES the labels carried by the Cluster in
    ``capi/cluster.yaml``, evaluated with real Kubernetes LabelSelector
    semantics (matchLabels plus matchExpressions In/NotIn/Exists/
    DoesNotExist) implemented programmatically below.

crs-payload-fidelity
    PAYLOAD EQUALITY RULE (chosen, asserted consistently): normalized
    YAML-equal, NOT byte-for-byte. For every resource Secret referenced by
    a CRS, the set of payload keys must exactly cover the top-level
    ``*.yaml`` files of the mapped repo directory (rbac/, cilium/,
    coredns/) and may only reference files that exist anywhere under that
    directory (recursive). Each embedded payload (base64-decoded from
    ``data`` or taken verbatim from ``stringData``) must parse to the same
    number of YAML documents as the repo file, with each document deep-
    equal to the corresponding repo document. Rationale: byte-for-byte
    would reject harmless trailing-newline/whitespace drift introduced by
    any packaging step, while normalized equality still catches all
    content drift, which is the actual risk. A key present in BOTH ``data``
    and ``stringData`` is ambiguous and fails.

    cilium/install/ extension (REVIEW-R1-TESTFIX): the cilium CRS payload
    must ADDITIONALLY cover every ``*.yaml`` file under ``cilium/install/``
    recursively (the 00-crds/ bundle, 00-gateway-api-crds.yaml, and
    01-namespace..04-workloads.yaml). Without these the CRS-only delivery
    flow cannot function: Gateway/lb-pool/l2-policy resources would
    reference nonexistent CRDs and there would be no Cilium datapath.
    Kubernetes caps a single Secret at ~1MiB and the install tree exceeds
    that, so the contract is SECRET-GROUPING AGNOSTIC for install/: every
    install YAML file must appear EXACTLY ONCE under its CANONICAL KEY
    across ALL Secrets referenced by the cilium CRS combined (spec.resources
    accepts multiple refs). Canonical key rule (REVISION-R2-TESTFIX): the
    file's repo path relative to ``cilium/`` with every ``/`` replaced by
    ``--`` -- e.g. ``install/00-crds/foo.yaml`` maps to key
    ``install--00-crds--foo.yaml``. Raw repo-relative keys contain slashes,
    which Kubernetes IsConfigMapKey validation rejects for Secret data
    keys, so live addons-up would fail apiserver validation even though
    offline tests pass; content fidelity (exactly-once, normalized-YAML-
    equal) remains the real contract, while key spelling is pinned to this
    slash-free mapping, verified injective over the committed tree (no two
    install paths map to one key, no filename contains ``--``). Which
    Secret carries a given key stays free. The per-Secret top-level
    coverage rule above is unchanged, as are the rbac/ and coredns/
    contracts.

smoke-job
    ``capi/smoke-test/job.yaml`` parses, contains exactly one Job, uses
    ONLY namespace-scoped kinds, and probes a host consistent with the
    Cilium LB config: candidate LB service DNS names are derived from
    cilium/**.yaml (Gateway objects -> the same-named Service Cilium's
    gateway implementation provisions in the Gateway's namespace, plus any
    explicit ``type: LoadBalancer`` Services), expanded to the DNS forms
    ``<name>.<ns>.svc.cluster.local``, ``<name>.<ns>.svc``, ``<name>.<ns>``
    and bare ``<name>``. The Job must reference at least one candidate in
    its command/args/env strings. Fallback branch (documented): if no
    candidate can be derived from cilium/ manifests, the Job must instead
    mention the placeholder constant ``LB_SERVICE_HOST_PLACEHOLDER``. With
    the current cilium/ tree (Gateway cilium-gw@default) the primary
    branch always applies.

kubeconfig-fetch-contract
    THESE TESTS DEFINE THE REQUIRED INTERFACE of the not-yet-written
    helper: an executable ``scripts/fetch-kubeconfig`` invoked as
    ``scripts/fetch-kubeconfig <path-to-secret-manifest.yaml>``. It must
    read the Secret manifest file, decode its ``value`` key (base64 in
    ``data`` or plaintext in ``stringData``), print the decoded kubeconfig
    YAML to stdout, and exit 0. On a missing ``value`` key, invalid base64,
    empty decoded content, or a decoded document whose kind is not Config /
    apiVersion is not v1, it must exit non-zero and write a diagnostic to
    stderr. The workload Secret shape this serves is
    ``<cluster>-kubeconfig`` with data key ``value``.

version-pin-consistency
    The pinned Kubernetes version is extracted from the extensions build
    config (``extensions/manifest.yaml``, sysext entries ``kubelet`` and
    ``kubernetes-cp``); the two pins must agree, and
    ``capi/cluster.yaml`` topology.version must equal that pin.

offline-validation
    CHOSEN METHOD: pure client-side YAML/schema assertions, NOT
    ``kubectl apply --dry-run=client``. Reason (verified against kubectl
    1.32 behavior): the assets use CRD kinds (cluster.x-k8s.io/v1beta1,
    infrastructure|controlplane|bootstrap.cluster.x-k8s.io/v1alpha1,
    addons.cluster.x-k8s.io/v1beta1) that are absent from kubectl's local
    scheme, so even with ``--validate=false`` client-side apply needs REST
    mapping via discovery against a live API server and fails offline.
    The structural checks below (apiVersion/kind/metadata.name presence,
    DNS-1123 name rules, kind-specific required fields) run entirely
    offline.

Running: requires PyYAML. Either
``python3 -m pytest tests/test_capi_assets.py`` (system interpreter ships
PyYAML) or ``uv run --with pyyaml --no-sync pytest tests/test_capi_assets.py``
(the project venv does not currently pin PyYAML).
"""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPI_DIR = REPO_ROOT / "capi"
CLUSTER_YAML = CAPI_DIR / "cluster.yaml"
ADDONS_DIR = CAPI_DIR / "addons"
SMOKE_JOB = CAPI_DIR / "smoke-test" / "job.yaml"
EXT_MANIFEST = REPO_ROOT / "extensions" / "manifest.yaml"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch-kubeconfig"

LB_HOST_PLACEHOLDER = "LB_SERVICE_HOST_PLACEHOLDER"

# Concrete values committed from cluster-api-hypervisor template defaults
# (templates/cluster-example.yaml twin, lines 81-85 of cluster-template.yaml).
EXPECTED_CLUSTER_NAME = "k8labs"
EXPECTED_NAMESPACE = "default"
EXPECTED_CLUSTERCLASS = "hypervisor-cluster-template"
EXPECTED_CP_REPLICAS = 1
EXPECTED_WORKER_CLASS = "default-worker"
EXPECTED_WORKER_REPLICAS = 3

# Directory each CRS maps to, keyed by the token that must appear in the
# CRS metadata.name.
CRS_DIR_MAP: dict[str, str] = {
    "rbac": "rbac",
    "cilium": "cilium",
    "coredns": "coredns",
}

RESOURCE_SET_SECRET_TYPE = "addons.cluster.x-k8s.io/resource-set"

# Kinds allowed in the smoke-test stream; anything else (and specifically
# the denylist members) must not appear.
NAMESPACE_SCOPED_KINDS = {
    "Job",
    "CronJob",
    "Pod",
    "Service",
    "ServiceAccount",
    "ConfigMap",
    "Secret",
    "Role",
    "RoleBinding",
    "PersistentVolumeClaim",
}
CLUSTER_SCOPED_DENYLIST = {
    "Namespace",
    "Node",
    "ClusterRole",
    "ClusterRoleBinding",
    "PersistentVolume",
    "CustomResourceDefinition",
    "APIService",
    "ValidatingWebhookConfiguration",
    "MutatingWebhookConfiguration",
    "Cluster",
    "ClusterClass",
    "ClusterResourceSet",
}

DNS_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
TEMPLATE_MARKER_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def as_mapping(value: object, where: str) -> dict[str, object]:
    """Narrow an arbitrary parsed value to a mapping, failing loudly."""
    assert isinstance(value, dict), f"{where}: expected a mapping, got {type(value)}"
    return cast(dict[str, object], value)


def load_docs(path: Path) -> list[dict[str, object]]:
    """Parse a YAML file into its non-null mapping documents."""
    text = path.read_text(encoding="utf-8")
    raw_docs = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    assert raw_docs, f"{path.relative_to(REPO_ROOT)}: parsed to zero documents"
    return [as_mapping(doc, str(path.relative_to(REPO_ROOT))) for doc in raw_docs]


def docs_of_kind(docs: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [doc for doc in docs if doc.get("kind") == kind]


def obj_name(obj: dict[str, object]) -> str:
    """Best-effort metadata.name of a parsed object."""
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str):
            return name
    return ""


def label_selector_matches(selector: dict[str, object], labels: dict[str, str]) -> bool:
    """Evaluate a Kubernetes LabelSelector against a label set."""
    match_labels = cast(dict[str, str], selector.get("matchLabels") or {})
    for key, value in match_labels.items():
        if labels.get(key) != value:
            return False
    expressions = cast(list[dict[str, object]], selector.get("matchExpressions") or [])
    for expr in expressions:
        key = cast(str, expr.get("key"))
        operator = cast(str, expr.get("operator"))
        values = cast(list[str], expr.get("values") or [])
        actual = labels.get(key)
        if operator == "In":
            if actual is None or actual not in values:
                return False
        elif operator == "NotIn":
            if actual is not None and actual in values:
                return False
        elif operator == "Exists":
            if actual is None:
                return False
        elif operator == "DoesNotExist":
            if actual is not None:
                return False
        else:
            raise AssertionError(f"unsupported selector operator: {operator!r}")
    return True


def secret_payloads(secret: dict[str, object]) -> dict[str, bytes]:
    """Merge a Secret's stringData/data payloads into plaintext bytes."""
    data = cast(dict[str, str], secret.get("data") or {})
    string_data = cast(dict[str, str], secret.get("stringData") or {})
    overlap = sorted(set(data) & set(string_data))
    assert not overlap, (
        f"Secret {obj_name(secret)!r}: key(s) {overlap} present in both "
        "data and stringData"
    )
    payloads: dict[str, bytes] = {}
    for key, encoded in data.items():
        try:
            payloads[key] = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # surfaced as assertion text below
            raise AssertionError(
                f"Secret {obj_name(secret)!r}: data[{key!r}] is not valid base64: {exc}"
            ) from exc
    for key, plain in string_data.items():
        payloads[key] = plain.encode("utf-8")
    return payloads


def normalized_yaml_equal(payload: bytes, repo_file: Path) -> bool:
    """Payload-fidelity rule: same doc count, each document deep-equal."""
    payload_docs = [
        doc for doc in yaml.safe_load_all(payload.decode("utf-8")) if doc is not None
    ]
    repo_docs = [
        doc
        for doc in yaml.safe_load_all(repo_file.read_text(encoding="utf-8"))
        if doc is not None
    ]
    if len(payload_docs) != len(repo_docs):
        return False
    return all(a == b for a, b in zip(payload_docs, repo_docs))


def crs_to_repo_dir(crs_name: str) -> str | None:
    """Map a CRS name to its repo manifest directory via name token."""
    hits = [d for token, d in CRS_DIR_MAP.items() if token in crs_name]
    assert len(hits) <= 1, f"CRS {crs_name!r} matches multiple addon tokens"
    return hits[0] if hits else None


def pinned_k8s_versions() -> dict[str, str]:
    """Extract Kubernetes version pins from the extensions build config."""
    manifest = as_mapping(
        yaml.safe_load(EXT_MANIFEST.read_text(encoding="utf-8")),
        str(EXT_MANIFEST),
    )
    sysexts = cast(list[dict[str, object]], manifest["sysexts"])
    versions: dict[str, str] = {}
    for entry in sysexts:
        name = cast(str, entry["name"])
        if name in ("kubelet", "kubernetes-cp"):
            versions[name] = cast(str, entry["version"])
    return versions


def lb_service_candidates() -> set[str]:
    """Derive LB service DNS candidates from the cilium/ manifests.

    Gateway objects yield the same-named Service Cilium's gateway
    implementation provisions in the Gateway namespace; explicit
    type=LoadBalancer Services yield their own names.
    """
    candidates: set[str] = set()
    cilium_dir = REPO_ROOT / "cilium"
    for path in sorted(cilium_dir.rglob("*.yaml")):
        for doc in load_docs(path):
            name = obj_name(doc)
            meta = doc.get("metadata")
            namespace = (
                str(meta.get("namespace", "default"))
                if isinstance(meta, dict)
                else "default"
            )
            spec = doc.get("spec")
            is_gateway = doc.get("kind") == "Gateway"
            is_lb_service = (
                doc.get("kind") == "Service"
                and isinstance(spec, dict)
                and spec.get("type") == "LoadBalancer"
            )
            if (is_gateway or is_lb_service) and name:
                candidates.update(
                    {
                        f"{name}.{namespace}.svc.cluster.local",
                        f"{name}.{namespace}.svc",
                        f"{name}.{namespace}",
                        name,
                    }
                )
    return candidates


def job_string_tokens(job: dict[str, object]) -> list[str]:
    """Collect all string tokens from the job pod template (recursively)."""
    tokens: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            tokens.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    spec = as_mapping(job.get("spec") or {}, "Job.spec")
    walk(spec.get("template"))
    return tokens


def fetch_kubeconfig(secret_file: Path) -> subprocess.CompletedProcess[str]:
    """Invoke scripts/fetch-kubeconfig per the contract defined above."""
    return subprocess.run(
        [str(FETCH_SCRIPT), str(secret_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


MINIMAL_KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: k8labs
  cluster:
    server: https://10.0.0.1:6443
users:
- name: admin
  user:
    token: dummy
contexts:
- name: k8labs
  context:
    cluster: k8labs
    user: admin
current-context: k8labs
"""


def write_secret_manifest(
    tmp_path: Path,
    *,
    name: str = "k8labs-kubeconfig",
    data_value: str | None = None,
    string_data_value: str | None = None,
) -> Path:
    """Write a synthetic kubeconfig Secret manifest into tmp_path."""
    secret: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": "default"},
        "type": "Opaque",
    }
    if data_value is not None:
        secret["data"] = {"value": data_value}
    if string_data_value is not None:
        secret["stringData"] = {"value": string_data_value}
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(secret), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Requirement 1: cluster-manifest
# ---------------------------------------------------------------------------


def test_cluster_yaml_parses_with_exactly_one_cluster() -> None:
    docs = load_docs(CLUSTER_YAML)
    clusters = docs_of_kind(docs, "Cluster")
    assert len(clusters) == 1, (
        f"expected exactly one Cluster object, found {len(clusters)}"
    )


def test_cluster_carries_concrete_committed_values() -> None:
    text = CLUSTER_YAML.read_text(encoding="utf-8")
    markers = TEMPLATE_MARKER_RE.findall(text)
    assert not markers, f"unsubstituted template markers remain: {markers}"
    cluster = docs_of_kind(load_docs(CLUSTER_YAML), "Cluster")[0]
    meta = as_mapping(cluster.get("metadata") or {}, "Cluster.metadata")
    assert meta.get("name") == EXPECTED_CLUSTER_NAME
    assert meta.get("namespace") == EXPECTED_NAMESPACE
    spec = as_mapping(cluster.get("spec") or {}, "Cluster.spec")
    topology = as_mapping(spec.get("topology") or {}, "Cluster.spec.topology")
    assert topology.get("class") == EXPECTED_CLUSTERCLASS
    control_plane = as_mapping(
        topology.get("controlPlane") or {}, "topology.controlPlane"
    )
    assert control_plane.get("replicas") == EXPECTED_CP_REPLICAS
    workers = as_mapping(topology.get("workers") or {}, "topology.workers")
    deployments = cast(
        list[dict[str, object]],
        workers.get("machineDeployments") or [],
    )
    assert deployments, "topology.workers.machineDeployments is empty"
    md = deployments[0]
    assert md.get("class") == EXPECTED_WORKER_CLASS
    assert md.get("replicas") == EXPECTED_WORKER_REPLICAS


def test_cluster_metadata_labels_are_strings() -> None:
    """The Cluster must carry labels for CRS clusterSelectors to match."""
    cluster = docs_of_kind(load_docs(CLUSTER_YAML), "Cluster")[0]
    meta = as_mapping(cluster.get("metadata") or {}, "Cluster.metadata")
    labels = meta.get("labels")
    assert isinstance(labels, dict) and labels, (
        "Cluster carries no metadata.labels; CRS clusterSelectors cannot match"
    )
    for key, value in labels.items():
        assert isinstance(key, str) and isinstance(value, str), (
            f"label {key!r}={value!r} is not a string pair"
        )


# ---------------------------------------------------------------------------
# Requirement 2 (selector half): crs-selector-match
# ---------------------------------------------------------------------------


def _cluster_labels() -> dict[str, str]:
    cluster = docs_of_kind(load_docs(CLUSTER_YAML), "Cluster")[0]
    meta = as_mapping(cluster.get("metadata") or {}, "Cluster.metadata")
    labels = meta.get("labels") or {}
    assert isinstance(labels, dict)
    return {str(k): str(v) for k, v in labels.items()}


def _addon_yaml_files(directory: Path) -> list[Path]:
    assert directory.is_dir(), f"{directory} does not exist yet"
    files = sorted(directory.glob("*.yaml"))
    assert files, f"{directory} contains no YAML files"
    return files


def _addon_crs_files() -> list[Path]:
    return _addon_yaml_files(ADDONS_DIR)


def test_label_selector_match_expressions_semantics() -> None:
    """Pure-logic self-test of the selector evaluator used above.

    LEGITIMATELY PASSES BEFORE IMPLEMENTATION: it exercises only
    ``label_selector_matches`` against fixed vectors and touches no capi/
    asset. It exists so selector-semantics bugs cannot hide behind a
    coincidental empty-selector match elsewhere in this module.
    """
    vectors: list[tuple[dict[str, object], dict[str, str], bool]] = [
        ({"matchLabels": {"a": "b"}}, {"a": "b"}, True),
        ({"matchLabels": {"a": "b"}}, {"a": "c"}, False),
        ({"matchLabels": {"a": "b"}}, {}, False),
        (
            {
                "matchExpressions": [
                    {"key": "a", "operator": "In", "values": ["x", "y"]}
                ]
            },
            {"a": "y"},
            True,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "In", "values": ["x"]}]},
            {"b": "x"},
            False,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["x"]}]},
            {"a": "y"},
            True,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["x"]}]},
            {"a": "x"},
            False,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["x"]}]},
            {},
            True,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "Exists"}]},
            {"a": "1"},
            True,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "Exists"}]},
            {},
            False,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "DoesNotExist"}]},
            {},
            True,
        ),
        (
            {"matchExpressions": [{"key": "a", "operator": "DoesNotExist"}]},
            {"a": "1"},
            False,
        ),
        (
            {
                "matchLabels": {"tier": "edge"},
                "matchExpressions": [
                    {"key": "env", "operator": "In", "values": ["prod", "staging"]}
                ],
            },
            {"tier": "edge", "env": "prod"},
            True,
        ),
        (
            {
                "matchLabels": {"tier": "edge"},
                "matchExpressions": [
                    {"key": "env", "operator": "In", "values": ["prod"]}
                ],
            },
            {"tier": "edge", "env": "dev"},
            False,
        ),
    ]
    for selector, labels, expected in vectors:
        assert label_selector_matches(selector, labels) is expected, (
            f"selector={selector} labels={labels} expected={expected}"
        )


def test_every_crs_selector_matches_cluster_labels() -> None:
    labels = _cluster_labels()
    for path in _addon_crs_files():
        for crs in docs_of_kind(load_docs(path), "ClusterResourceSet"):
            name = obj_name(crs)
            spec = as_mapping(crs.get("spec") or {}, f"CRS {name}.spec")
            selector = spec.get("clusterSelector")
            assert isinstance(selector, dict) and selector, (
                f"CRS {name!r} ({path.name}): missing/empty clusterSelector"
            )
            assert label_selector_matches(cast(dict[str, object], selector), labels), (
                f"CRS {name!r} ({path.name}): clusterSelector {selector} does "
                f"not match Cluster labels {labels}"
            )


def test_addons_cover_all_three_addon_sets() -> None:
    seen: set[str] = set()
    for path in _addon_crs_files():
        for crs in docs_of_kind(load_docs(path), "ClusterResourceSet"):
            name = obj_name(crs)
            dirname = crs_to_repo_dir(name)
            assert dirname is not None, (
                f"CRS {name!r} ({path.name}) matches none of the expected "
                f"addon tokens {sorted(CRS_DIR_MAP)}"
            )
            seen.add(dirname)
    assert seen == set(CRS_DIR_MAP.values()), (
        f"missing CRS coverage for addon dirs "
        f"{sorted(set(CRS_DIR_MAP.values()) - seen)}"
    )


# ---------------------------------------------------------------------------
# Requirement 2 (payload half): crs-payload-fidelity
# ---------------------------------------------------------------------------


def test_crs_resources_reference_existing_secrets() -> None:
    secrets_by_name: set[str] = set()
    for path in _addon_crs_files():
        for secret in docs_of_kind(load_docs(path), "Secret"):
            name = obj_name(secret)
            assert name not in secrets_by_name, (
                f"duplicate Secret name {name!r} across capi/addons/"
            )
            secrets_by_name.add(name)
    for path in _addon_crs_files():
        for crs in docs_of_kind(load_docs(path), "ClusterResourceSet"):
            name = obj_name(crs)
            spec = as_mapping(crs.get("spec") or {}, f"CRS {name}.spec")
            resources = cast(list[dict[str, object]], spec.get("resources") or [])
            assert resources, f"CRS {name!r} references no resources"
            for resource in resources:
                assert resource.get("kind") == "Secret", (
                    f"CRS {name!r}: resource {resource!r} is not kind Secret"
                )
                ref = resource.get("name")
                assert ref in secrets_by_name, (
                    f"CRS {name!r}: referenced Secret {ref!r} not found in {ADDONS_DIR}"
                )


def test_resource_secrets_use_resource_set_type() -> None:
    for path in _addon_crs_files():
        for secret in docs_of_kind(load_docs(path), "Secret"):
            name = obj_name(secret)
            assert secret.get("type") == RESOURCE_SET_SECRET_TYPE, (
                f"Secret {name!r} ({path.name}): type must be "
                f"{RESOURCE_SET_SECRET_TYPE}, got {secret.get('type')!r}"
            )


def test_secret_payloads_normalized_equal_repo_manifests() -> None:
    """Payload rule: normalized YAML-equal (see module docstring).

    REVISION-R2-TESTFIX2: payload keys are resolved through the inverse of
    the canonical dash-key rule (repo path relative to the mapped directory
    with every ``/`` replaced by ``--``, cf. ``canonical_install_key``):
    nested manifests such as ``cilium/install/**`` sit under dash-keys, so
    membership and file resolution must map each key back to its repo path
    first. Content fidelity is unchanged -- every payload key must resolve
    to exactly one real repo file and be normalized-YAML-equal to it.
    """
    for path in _addon_crs_files():
        for crs in docs_of_kind(load_docs(path), "ClusterResourceSet"):
            crs_name = obj_name(crs)
            dirname = crs_to_repo_dir(crs_name)
            assert dirname is not None
            repo_dir = REPO_ROOT / dirname
            repo_top_level = sorted(p.name for p in repo_dir.glob("*.yaml"))
            # Canonical dash-key -> slash-form repo-relative path (inverse
            # of the canonical_install_key rule). Asserted injective so
            # every payload key resolves to exactly one real repo file.
            key_to_relpath: dict[str, str] = {}
            for p in sorted(repo_dir.rglob("*.yaml")):
                rel = str(p.relative_to(repo_dir))
                canonical = rel.replace("/", "--")
                assert canonical not in key_to_relpath, (
                    f"{dirname}/: canonical key {canonical!r} maps to both "
                    f"{key_to_relpath[canonical]!r} and {rel!r}"
                )
                key_to_relpath[canonical] = rel
            spec = as_mapping(crs.get("spec") or {}, f"CRS {crs_name}.spec")
            resources = cast(list[dict[str, object]], spec.get("resources") or [])
            for resource in resources:
                ref = str(resource.get("name"))
                secret_path = ADDONS_DIR / f"{ref}.yaml"
                matches = [
                    doc
                    for doc in load_docs(secret_path)
                    if doc.get("kind") == "Secret" and obj_name(doc) == ref
                ]
                assert matches, f"referenced Secret {ref!r} missing at red phase"
                payloads = secret_payloads(matches[0])
                keys = sorted(payloads)
                for key in keys:
                    assert key in key_to_relpath, (
                        f"CRS {crs_name!r}: Secret {ref!r} embeds {key!r} "
                        f"which does not map to any manifest under {dirname}/"
                    )
                missing = [f for f in repo_top_level if f not in keys]
                assert not missing, (
                    f"CRS {crs_name!r}: Secret {ref!r} does not embed "
                    f"top-level {dirname}/ manifest(s) {missing}"
                )
                for key in keys:
                    rel = key_to_relpath[key]
                    repo_file = repo_dir / rel
                    assert repo_file.is_file(), (
                        f"CRS {crs_name!r}: embedded key {key!r} resolves to "
                        f"non-file {repo_file}"
                    )
                    assert normalized_yaml_equal(payloads[key], repo_file), (
                        f"CRS {crs_name!r}: Secret {ref!r} payload {key!r} is "
                        f"not normalized-YAML-equal to {dirname}/{rel}"
                    )


def canonical_install_key(install_file: Path) -> str:
    """Canonical Secret data key for a cilium/install/ manifest.

    REVISION-R2-TESTFIX: the file's repo path relative to ``cilium/`` with
    every ``/`` replaced by ``--`` (e.g. ``install/00-crds/foo.yaml`` ->
    ``install--00-crds--foo.yaml``). Kubernetes rejects ``/`` in Secret
    data keys (IsConfigMapKey validation), so raw repo-relative paths are
    unusable as keys; the dash form is slash-free and injective over the
    committed tree.
    """
    return str(install_file.relative_to(REPO_ROOT / "cilium")).replace("/", "--")


def test_cilium_crs_embeds_install_manifests_exactly_once() -> None:
    """The cilium CRS payloads must also carry cilium/install/ (recursive).

    REVIEW-R1-TESTFIX: top-level-only coverage left out cilium/install/
    (Cilium agent manifests plus the CRD bundle); without them the
    CRS-only delivery flow cannot function -- Gateway/lb-pool/l2-policy
    resources would reference nonexistent CRDs. Kubernetes caps a single
    Secret at ~1MiB and the install tree exceeds that, so the contract
    cannot demand one Secret; it is SECRET-GROUPING AGNOSTIC instead:
    every ``*.yaml`` file under ``cilium/install/`` must appear EXACTLY
    ONCE across ALL Secrets referenced by the cilium CRS combined
    (spec.resources accepts multiple refs).

    REVISION-R2-TESTFIX: "appears" is KEY-PINNED -- the payload must sit
    under its canonical mapped key (repo path relative to ``cilium/`` with
    every ``/`` replaced by ``--``, see ``canonical_install_key``) AND be
    normalized-YAML-equal to the whole repo file. Slash-containing keys
    fail apiserver validation, so key spelling is part of the contract;
    which Secret carries a given canonical key remains free.
    """
    repo_dir = REPO_ROOT / "cilium"
    install_root = repo_dir / "install"
    assert install_root.is_dir(), f"{install_root} does not exist yet"
    install_files = sorted(install_root.rglob("*.yaml"))
    assert install_files, f"{install_root} contains no YAML files"

    # Guard the canonical key-mapping rule itself: it must stay injective
    # over the actual tree (two distinct paths may never map to one key)
    # and unambiguous (no path component may itself contain "--").
    canonical_keys = [canonical_install_key(f) for f in install_files]
    assert len(set(canonical_keys)) == len(canonical_keys), (
        "canonical install key mapping is not injective over "
        f"{install_root}: duplicate key(s) "
        f"{sorted(k for k in set(canonical_keys) if canonical_keys.count(k) > 1)}"
    )
    ambiguous_parts = sorted(
        {
            part
            for f in install_files
            for part in f.relative_to(repo_dir).parts
            if "--" in part
        }
    )
    assert not ambiguous_parts, (
        f"cilium/install/ path component(s) {ambiguous_parts} contain '--', "
        "making the canonical slash-to-dash key mapping ambiguous"
    )

    # Payload entries from every Secret referenced by any cilium-mapped CRS.
    entries: list[tuple[str, str, bytes]] = []
    seen_refs: set[str] = set()
    for path in _addon_crs_files():
        for crs in docs_of_kind(load_docs(path), "ClusterResourceSet"):
            crs_name = obj_name(crs)
            if crs_to_repo_dir(crs_name) != "cilium":
                continue
            spec = as_mapping(crs.get("spec") or {}, f"CRS {crs_name}.spec")
            resources = cast(list[dict[str, object]], spec.get("resources") or [])
            assert resources, f"CRS {crs_name!r} references no resources"
            for resource in resources:
                ref = str(resource.get("name"))
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                secret_path = ADDONS_DIR / f"{ref}.yaml"
                matches = [
                    doc
                    for doc in load_docs(secret_path)
                    if doc.get("kind") == "Secret" and obj_name(doc) == ref
                ]
                assert matches, f"referenced Secret {ref!r} missing at red phase"
                for key, payload in sorted(secret_payloads(matches[0]).items()):
                    entries.append((ref, key, payload))

    problems: list[str] = []
    for install_file in install_files:
        rel = str(install_file.relative_to(repo_dir))
        expected_key = canonical_install_key(install_file)
        holders = [
            f"Secret {secret!r} key {key!r}"
            for secret, key, payload in entries
            if key == expected_key and normalized_yaml_equal(payload, install_file)
        ]
        if len(holders) != 1:
            detail = f"; carried by {'; '.join(holders)}" if holders else ""
            misplaced = [
                f"Secret {secret!r} key {key!r}"
                for secret, key, payload in entries
                if key != expected_key and normalized_yaml_equal(payload, install_file)
            ]
            if misplaced:
                detail += (
                    "; matching content sits under non-canonical key(s): "
                    + "; ".join(misplaced)
                )
            problems.append(
                f"cilium/{rel}: expected exactly 1 payload under canonical "
                f"key {expected_key!r} across all Secrets referenced by the "
                f"cilium CRS, found {len(holders)}{detail}"
            )
    assert not problems, (
        "cilium CRS payloads do not cover cilium/install/ exactly-once "
        "under canonical keys:\n" + "\n".join(problems)
    )


# ---------------------------------------------------------------------------
# Requirement 3: smoke-job
# ---------------------------------------------------------------------------


def test_smoke_job_parses_single_job() -> None:
    docs = load_docs(SMOKE_JOB)
    jobs = docs_of_kind(docs, "Job")
    assert len(jobs) == 1, f"expected exactly one Job, found {len(jobs)}"


def test_smoke_job_is_namespace_scoped_only() -> None:
    for index, doc in enumerate(load_docs(SMOKE_JOB)):
        kind = doc.get("kind")
        assert kind in NAMESPACE_SCOPED_KINDS, (
            f"smoke-test stream doc #{index}: kind {kind!r} is not in the "
            f"namespace-scoped allowlist {sorted(NAMESPACE_SCOPED_KINDS)}"
        )
        assert kind not in CLUSTER_SCOPED_DENYLIST, (
            f"smoke-test stream doc #{index}: cluster-scoped kind {kind!r}"
        )


def test_smoke_job_probes_lb_service_dns_from_cilium_config() -> None:
    candidates = lb_service_candidates()
    job = docs_of_kind(load_docs(SMOKE_JOB), "Job")[0]
    tokens = job_string_tokens(job)

    if not candidates:
        # Documented fallback: no LB source derivable from cilium/;
        # the Job must carry the placeholder constant instead.
        assert any(LB_HOST_PLACEHOLDER in token for token in tokens), (
            f"no LB candidates derivable from cilium/ and the Job does not "
            f"mention placeholder {LB_HOST_PLACEHOLDER}"
        )
        return

    referenced = {c for c in candidates if any(c in token for token in tokens)}
    assert referenced, (
        f"Job references none of the cilium-derived LB service names "
        f"{sorted(candidates)}; command/args/env tokens scanned: "
        f"{len(tokens)}"
    )


def test_smoke_job_structural_sanity() -> None:
    job = docs_of_kind(load_docs(SMOKE_JOB), "Job")[0]
    spec = as_mapping(job.get("spec") or {}, "Job.spec")
    backoff = spec.get("backoffLimit", 6)
    assert isinstance(backoff, int) and backoff >= 0
    template = as_mapping(spec.get("template") or {}, "Job.spec.template")
    pod_spec = as_mapping(template.get("spec") or {}, "Job.spec.template.spec")
    assert pod_spec.get("restartPolicy") in ("Never", "OnFailure"), (
        f"Job restartPolicy must be Never or OnFailure, got "
        f"{pod_spec.get('restartPolicy')!r}"
    )
    containers = cast(list[dict[str, object]], pod_spec.get("containers") or [])
    assert containers, "Job pod template has no containers"
    for container in containers:
        assert container.get("image"), (
            f"container {container.get('name')!r} has no image"
        )


# ---------------------------------------------------------------------------
# Requirement 4: kubeconfig-fetch-contract (interface DEFINED here)
# ---------------------------------------------------------------------------


def test_fetch_kubeconfig_decodes_data_value_to_valid_config(
    tmp_path: Path,
) -> None:
    encoded = base64.b64encode(MINIMAL_KUBECONFIG.encode("utf-8")).decode("ascii")
    secret = write_secret_manifest(tmp_path, data_value=encoded)
    proc = fetch_kubeconfig(secret)
    assert proc.returncode == 0, f"exit={proc.returncode} stderr={proc.stderr!r}"
    config = as_mapping(yaml.safe_load(proc.stdout), "stdout")
    assert config.get("apiVersion") == "v1"
    assert config.get("kind") == "Config"


def test_fetch_kubeconfig_accepts_stringdata_secret(tmp_path: Path) -> None:
    secret = write_secret_manifest(tmp_path, string_data_value=MINIMAL_KUBECONFIG)
    proc = fetch_kubeconfig(secret)
    assert proc.returncode == 0, f"exit={proc.returncode} stderr={proc.stderr!r}"
    config = as_mapping(yaml.safe_load(proc.stdout), "stdout")
    assert config.get("kind") == "Config"


def test_fetch_kubeconfig_missing_value_key_fails(tmp_path: Path) -> None:
    secret = write_secret_manifest(tmp_path)
    proc = fetch_kubeconfig(secret)
    assert proc.returncode != 0, "missing 'value' key must exit non-zero"
    assert proc.stderr.strip(), "failure must diagnose on stderr"


def test_fetch_kubeconfig_invalid_base64_fails(tmp_path: Path) -> None:
    secret = write_secret_manifest(tmp_path, data_value="!!!not-base64!!!")
    proc = fetch_kubeconfig(secret)
    assert proc.returncode != 0, "invalid base64 must exit non-zero"
    assert proc.stderr.strip(), "failure must diagnose on stderr"


def test_fetch_kubeconfig_non_config_payload_fails(tmp_path: Path) -> None:
    not_a_kubeconfig = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n"
    encoded = base64.b64encode(not_a_kubeconfig.encode("utf-8")).decode("ascii")
    secret = write_secret_manifest(tmp_path, data_value=encoded)
    proc = fetch_kubeconfig(secret)
    assert proc.returncode != 0, "decoded kind!=Config must exit non-zero"
    assert proc.stderr.strip(), "failure must diagnose on stderr"


def test_fetch_kubeconfig_empty_input_fails(tmp_path: Path) -> None:
    encoded = base64.b64encode(b"").decode("ascii")
    secret = write_secret_manifest(tmp_path, data_value=encoded)
    proc = fetch_kubeconfig(secret)
    assert proc.returncode != 0, "empty decoded payload must exit non-zero"


# ---------------------------------------------------------------------------
# Requirement 5: version-pin-consistency
# ---------------------------------------------------------------------------


def test_extensions_pin_source_self_consistent() -> None:
    """Guard the pin source itself against ambiguity.

    LEGITIMATELY PASSES BEFORE IMPLEMENTATION: it reads only
    extensions/manifest.yaml, which predates capi/. It fails if the
    kubelet and kubernetes-cp sysext pins ever diverge, which would make
    "the pinned Kubernetes version" ill-defined for the consistency check
    against capi/cluster.yaml.
    """
    versions = pinned_k8s_versions()
    assert set(versions) == {"kubelet", "kubernetes-cp"}, (
        f"pin extraction incomplete: found {sorted(versions)}"
    )
    assert versions["kubelet"] == versions["kubernetes-cp"], (
        f"kubelet pin {versions['kubelet']} != kubernetes-cp pin "
        f"{versions['kubernetes-cp']}; the pinned k8s version is ambiguous"
    )


def test_cluster_version_equals_extensions_pin() -> None:
    pin = pinned_k8s_versions()["kubelet"]
    cluster = docs_of_kind(load_docs(CLUSTER_YAML), "Cluster")[0]
    spec = as_mapping(cluster.get("spec") or {}, "Cluster.spec")
    topology = as_mapping(spec.get("topology") or {}, "Cluster.spec.topology")
    assert topology.get("version") == pin, (
        f"capi/cluster.yaml topology.version {topology.get('version')!r} != "
        f"extensions pin {pin!r}"
    )


# ---------------------------------------------------------------------------
# Requirement 6: offline-validation (pure structural assertions)
# ---------------------------------------------------------------------------


def _iter_capi_yaml_files() -> list[Path]:
    assert CAPI_DIR.is_dir(), f"{CAPI_DIR} does not exist yet"
    files = sorted(CAPI_DIR.rglob("*.yaml"))
    assert files, f"{CAPI_DIR} contains no YAML files"
    return files


def test_all_capi_manifests_structurally_valid_offline() -> None:
    """Offline method choice documented in the module docstring."""
    for path in _iter_capi_yaml_files():
        rel = str(path.relative_to(REPO_ROOT))
        for index, doc in enumerate(load_docs(path)):
            where = f"{rel}#{index}"
            api_version = doc.get("apiVersion")
            kind = doc.get("kind")
            assert isinstance(api_version, str) and api_version, (
                f"{where}: missing apiVersion"
            )
            assert isinstance(kind, str) and kind, f"{where}: missing kind"
            meta = as_mapping(doc.get("metadata"), f"{where}.metadata")
            name = meta.get("name")
            assert isinstance(name, str) and DNS_SUBDOMAIN_RE.fullmatch(name), (
                f"{where}: metadata.name {name!r} violates DNS-1123 rules"
            )
            if kind == "Secret":
                assert isinstance(doc.get("type"), str), f"{where}: Secret without type"
            if kind == "Service":
                spec = as_mapping(doc.get("spec") or {}, f"{where}.spec")
                ports = spec.get("ports")
                assert ports is None or isinstance(ports, list), (
                    f"{where}: Service ports must be a list"
                )
            if kind == "Job":
                spec = as_mapping(doc.get("spec") or {}, f"{where}.spec")
                template = as_mapping(
                    spec.get("template") or {}, f"{where}.spec.template"
                )
                pod_spec = as_mapping(
                    template.get("spec") or {}, f"{where}.spec.template.spec"
                )
                containers = pod_spec.get("containers")
                assert isinstance(containers, list) and containers, (
                    f"{where}: Job without containers"
                )


def test_no_unsubstituted_template_markers_in_capi_tree() -> None:
    """All values are committed concretely across every capi/ manifest."""
    for path in _iter_capi_yaml_files():
        text = path.read_text(encoding="utf-8")
        markers = TEMPLATE_MARKER_RE.findall(text)
        assert not markers, (
            f"{path.relative_to(REPO_ROOT)}: unsubstituted markers {markers}"
        )
