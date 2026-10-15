"""Offline validation of the linux-sched telemetry manifests.

Enforces the invariants the plan froze for the privileged observers:

* every document declares apiVersion/kind;
* each host-observing DaemonSet namespaces into a namespace that carries an
  explicit privileged PSA exception;
* every host-observing DaemonSet is node-selected to research workers;
* no container (including init and ephemeral containers) mounts the host root;
* host-path volumes are restricted to an allowlist and mounted read-only;
* observer ServiceAccounts disable automatic token mounting.

Runs without a cluster, so it is safe in the default test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

TELEMETRY_DIR = Path(__file__).resolve().parent
ALLOWED_HOST_MOUNTS = {"/proc", "/sys/kernel", "/sys/fs/cgroup", "/lib/modules"}
CONTAINER_KEYS = ("containers", "initContainers", "ephemeralContainers")


def load_documents(directory: Path | None = None) -> list[dict]:
    docs: list[dict] = []
    for path in sorted((directory or TELEMETRY_DIR).glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                doc["__file__"] = path.name
                docs.append(doc)
    return docs


def validate(docs: list[dict] | None = None) -> list[str]:
    errors: list[str] = []
    docs = docs if docs is not None else load_documents()
    if not docs:
        return ["no telemetry manifests found"]

    for doc in docs:
        origin = doc.get("__file__", "?")
        if not doc.get("apiVersion") or not doc.get("kind"):
            errors.append(f"{origin}: document missing apiVersion/kind")

    privileged_namespaces: set[str] = set()
    for ns in (d for d in docs if d.get("kind") == "Namespace"):
        labels = ns.get("metadata", {}).get("labels", {})
        if labels.get("pod-security.kubernetes.io/enforce") == "privileged":
            privileged_namespaces.add(ns.get("metadata", {}).get("name", ""))
        else:
            errors.append(f"{ns['__file__']}: namespace lacks privileged PSA exception")

    daemonsets = [d for d in docs if d.get("kind") == "DaemonSet"]
    if not daemonsets:
        errors.append("no DaemonSet manifests found")
    for ds in daemonsets:
        origin = ds["__file__"]
        namespace = ds.get("metadata", {}).get("namespace", "")
        if namespace not in privileged_namespaces:
            errors.append(
                f"{origin}: namespace {namespace!r} is not one of the validated "
                f"privileged namespaces {sorted(privileged_namespaces)}"
            )
        pod_spec = ds.get("spec", {}).get("template", {}).get("spec", {})
        if pod_spec.get("nodeSelector", {}).get("linux-sched/research") != "observer":
            errors.append(f"{origin}: DaemonSet is not node-selected to observers")
        if pod_spec.get("hostNetwork") or pod_spec.get("hostIPC"):
            errors.append(f"{origin}: DaemonSet must not use host network/IPC")
        if pod_spec.get("automountServiceAccountToken") is not False:
            errors.append(f"{origin}: pod must disable automatic token mounting")

        host_volumes = {
            volume["name"]
            for volume in pod_spec.get("volumes", [])
            if volume.get("hostPath")
        }
        for volume in pod_spec.get("volumes", []):
            host_path = volume.get("hostPath")
            if not host_path:
                continue
            path = host_path.get("path")
            if path == "/":
                errors.append(f"{origin}: volume uses hostPath /")
            elif path not in ALLOWED_HOST_MOUNTS:
                errors.append(
                    f"{origin}: unexpected hostPath {path!r} "
                    f"(allowed: {sorted(ALLOWED_HOST_MOUNTS)})"
                )

        for key in CONTAINER_KEYS:
            for container in pod_spec.get(key, []) or []:
                for mount in container.get("volumeMounts", []):
                    if mount.get("mountPath") == "/":
                        errors.append(f"{origin}: {key} mounts host root")
                    if (
                        mount.get("name") in host_volumes
                        and mount.get("readOnly") is not True
                    ):
                        errors.append(
                            f"{origin}: hostPath mount "
                            f"{mount.get('name')!r} in {key} must be readOnly"
                        )

    for sa in (d for d in docs if d.get("kind") == "ServiceAccount"):
        if sa.get("automountServiceAccountToken") is not False:
            errors.append(
                f"{sa['__file__']}: ServiceAccount must disable token automount"
            )

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("telemetry manifests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
