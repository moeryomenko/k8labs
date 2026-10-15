#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import yaml
from cache import cache_dir, validate_cache
from lock import load_lock
from normalize_manifest import normalize

HOST_ENDPOINT_NAME = "host.containers.internal"
HOST_ENDPOINT_START = "# BEGIN k8labs host API endpoint"
HOST_ENDPOINT_END = "# END k8labs host API endpoint"
PUBLISH_PORT_START = 20000
PUBLISH_PORT_END = 21000


def run(command: list[str], *, input_text: str | None = None) -> None:
    subprocess.run(command, check=True, text=True, input=input_text)


def cache_file(lock: dict[str, Any], root: Path, artifact_id: str) -> Path:
    directory = cache_dir(lock, root)
    for artifact in lock["artifacts"]:
        if artifact["id"] == artifact_id:
            return directory / artifact["cache_path"]
    raise SystemExit(f"ERROR: lock has no artifact named {artifact_id}")


def image_reference(lock: dict[str, Any], image_id: str) -> str:
    for image in lock["oci_images"]:
        if image["id"] == image_id:
            return image["reference"]
    raise SystemExit(f"ERROR: lock has no OCI image named {image_id}")


def parse_host_endpoint_address(hosts_text: str) -> str:
    addresses: set[str] = set()
    for line in hosts_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2 or HOST_ENDPOINT_NAME not in fields[1:]:
            continue
        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError as error:
            raise SystemExit(
                f"ERROR: invalid {HOST_ENDPOINT_NAME} address in Kind node hosts file"
            ) from error
        if (
            address.version != 4
            or address.is_unspecified
            or address.is_loopback
            or address.is_multicast
        ):
            raise SystemExit(
                f"ERROR: unsafe {HOST_ENDPOINT_NAME} address in Kind node hosts file"
            )
        addresses.add(str(address))

    if not addresses:
        raise SystemExit(
            f"ERROR: {HOST_ENDPOINT_NAME} mapping not found in Kind node hosts file"
        )
    if len(addresses) != 1:
        raise SystemExit(
            f"ERROR: multiple {HOST_ENDPOINT_NAME} addresses found in Kind node hosts file"
        )
    return addresses.pop()


def discover_host_endpoint_address(context: str) -> str:
    prefix = "kind-"
    if not context.startswith(prefix) or not context[len(prefix) :]:
        raise SystemExit(f"ERROR: expected a Kind context, got {context!r}")
    node = f"{context[len(prefix) :]}-control-plane"
    try:
        result = subprocess.run(
            ["podman", "exec", node, "cat", "/etc/hosts"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"ERROR: unable to read hosts file from Kind node {node!r}"
        ) from error
    return parse_host_endpoint_address(result.stdout)


def corefile_with_host_endpoint(corefile: str, host_address: str) -> str:
    try:
        address = ipaddress.ip_address(host_address)
    except ValueError as error:
        raise SystemExit(
            f"ERROR: invalid host endpoint address {host_address!r}"
        ) from error
    if (
        address.version != 4
        or address.is_unspecified
        or address.is_loopback
        or address.is_multicast
    ):
        raise SystemExit(f"ERROR: unsafe host endpoint address {host_address!r}")

    block = (
        f"{HOST_ENDPOINT_START}\n"
        "    hosts {\n"
        f"        {address} {HOST_ENDPOINT_NAME}\n"
        "        fallthrough\n"
        "    }\n"
        f"    {HOST_ENDPOINT_END}"
    )
    start = corefile.find(HOST_ENDPOINT_START)
    end = corefile.find(HOST_ENDPOINT_END)
    if (start == -1) != (end == -1) or (start != -1 and end < start):
        raise SystemExit(
            "ERROR: malformed managed host endpoint block in CoreDNS Corefile"
        )
    if start != -1:
        line_start = corefile.rfind("\n", 0, start) + 1
        line_end = corefile.find("\n", end)
        line_end = len(corefile) if line_end == -1 else line_end + 1
        return corefile[:line_start] + block + "\n" + corefile[line_end:]

    for line in corefile.splitlines():
        fields = line.split("#", 1)[0].split()
        if HOST_ENDPOINT_NAME in fields[1:]:
            if fields[0] == str(address):
                return corefile
            raise SystemExit(
                f"ERROR: conflicting unmanaged {HOST_ENDPOINT_NAME} mapping in CoreDNS Corefile"
            )
    if re.search(r"(?m)^\s*hosts(?:\s|\{)", corefile):
        raise SystemExit("ERROR: unmanaged hosts plugin in CoreDNS Corefile")

    root_server = re.search(r"(?m)^(\s*\.\:53\s*\{)\s*$", corefile)
    if root_server is None:
        raise SystemExit("ERROR: unable to locate the CoreDNS .:53 server block")
    position = root_server.end()
    return corefile[:position] + "\n" + block + corefile[position:]


def safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if not members:
            raise SystemExit("ERROR: provider source archive is empty")
        for member in members:
            target = (destination / member.name).resolve()
            if (
                destination.resolve() not in target.parents
                and target != destination.resolve()
            ):
                raise SystemExit(
                    f"ERROR: provider archive contains unsafe path: {member.name}"
                )
            if (
                member.issym()
                or member.islnk()
                or not member.isfile()
                and not member.isdir()
            ):
                raise SystemExit(
                    f"ERROR: provider archive contains unsupported member: {member.name}"
                )
        source.extractall(destination, members)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise SystemExit("ERROR: provider archive must contain exactly one source root")
    return roots[0]


def host_agent_settings(config_dir: Path) -> dict[str, str]:
    settings_file = config_dir.expanduser().resolve() / "settings.json"
    if not settings_file.is_file():
        raise SystemExit(
            f"ERROR: HostAgent settings do not exist: {settings_file}; run make host-agent-install"
        )
    loaded = json.loads(settings_file.read_text(encoding="utf-8"))
    required = {
        "agent_address",
        "server_name",
        "manager_identity",
        "tls_dir",
        "ssh_public_key",
    }
    if not isinstance(loaded, dict) or not required <= loaded.keys():
        raise SystemExit("ERROR: HostAgent settings are incomplete")
    return {key: str(value) for key, value in loaded.items()}


def apply_tls_secrets(context: str, settings: dict[str, str]) -> None:
    tls = Path(settings["tls_dir"])
    required = {
        "ca.crt": tls / "ca.crt",
        "manager.crt": tls / "manager.crt",
        "manager.key": tls / "manager.key",
        "ssh_public_key": Path(settings["ssh_public_key"]),
    }
    for name, path in required.items():
        if not path.is_file():
            raise SystemExit(f"ERROR: HostAgent TLS input missing: {name} ({path})")
    namespace = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "create",
            "namespace",
            "hypervisor-system",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    run(
        ["kubectl", "--context", context, "apply", "--server-side", "-f", "-"],
        input_text=namespace,
    )
    secret = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "hypervisor-system",
            "create",
            "secret",
            "tls",
            "k8labs-manager-agent-client",
            f"--cert={required['manager.crt']}",
            f"--key={required['manager.key']}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    run(
        ["kubectl", "--context", context, "apply", "--server-side", "-f", "-"],
        input_text=secret,
    )
    ca_secret = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "hypervisor-system",
            "create",
            "secret",
            "generic",
            "k8labs-host-agent-ca",
            f"--from-file=ca.crt={required['ca.crt']}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    run(
        ["kubectl", "--context", context, "apply", "--server-side", "-f", "-"],
        input_text=ca_secret,
    )
    ssh_secret = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "hypervisor-system",
            "create",
            "secret",
            "generic",
            "k8labs-manager-ssh-public-key",
            f"--from-file=ssh-lab.pub={required['ssh_public_key']}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    run(
        ["kubectl", "--context", context, "apply", "--server-side", "-f", "-"],
        input_text=ssh_secret,
    )


def patch_provider(source: Path, image: str, settings: dict[str, str]) -> None:
    deployment = source / "config" / "manager" / "deployment.yaml"
    if not deployment.is_file():
        raise SystemExit(
            f"ERROR: locked provider source lacks manager deployment: {deployment}"
        )
    documents = list(yaml.safe_load_all(deployment.read_text(encoding="utf-8")))
    changed = False
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        spec = document["spec"]["template"]["spec"]
        containers = spec["containers"]
        manager = next(
            (
                container
                for container in containers
                if container.get("name") == "manager"
            ),
            None,
        )
        if manager is None:
            raise SystemExit(
                "ERROR: locked provider manager deployment has no manager container"
            )
        manager["image"] = image
        args = manager.get("args")
        if not isinstance(args, list):
            raise SystemExit(
                "ERROR: locked provider manager deployment has no argument list"
            )
        replacements = {
            "--agent-address=": f"--agent-address={settings['agent_address']}:9444",
            "--agent-server-name=": f"--agent-server-name={settings['server_name']}",
            "--agent-ca=": "--agent-ca=/tls/agent-ca/ca.crt",
            "--agent-client-cert-dir=": "--agent-client-cert-dir=/tls/agent-client",
        }
        found = set()
        manager["args"] = [
            next(
                (
                    value
                    for prefix, value in replacements.items()
                    if argument.startswith(prefix)
                ),
                argument,
            )
            for argument in args
        ]
        for argument in manager["args"]:
            for prefix in replacements:
                if argument.startswith(prefix):
                    found.add(prefix)
        if found != set(replacements):
            missing = ", ".join(sorted(set(replacements) - found))
            raise SystemExit(
                f"ERROR: locked provider manager is missing agent TLS flags: {missing}"
            )
        manager["securityContext"] = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        spec["securityContext"] = {"seccompProfile": {"type": "RuntimeDefault"}}
        spec["hostAliases"] = [
            {"ip": settings["agent_address"], "hostnames": [settings["server_name"]]}
        ]
        for volume in spec.get("volumes", []):
            if volume.get("name") == "agent-client-certs":
                volume["secret"] = {"secretName": "k8labs-manager-agent-client"}
            elif volume.get("name") == "agent-ca":
                volume["secret"] = {
                    "secretName": "k8labs-host-agent-ca",
                    "items": [{"key": "ca.crt", "path": "ca.crt"}],
                }
        volumes = spec.setdefault("volumes", [])
        volumes[:] = [
            volume
            for volume in volumes
            if volume.get("name") != "manager-ssh-public-key"
        ]
        volumes.append(
            {
                "name": "manager-ssh-public-key",
                "secret": {"secretName": "k8labs-manager-ssh-public-key"},
            }
        )
        mounts = manager.setdefault("volumeMounts", [])
        mounts[:] = [
            mount for mount in mounts if mount.get("name") != "manager-ssh-public-key"
        ]
        mounts.append(
            {
                "name": "manager-ssh-public-key",
                "mountPath": "/etc/k8labs/ssh",
                "readOnly": True,
            }
        )
        environment = manager.setdefault("env", [])
        environment[:] = [
            entry
            for entry in environment
            if entry.get("name") != "HYPERVISOR_SSH_PUBLIC_KEY_FILE"
        ]
        environment.append(
            {
                "name": "HYPERVISOR_SSH_PUBLIC_KEY_FILE",
                "value": "/etc/k8labs/ssh/ssh-lab.pub",
            }
        )
        changed = True
    webhook_manifest = source / "config" / "webhook" / "manifests.yaml"
    if not webhook_manifest.is_file():
        raise SystemExit(
            f"ERROR: locked provider source lacks webhook manifest: {webhook_manifest}"
        )
    webhook_documents = list(
        yaml.safe_load_all(webhook_manifest.read_text(encoding="utf-8"))
    )
    for document in webhook_documents:
        if document.get("kind") not in {
            "MutatingWebhookConfiguration",
            "ValidatingWebhookConfiguration",
        }:
            continue
        document.setdefault("metadata", {}).setdefault("annotations", {})[
            "cert-manager.io/inject-ca-from"
        ] = "hypervisor-system/hypervisor-webhook-serving"
        for webhook in document.get("webhooks", []):
            service = webhook.get("clientConfig", {}).get("service", {})
            if service.get("name") == "webhook-service":
                service["namespace"] = "hypervisor-system"
    webhook_manifest.write_text(
        yaml.safe_dump_all(webhook_documents, sort_keys=False), encoding="utf-8"
    )
    if not changed:
        raise SystemExit("ERROR: locked provider source has no manager Deployment")
    deployment.write_text(
        yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8"
    )


def apply_provider(context: str, source: Path) -> None:
    config = source / "config" / "release"
    if not (config / "kustomization.yaml").is_file():
        raise SystemExit(
            f"ERROR: locked provider source lacks release kustomization: {config}"
        )
    kustomization = config / "kustomization.yaml"
    resources = kustomization.read_text(encoding="utf-8")
    if "../agent" not in resources:
        raise SystemExit(
            "ERROR: provider release kustomization no longer exposes the expected agent resource"
        )
    filtered = (
        "\n".join(
            line for line in resources.splitlines() if line.strip() != "- ../agent"
        )
        + "\n"
    )
    kustomization.write_text(filtered, encoding="utf-8")
    run(
        [
            "kubectl",
            "--context",
            context,
            "apply",
            "--server-side",
            "--force-conflicts",
            "-k",
            str(config),
        ]
    )


def apply_management_dns(context: str, host_address: str) -> None:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                "kube-system",
                "get",
                "configmap",
                "coredns",
                "-o",
                "json",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        config_map = json.loads(result.stdout)
        corefile = config_map["data"]["Corefile"]
    except (
        OSError,
        subprocess.CalledProcessError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit(
            "ERROR: unable to read kube-system/coredns Corefile"
        ) from error

    updated = corefile_with_host_endpoint(corefile, host_address)
    if updated == corefile:
        return
    patch = json.dumps({"data": {"Corefile": updated}})
    run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "kube-system",
            "patch",
            "configmap",
            "coredns",
            "--type=merge",
            "--patch",
            patch,
        ]
    )


def apply_manager_network_policy(
    context: str, agent_address: str, host_address: str
) -> None:
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "controller-manager",
            "namespace": "hypervisor-system",
        },
        "spec": {
            "podSelector": {"matchLabels": {"control-plane": "controller-manager"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"ports": [{"protocol": "TCP", "port": 9443}]}],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": f"{agent_address}/32"}}],
                    "ports": [{"protocol": "TCP", "port": 9444}],
                },
                {
                    "to": [{"ipBlock": {"cidr": f"{host_address}/32"}}],
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": PUBLISH_PORT_START,
                            "endPort": PUBLISH_PORT_END,
                        }
                    ],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "ports": [
                        {"protocol": "TCP", "port": 443},
                        {"protocol": "TCP", "port": 6443},
                    ]
                },
            ],
        },
    }
    run(
        ["kubectl", "--context", context, "apply", "--server-side", "-f", "-"],
        input_text=yaml.safe_dump(policy, sort_keys=False),
    )


def wait_for_management(context: str) -> None:
    crds = [
        "clusters.cluster.x-k8s.io",
        "hypervisorclusters.infrastructure.cluster.x-k8s.io",
        "hypervisorclustertemplates.infrastructure.cluster.x-k8s.io",
        "hypervisormachines.infrastructure.cluster.x-k8s.io",
        "hypervisormachinetemplates.infrastructure.cluster.x-k8s.io",
    ]
    for crd in crds:
        run(
            [
                "kubectl",
                "--context",
                context,
                "wait",
                "--for=condition=Established",
                f"customresourcedefinition/{crd}",
                "--timeout=5m",
            ]
        )
    wait_for_manager_agent_capability(context)


def wait_for_manager_agent_capability(context: str) -> None:
    # CAPH runs its authenticated HostAgent Health RPC before controller setup.
    run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "hypervisor-system",
            "rollout",
            "status",
            "deployment/controller-manager",
            "--timeout=5m",
        ]
    )


def bootstrap(args: argparse.Namespace) -> None:
    lock = load_lock(Path(args.lock))
    cache_root = Path(args.cache_root).expanduser()
    problems = validate_cache(lock, cache_root)
    if problems:
        raise SystemExit(
            "ERROR: verified management cache required: " + "; ".join(problems)
        )
    settings = host_agent_settings(Path(args.host_agent_config))
    context = args.context
    host_address = discover_host_endpoint_address(context)
    apply_management_dns(context, host_address)
    cert_manager = cache_file(lock, cache_root, "cert-manager-v1.21.2-manifest")
    capi = cache_file(lock, cache_root, "cluster-api-v1.14.2-core-components")
    provider = cache_file(
        lock,
        cache_root,
        f"cluster-api-hypervisor-{lock['cluster_api_hypervisor']['version']}-source",
    )
    for asset in (cert_manager, capi, provider):
        if not asset.is_file():
            raise SystemExit(f"ERROR: verified cache entry disappeared: {asset}")
    run(
        [
            "kubectl",
            "--context",
            context,
            "apply",
            "--server-side",
            "--force-conflicts",
            "-f",
            str(cert_manager),
        ]
    )
    run(
        [
            "kubectl",
            "--context",
            context,
            "-n",
            "cert-manager",
            "wait",
            "--for=condition=Available",
            "deployment",
            "--all",
            "--timeout=5m",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="k8labs-capi-") as temporary:
        normalized_capi = Path(temporary) / "core-components.yaml"
        normalized = normalize(capi.read_text(encoding="utf-8"))
        if "ClusterTopology=false" not in normalized:
            raise SystemExit(
                "ERROR: normalized CAPI manifest must enable ClusterTopology"
            )
        normalized_capi.write_text(
            normalized.replace("ClusterTopology=false", "ClusterTopology=true", 1),
            encoding="utf-8",
        )
        run(
            [
                "kubectl",
                "--context",
                context,
                "apply",
                "--server-side",
                "--force-conflicts",
                "-f",
                str(normalized_capi),
            ]
        )
    with tempfile.TemporaryDirectory(prefix="k8labs-provider-") as temporary:
        source = safe_extract(provider, Path(temporary))
        patch_provider(
            source, image_reference(lock, "cluster-api-hypervisor"), settings
        )
        apply_tls_secrets(context, settings)
        apply_provider(context, source)
        apply_manager_network_policy(context, settings["agent_address"], host_address)
        wait_for_management(context)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap the immutable Kind management plane"
    )
    parser.add_argument("--lock", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--host-agent-config", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    bootstrap(parse_args())
