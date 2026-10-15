"""Offline validation of the linux-sched observer DaemonSet.

The observer runs with capabilities and host PID access, never the `privileged`
flag. These checks are a gate: `make linux-sched-observers-apply` runs them
before applying, so a manifest that quietly regains privilege, host networking,
host root, or an unbounded host path cannot reach the cluster.

Runs without a cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

OBSERVERS_DIR = Path(__file__).resolve().parent
DAEMONSET_FILE = OBSERVERS_DIR / "observer-daemonset.yaml"
ALLOWED_HOST_PATHS = {
    "/sys/kernel/tracing",
    "/sys/kernel/btf",
    "/boot",
    "/lib/modules",
    "/usr/src/kernels",
    "/var/tmp/linux-sched",
}
# /var/tmp/linux-sched is the artifact spool; it must be writable. Every other
# host path is read-only input.
WRITABLE_HOST_PATHS = {"/var/tmp/linux-sched"}
REQUIRED_CAPABILITIES = {"BPF", "PERFMON"}
CONTAINER_KEYS = ("containers", "initContainers", "ephemeralContainers")


def load(path: Path | None = None) -> list[dict]:
    docs: list[dict] = []
    for doc in yaml.safe_load_all((path or DAEMONSET_FILE).read_text(encoding="utf-8")):
        if doc:
            docs.append(doc)
    return docs


def validate(docs: list[dict] | None = None) -> list[str]:
    errors: list[str] = []
    docs = docs if docs is not None else load()

    namespaces = [d for d in docs if d.get("kind") == "Namespace"]
    daemonsets = [d for d in docs if d.get("kind") == "DaemonSet"]
    if not namespaces:
        errors.append("no Namespace manifest found")
    if not daemonsets:
        errors.append("no DaemonSet manifest found")
        return errors

    namespace_names = {d.get("metadata", {}).get("name") for d in namespaces}

    for ds in daemonsets:
        origin = ds.get("metadata", {}).get("name", "?")
        ns = ds.get("metadata", {}).get("namespace")
        if ns not in namespace_names:
            errors.append(f"{origin}: namespace {ns!r} is not one of {namespace_names}")
        spec = ds.get("spec", {}).get("template", {}).get("spec", {})

        if spec.get("hostPID") is not True:
            errors.append(f"{origin}: observer needs hostPID to see host tasks")
        if spec.get("hostNetwork") or spec.get("hostIPC"):
            errors.append(f"{origin}: observer must not use host network/IPC")
        if spec.get("automountServiceAccountToken") is not False:
            errors.append(f"{origin}: pod must disable automatic token mounting")
        if spec.get("nodeSelector", {}).get("linux-sched/research") != "observer":
            errors.append(f"{origin}: must be node-selected to research workers")

        pod_privileged = spec.get("securityContext", {}).get("privileged")
        if pod_privileged:
            errors.append(f"{origin}: pod must not be privileged")

        for volume in spec.get("volumes", []):
            host_path = volume.get("hostPath")
            if not host_path:
                continue
            path = host_path.get("path")
            if path == "/":
                errors.append(f"{origin}: volume uses hostPath /")
            elif path not in ALLOWED_HOST_PATHS:
                errors.append(
                    f"{origin}: unexpected hostPath {path!r} "
                    f"(allowed: {sorted(ALLOWED_HOST_PATHS)})"
                )

        # Map volume name -> host path so mounts can be classified.
        host_volume_paths = {
            v["name"]: v["hostPath"]["path"]
            for v in spec.get("volumes", [])
            if v.get("hostPath")
        }

        for key in CONTAINER_KEYS:
            for container in spec.get(key, []) or []:
                context = container.get("securityContext", {})
                if context.get("privileged"):
                    errors.append(f"{origin}: container in {key} is privileged")
                caps = set(context.get("capabilities", {}).get("add", []) or [])
                if key == "containers":
                    missing = REQUIRED_CAPABILITIES - caps
                    if missing:
                        errors.append(
                            f"{origin}: observer missing capabilities {sorted(missing)}"
                        )
                    if "SYS_ADMIN" not in caps:
                        errors.append(f"{origin}: observer missing SYS_ADMIN")
                for mount in container.get("volumeMounts", []):
                    if mount.get("mountPath") == "/":
                        errors.append(f"{origin}: {key} mounts host root")
                    host_path = host_volume_paths.get(mount.get("name"))
                    if host_path is None or host_path in WRITABLE_HOST_PATHS:
                        continue
                    if mount.get("readOnly") is not True:
                        errors.append(
                            f"{origin}: host mount {host_path!r} in {key} "
                            "must be readOnly"
                        )

    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("observer manifests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
