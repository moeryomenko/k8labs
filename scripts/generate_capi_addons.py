#!/usr/bin/env python3
"""Generate CAPI addon resource-set Secrets from committed source manifests.

This generator reads the committed addon source trees (``cilium/``,
``coredns/``, ``rbac/``) and produces deterministic Kubernetes Secret
manifests of type ``addons.cluster.x-k8s.io/resource-set``.  Cilium payloads
are split across multiple Secrets when the encoded size of the top-level
manifests plus the ``cilium/install/**`` tree exceeds the per-Secret size
limit (Kubernetes caps a single Secret at ~1 MiB).

The output is written to ``build/capi-addons/`` (or a caller-supplied
directory).  ``capi/addons/`` retains only the three ``ClusterResourceSet``
definitions.

Usage::

    python3 scripts/generate_capi_addons.py \
        --repo-root /path/to/repo \
        --output-dir /path/to/output \
        [--cilium-limit 921600] \
        [--check]
"""

from __future__ import annotations

import argparse
import base64
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ManifestEntry(NamedTuple):
    """A single source manifest to embed in a Secret."""

    key: str
    """Canonical Secret data key (slash-free, e.g. ``install--00-crds--foo.yaml``)."""

    path: Path
    """Absolute path to the source YAML file."""

    content: bytes
    """Raw file bytes (not yet base64-encoded)."""

    encoded_size: int
    """Length of the base64-encoded payload in characters."""


@dataclass
class SecretGroup:
    """A collection of manifests that fit into one Kubernetes Secret."""

    name: str
    """The Secret's metadata.name."""

    entries: list[ManifestEntry]
    """The manifests carried by this Secret."""

    encoded_size: int
    """Total encoded payload size (sum of entries' encoded_size)."""


@dataclass
class GeneratedSecret:
    """A fully assembled Secret manifest ready for YAML serialization."""

    name: str
    """The Secret's metadata.name."""

    data: dict[str, str]
    """Mapping of canonical key -> base64-encoded payload."""

    namespace: str = "default"
    """Kubernetes namespace."""

    secret_type: str = "addons.cluster.x-k8s.io/resource-set"
    """Kubernetes Secret type."""


# ---------------------------------------------------------------------------
# Discovery and key mapping
# ---------------------------------------------------------------------------

DEFAULT_NAMESPACE = "default"
RESOURCE_SET_TYPE = "addons.cluster.x-k8s.io/resource-set"
CILIUM_BASE_NAME = "cilium-manifests"
COREDNS_BASE_NAME = "coredns-manifests"
RBAC_BASE_NAME = "rbac-manifests"


def canonical_key(rel_path: Path) -> str:
    """Convert a repo-relative path to a canonical Secret data key.

    Top-level filenames remain their basename.  Nested paths have every
    ``/`` replaced by ``--`` (e.g. ``install/00-crds/foo.yaml`` ->
    ``install--00-crds--foo.yaml``).
    """
    return str(rel_path).replace("/", "--")


def discover_manifests(
    repo_root: Path,
    addon_dir: str,
    *,
    recursive: bool = False,
) -> list[ManifestEntry]:
    """Discover ``*.yaml`` source files under ``addon_dir``.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    addon_dir:
        Directory name relative to ``repo_root`` (e.g. ``"cilium"``).
    recursive:
        If True, also discover ``*.yaml`` files under subdirectories.

    Returns
    -------
    list of ManifestEntry, sorted by canonical key for deterministic output.
    """
    addon_root = repo_root / addon_dir
    if not addon_root.is_dir():
        raise FileNotFoundError(f"addon directory not found: {addon_root}")

    entries: list[ManifestEntry] = []

    if recursive:
        yaml_files = sorted(addon_root.rglob("*.yaml"))
    else:
        yaml_files = sorted(addon_root.glob("*.yaml"))

    if not yaml_files:
        raise ValueError(f"no YAML files found under {addon_root}")

    for path in yaml_files:
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        # Key is relative to the addon directory (not the repo root).
        rel_to_addon = path.relative_to(addon_root)
        key = canonical_key(rel_to_addon)

        content = path.read_bytes()
        encoded = base64.b64encode(content)
        entries.append(
            ManifestEntry(
                key=key,
                path=path,
                content=content,
                encoded_size=len(encoded),
            )
        )

    # Sort by key for deterministic output.
    entries.sort(key=lambda e: e.key)

    # Guard against duplicate keys.
    keys = [e.key for e in entries]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise ValueError(f"duplicate canonical keys under {addon_dir}: {dupes}")

    return entries


# ---------------------------------------------------------------------------
# Size-based splitting (first-fit greedy)
# ---------------------------------------------------------------------------


def compute_default_threshold(
    entries: list[ManifestEntry],
    existing_secrets: list[Path] | None = None,
) -> int:
    """Compute the default per-Secret encoded size threshold.

    The threshold is the largest existing generated Cilium payload (from
    ``existing_secrets``) plus 10%, capped below Kubernetes' ~1 MiB limit
    (1048576 bytes).  If no existing secrets are available, a conservative
    default of 900 KiB (921600 bytes) is used.
    """
    limit = 1024 * 1024  # 1 MiB

    if existing_secrets:
        max_payload = 0
        for secret_path in existing_secrets:
            text = secret_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") or not stripped:
                    continue
                # Look for base64 data values (long lines with base64 chars).
                if len(stripped) > 200 and any(
                    c in stripped
                    for c in "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYyZz0123456789+/="
                ):
                    if len(stripped) > max_payload:
                        max_payload = len(stripped)
        if max_payload > 0:
            threshold = int(max_payload * 1.10)
            return min(threshold, limit - 4096)

    return 900 * 1024  # 900 KiB


def split_by_size(
    entries: list[ManifestEntry],
    base_name: str,
    threshold: int,
) -> list[SecretGroup]:
    """Split entries into Secret groups using first-fit greedy bin-packing.

    Entries are sorted by key before packing to ensure deterministic output.
    """
    sorted_entries = sorted(entries, key=lambda e: e.key)

    groups: list[SecretGroup] = []
    current_group: SecretGroup | None = None

    for entry in sorted_entries:
        if entry.encoded_size > threshold:
            raise ValueError(
                f"manifest {entry.path} has encoded size {entry.encoded_size} "
                f"which exceeds the per-Secret limit of {threshold} bytes; "
                f"cannot split a single file"
            )

        if (
            current_group is None
            or current_group.encoded_size + entry.encoded_size > threshold
        ):
            current_group = SecretGroup(
                name=f"{base_name}-{len(groups) + 1}",
                entries=[],
                encoded_size=0,
            )
            groups.append(current_group)

        current_group.entries.append(entry)
        current_group.encoded_size += entry.encoded_size

    return groups


# ---------------------------------------------------------------------------
# Secret assembly
# ---------------------------------------------------------------------------


def assemble_secret(
    group: SecretGroup, namespace: str = DEFAULT_NAMESPACE
) -> GeneratedSecret:
    """Assemble a GeneratedSecret from a SecretGroup."""
    data: dict[str, str] = {}
    for entry in group.entries:
        encoded = base64.b64encode(entry.content).decode("ascii")
        data[entry.key] = encoded
    return GeneratedSecret(
        name=group.name,
        data=data,
        namespace=namespace,
        secret_type=RESOURCE_SET_TYPE,
    )


# ---------------------------------------------------------------------------
# CRS generation
# ---------------------------------------------------------------------------


def generate_crs_manifest(
    crs_name: str,
    secret_names: list[str],
    cluster_name: str = "k8labs",
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, object]:
    """Generate a ClusterResourceSet manifest dict."""
    return {
        "apiVersion": "addons.cluster.x-k8s.io/v1beta2",
        "kind": "ClusterResourceSet",
        "metadata": {
            "name": crs_name,
            "namespace": namespace,
        },
        "spec": {
            "clusterSelector": {
                "matchLabels": {
                    "cluster.x-k8s.io/cluster-name": cluster_name,
                }
            },
            "resources": [{"kind": "Secret", "name": name} for name in secret_names],
        },
    }


# ---------------------------------------------------------------------------
# YAML emission
# ---------------------------------------------------------------------------


def serialize_secret(secret: GeneratedSecret) -> str:
    """Serialize a GeneratedSecret to a YAML string."""
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret.name,
            "namespace": secret.namespace,
        },
        "type": secret.secret_type,
        "data": {key: value for key, value in sorted(secret.data.items())},
    }
    return yaml.dump(doc, default_flow_style=False, sort_keys=False, width=200)


def serialize_crs(crs: dict[str, object]) -> str:
    """Serialize a ClusterResourceSet manifest dict to a YAML string."""
    return yaml.dump(crs, default_flow_style=False, sort_keys=False, width=200)


# ---------------------------------------------------------------------------
# Main generation logic
# ---------------------------------------------------------------------------


def generate_all(
    repo_root: Path,
    output_dir: Path,
    *,
    cilium_limit: int | None = None,
    cluster_name: str = "k8labs",
    existing_cilium_secrets: list[Path] | None = None,
    update_tracked_crs: bool = True,
) -> dict[str, list[Path]]:
    """Generate all addon resource-set Secrets.

    Parameters
    ----------
    repo_root:
        Path to the repository root.
    output_dir:
        Directory to write generated Secret and CRS manifests.
    cilium_limit:
        Maximum encoded payload size per Cilium Secret in bytes.
    cluster_name:
        Cluster name label for CRS selectors.
    existing_cilium_secrets:
        Paths to existing Cilium Secret manifests for threshold computation.
    update_tracked_crs:
        If True, also update the tracked CRS files in ``capi/addons/`` to
        reference the correct secret names (so they never drift).

    Returns a mapping of addon name to list of output file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, list[Path]] = {}

    # --- RBAC ---
    rbac_entries = discover_manifests(repo_root, "rbac")
    rbac_secret = assemble_secret(
        SecretGroup(
            name=RBAC_BASE_NAME,
            entries=rbac_entries,
            encoded_size=sum(e.encoded_size for e in rbac_entries),
        )
    )
    rbac_path = output_dir / f"{RBAC_BASE_NAME}.yaml"
    rbac_path.write_text(serialize_secret(rbac_secret), encoding="utf-8")
    results["rbac"] = [rbac_path]

    # --- CoreDNS ---
    coredns_entries = discover_manifests(repo_root, "coredns")
    coredns_secret = assemble_secret(
        SecretGroup(
            name=COREDNS_BASE_NAME,
            entries=coredns_entries,
            encoded_size=sum(e.encoded_size for e in coredns_entries),
        )
    )
    coredns_path = output_dir / f"{COREDNS_BASE_NAME}.yaml"
    coredns_path.write_text(serialize_secret(coredns_secret), encoding="utf-8")
    results["coredns"] = [coredns_path]

    # --- Cilium ---
    cilium_entries = discover_manifests(repo_root, "cilium", recursive=True)

    if cilium_limit is None:
        threshold = compute_default_threshold(cilium_entries, existing_cilium_secrets)
    else:
        threshold = cilium_limit

    cilium_groups = split_by_size(cilium_entries, CILIUM_BASE_NAME, threshold)
    cilium_paths: list[Path] = []
    for group in cilium_groups:
        secret = assemble_secret(group)
        path = output_dir / f"{secret.name}.yaml"
        path.write_text(serialize_secret(secret), encoding="utf-8")
        cilium_paths.append(path)
    results["cilium"] = cilium_paths

    # --- CRS manifests (written into output_dir for kubectl apply) ---
    for addon, paths in results.items():
        secret_names = [p.stem for p in paths]
        crs_name = f"{addon}-crs"
        crs = generate_crs_manifest(crs_name, secret_names, cluster_name)
        crs_path = output_dir / f"{crs_name}.yaml"
        crs_path.write_text(serialize_crs(crs), encoding="utf-8")

    # --- Update tracked CRS files in capi/addons/ ---
    if update_tracked_crs:
        tracked_addons_dir = repo_root / "capi" / "addons"
        for addon, paths in results.items():
            secret_names = [p.stem for p in paths]
            crs_name = f"{addon}-crs"
            crs = generate_crs_manifest(crs_name, secret_names, cluster_name)
            crs_path = tracked_addons_dir / f"{crs_name}.yaml"
            if crs_path.exists():
                crs_path.write_text(serialize_crs(crs), encoding="utf-8")

    return results


# ---------------------------------------------------------------------------
# Check mode
# ---------------------------------------------------------------------------


def check(repo_root: Path, output_dir: Path) -> bool:
    """Validate that generated output matches expected source.

    Returns True if valid, False otherwise.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        generate_all(repo_root, tmp_path)
        # Compare output files exist and are non-empty.
        for f in sorted(output_dir.glob("*.yaml")):
            expected = tmp_path / f.name
            if not expected.exists():
                print(f"FAIL: missing generated file: {f.name}", file=sys.stderr)
                return False
            if f.read_bytes() != expected.read_bytes():
                print(f"FAIL: content mismatch: {f.name}", file=sys.stderr)
                return False
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate CAPI addon resource-set Secrets from source manifests."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("build/capi-addons"),
        help="Directory to write generated Secret manifests (default: build/capi-addons)",
    )
    parser.add_argument(
        "--cilium-limit",
        type=int,
        default=None,
        help=(
            "Maximum encoded payload size per Cilium Secret in bytes "
            "(default: computed from current payloads, ~900 KiB)"
        ),
    )
    parser.add_argument(
        "--cluster-name",
        type=str,
        default="k8labs",
        help="Cluster name label for CRS selectors (default: k8labs)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: validate output matches expected source without writing",
    )
    args = parser.parse_args()

    try:
        if args.check:
            if not args.output_dir.exists():
                print(
                    "output directory does not exist; nothing to check", file=sys.stderr
                )
                return 1
            ok = check(args.repo_root, args.output_dir)
            return 0 if ok else 1

        results = generate_all(
            args.repo_root,
            args.output_dir,
            cilium_limit=args.cilium_limit,
            cluster_name=args.cluster_name,
        )

        for addon, paths in sorted(results.items()):
            print(f"Generated {addon}: {len(paths)} Secret(s)")
            for p in paths:
                print(f"  {p}")

        return 0

    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
