"""Work around the pinned provider image dialing ``127.0.0.1``.

The pinned ``cluster-api-hypervisor`` image was built before
``21ac0d1 fix: use published management endpoint``. It probes the workload
apiserver at the literal address ``127.0.0.1:<published host port>``, which
inside the provider Pod is the Pod's own loopback, so cluster provisioning
never completes and no ``<cluster>-kubeconfig`` Secret is published.

Until a provider image built from the fixed source is released and pinned, this
script installs a ``loopback-forward`` sidecar in the provider Deployment. The
sidecar listens on the Pod loopback for the published apiserver and etcd ports
and forwards them to the host gateway, where k8netd publishes them.

The host ports are allocated per cluster, so they are read from the
HypervisorMachine's ``status.publishedPorts`` rather than hardcoded. The script
is idempotent: re-running replaces the sidecar with the current ports.

Remove this once ``versions.lock.yaml`` pins a provider image that contains
``21ac0d1``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

DEFAULT_CONTEXT = "kind-k8labs-mgmt"
HOST_GATEWAY = "169.254.1.2"
SIDECAR_NAME = "loopback-forward"
SIDECAR_IMAGE = "docker.io/alpine/socat:latest"
# vmPorts the provider dials over the pod loopback: the workload apiserver and
# the etcd client endpoint it snapshots.
LOOPBACK_VM_PORTS = (6443, 2379)


def build_socat_command(host_by_vm_port: dict[int, int]) -> list[str]:
    """Return the sidecar command forwarding each loopback port to the host."""

    forwards = []
    for vm_port in LOOPBACK_VM_PORTS:
        host_port = host_by_vm_port.get(vm_port)
        if host_port is None:
            raise ValueError(f"no published host port for vmPort {vm_port}")
        forwards.append(
            f"socat TCP-LISTEN:{host_port},fork,reuseaddr "
            f"TCP:{HOST_GATEWAY}:{host_port}"
        )
    return ["sh", "-c", " & ".join(forwards) + " & wait"]


def _kubectl(context: str, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--context", context, *args],
        capture_output=True,
        text=True,
        input=input_text,
    )
    if result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def hosted_ports(context: str, namespace: str) -> dict[int, int]:
    """Map vmPort -> hostPort from the control-plane HypervisorMachine."""

    raw = _kubectl(context, "get", "hypervisormachines", "-n", namespace, "-o", "json")
    machines = json.loads(raw)["items"]
    candidates = [m for m in machines if m.get("status", {}).get("publishedPorts")]
    if not candidates:
        raise SystemExit(
            "no HypervisorMachine has publishedPorts yet; the control-plane VM "
            "has not been provisioned"
        )
    if len(candidates) > 1:
        raise SystemExit(
            "multiple HypervisorMachines publish ports; this workaround assumes "
            "the single-cluster lab layout"
        )
    return {
        entry["vmPort"]: entry["hostPort"]
        for entry in candidates[0]["status"]["publishedPorts"]
    }


def apply_sidecar(
    context: str, namespace: str, deployment: str, command: list[str]
) -> None:
    sidecar = {"name": SIDECAR_NAME, "image": SIDECAR_IMAGE, "command": command}
    patch = {
        "spec": {
            "template": {
                "spec": {
                    # Strategic merge keys list containers by name, so this
                    # replaces the sidecar in place instead of duplicating it.
                    "containers": [sidecar]
                }
            }
        }
    }
    _kubectl(
        context,
        "-n",
        namespace,
        "patch",
        "deployment",
        deployment,
        "--type=strategic",
        "--patch-file",
        "/dev/stdin",
        input_text=json.dumps(patch),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    parser.add_argument("--namespace", default="hypervisor-system")
    parser.add_argument("--deployment", default="controller-manager")
    parser.add_argument("--cluster-namespace", default="default")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ports = hosted_ports(args.context, args.cluster_namespace)
    command = build_socat_command(ports)
    forwards = command[2]
    if args.dry_run:
        print(forwards)
        return 0

    apply_sidecar(args.context, args.namespace, args.deployment, command)
    _kubectl(
        args.context,
        "-n",
        args.namespace,
        "rollout",
        "status",
        f"deployment/{args.deployment}",
        "--timeout=300s",
    )
    print(f"applied {SIDECAR_NAME} sidecar forwarding: {forwards}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
