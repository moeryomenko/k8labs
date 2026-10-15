from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "bootstrap_management", SCRIPTS / "bootstrap_management.py"
)
assert SPEC and SPEC.loader
bootstrap_management = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap_management
SPEC.loader.exec_module(bootstrap_management)


LOCKED_IMAGE = "registry.test/cluster-api-hypervisor@sha256:" + "a" * 64
SETTINGS = {
    "agent_address": "10.89.0.2",
    "server_name": "hypervisor-agent",
    "manager_identity": "spiffe://k8labs/provider/manager",
    "tls_dir": "/unused",
    "ssh_public_key": "/unused/ssh-lab.pub",
}


def manager_deployment() -> dict[str, object]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "controller-manager"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "manager",
                            "image": "localhost/mutable",
                            "args": [
                                "--agent-address=service:9444",
                                "--agent-server-name=service",
                                "--agent-client-cert-dir=/tls/agent-client",
                                "--agent-ca=/tls/agent-ca/ca.crt",
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "agent-client-certs", "secret": {"secretName": "old"}},
                        {"name": "agent-ca", "secret": {"secretName": "old"}},
                    ],
                }
            }
        },
    }


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "provider.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(SystemExit, match="unsafe path"):
        bootstrap_management.safe_extract(archive, tmp_path / "extract")


def test_patch_provider_configures_external_agent_and_hardening(tmp_path: Path) -> None:
    deployment = tmp_path / "config" / "manager" / "deployment.yaml"
    deployment.parent.mkdir(parents=True)
    deployment.write_text(yaml.safe_dump(manager_deployment()), encoding="utf-8")
    webhook_manifest = tmp_path / "config" / "webhook" / "manifests.yaml"
    webhook_manifest.parent.mkdir(parents=True)
    webhook_manifest.write_text(
        yaml.safe_dump_all(
            [
                {
                    "kind": "MutatingWebhookConfiguration",
                    "metadata": {"name": "mutating-webhook-configuration"},
                    "webhooks": [
                        {
                            "clientConfig": {
                                "service": {
                                    "name": "webhook-service",
                                    "namespace": "system",
                                }
                            }
                        }
                    ],
                },
                {
                    "kind": "ValidatingWebhookConfiguration",
                    "metadata": {"name": "validating-webhook-configuration"},
                    "webhooks": [
                        {
                            "clientConfig": {
                                "service": {
                                    "name": "webhook-service",
                                    "namespace": "system",
                                }
                            }
                        }
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )

    bootstrap_management.patch_provider(tmp_path, LOCKED_IMAGE, SETTINGS)

    rendered = yaml.safe_load(deployment.read_text(encoding="utf-8"))
    pod_spec = rendered["spec"]["template"]["spec"]
    manager = pod_spec["containers"][0]
    assert manager["image"] == LOCKED_IMAGE
    assert manager["args"][-4:] == [
        "--agent-address=10.89.0.2:9444",
        "--agent-server-name=hypervisor-agent",
        "--agent-client-cert-dir=/tls/agent-client",
        "--agent-ca=/tls/agent-ca/ca.crt",
    ]
    assert manager["securityContext"] == {
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod_spec["hostAliases"] == [
        {"ip": "10.89.0.2", "hostnames": ["hypervisor-agent"]}
    ]
    volumes = {volume["name"]: volume["secret"] for volume in pod_spec["volumes"]}
    assert volumes["agent-client-certs"] == {
        "secretName": "k8labs-manager-agent-client"
    }
    assert volumes["agent-ca"] == {
        "secretName": "k8labs-host-agent-ca",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }
    assert volumes["manager-ssh-public-key"] == {
        "secretName": "k8labs-manager-ssh-public-key"
    }
    assert manager["env"] == [
        {
            "name": "HYPERVISOR_SSH_PUBLIC_KEY_FILE",
            "value": "/etc/k8labs/ssh/ssh-lab.pub",
        }
    ]
    assert manager["volumeMounts"] == [
        {
            "name": "manager-ssh-public-key",
            "mountPath": "/etc/k8labs/ssh",
            "readOnly": True,
        }
    ]
    webhook_documents = list(
        yaml.safe_load_all(webhook_manifest.read_text(encoding="utf-8"))
    )
    for webhook_document in webhook_documents:
        assert webhook_document["metadata"]["annotations"] == {
            "cert-manager.io/inject-ca-from": "hypervisor-system/hypervisor-webhook-serving"
        }
        assert (
            webhook_document["webhooks"][0]["clientConfig"]["service"]["namespace"]
            == "hypervisor-system"
        )


def test_manager_network_policy_allows_only_required_transport() -> None:
    with patch.object(bootstrap_management, "run") as run:
        bootstrap_management.apply_manager_network_policy(
            "kind-k8labs-mgmt", "10.89.0.2", "169.254.1.2"
        )

    command = run.call_args.args[0]
    rendered = yaml.safe_load(run.call_args.kwargs["input_text"])
    assert command == [
        "kubectl",
        "--context",
        "kind-k8labs-mgmt",
        "apply",
        "--server-side",
        "-f",
        "-",
    ]
    assert rendered["spec"]["podSelector"] == {
        "matchLabels": {"control-plane": "controller-manager"}
    }
    assert rendered["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert rendered["spec"]["ingress"] == [
        {"ports": [{"protocol": "TCP", "port": 9443}]}
    ]
    assert rendered["spec"]["egress"][0] == {
        "to": [{"ipBlock": {"cidr": "10.89.0.2/32"}}],
        "ports": [{"protocol": "TCP", "port": 9444}],
    }
    assert {
        "to": [{"ipBlock": {"cidr": "169.254.1.2/32"}}],
        "ports": [{"protocol": "TCP", "port": 20000, "endPort": 21000}],
    } in rendered["spec"]["egress"]


def test_parse_host_endpoint_address_reads_kind_node_hosts() -> None:
    hosts = """127.0.0.1 localhost
169.254.1.2 host.containers.internal host.docker.internal
10.89.0.3 k8labs-mgmt-control-plane
"""

    assert bootstrap_management.parse_host_endpoint_address(hosts) == "169.254.1.2"


def test_discover_host_endpoint_address_reads_the_contexts_kind_node() -> None:
    result = bootstrap_management.subprocess.CompletedProcess(
        args=["podman", "exec"],
        returncode=0,
        stdout="169.254.1.2 host.containers.internal\n",
    )
    with patch.object(
        bootstrap_management.subprocess, "run", return_value=result
    ) as run:
        address = bootstrap_management.discover_host_endpoint_address(
            "kind-k8labs-mgmt"
        )

    assert address == "169.254.1.2"
    assert run.call_args.args[0] == [
        "podman",
        "exec",
        "k8labs-mgmt-control-plane",
        "cat",
        "/etc/hosts",
    ]


def test_parse_host_endpoint_address_rejects_missing_or_ambiguous_mapping() -> None:
    with pytest.raises(SystemExit, match="host.containers.internal.*not found"):
        bootstrap_management.parse_host_endpoint_address("127.0.0.1 localhost\n")

    with pytest.raises(SystemExit, match="multiple host.containers.internal addresses"):
        bootstrap_management.parse_host_endpoint_address(
            "169.254.1.2 host.containers.internal\n192.0.2.1 host.containers.internal\n"
        )


def test_render_corefile_host_endpoint_is_preserving_and_idempotent() -> None:
    corefile = ".:53 {\n    errors\n    kubernetes cluster.local\n    forward . /etc/resolv.conf\n}\n"

    configured = bootstrap_management.corefile_with_host_endpoint(
        corefile, "169.254.1.2"
    )

    assert "169.254.1.2 host.containers.internal" in configured
    assert "forward . /etc/resolv.conf" in configured
    assert (
        bootstrap_management.corefile_with_host_endpoint(configured, "169.254.1.2")
        == configured
    )

    updated = bootstrap_management.corefile_with_host_endpoint(
        configured, "169.254.1.3"
    )
    assert "169.254.1.3 host.containers.internal" in updated
    assert "169.254.1.2 host.containers.internal" not in updated


def test_render_corefile_host_endpoint_rejects_unmanaged_hosts_plugin() -> None:
    corefile = ".:53 {\n    hosts /etc/hosts\n    forward . /etc/resolv.conf\n}\n"

    with pytest.raises(SystemExit, match="unmanaged hosts plugin"):
        bootstrap_management.corefile_with_host_endpoint(corefile, "169.254.1.2")


def test_apply_management_dns_patches_only_the_coredns_corefile() -> None:
    corefile = ".:53 {\n    errors\n    forward . /etc/resolv.conf\n}\n"
    config_map = {"data": {"Corefile": corefile, "unrelated": "keep"}}
    get_result = bootstrap_management.subprocess.CompletedProcess(
        args=["kubectl", "get"], returncode=0, stdout=json.dumps(config_map)
    )
    with (
        patch.object(bootstrap_management.subprocess, "run", return_value=get_result),
        patch.object(bootstrap_management, "run") as run,
    ):
        bootstrap_management.apply_management_dns("kind-k8labs-mgmt", "169.254.1.2")

    command = run.call_args.args[0]
    patch_doc = json.loads(command[-1])
    assert command[:8] == [
        "kubectl",
        "--context",
        "kind-k8labs-mgmt",
        "-n",
        "kube-system",
        "patch",
        "configmap",
        "coredns",
    ]
    assert patch_doc["data"].keys() == {"Corefile"}
    assert "unrelated" not in patch_doc["data"]
    assert "169.254.1.2 host.containers.internal" in patch_doc["data"]["Corefile"]


def test_wait_for_management_waits_for_provider_crds_and_manager() -> None:
    with patch.object(bootstrap_management, "run") as run:
        bootstrap_management.wait_for_management("kind-k8labs-mgmt")

    commands = [call.args[0] for call in run.call_args_list]
    assert [
        "kubectl",
        "--context",
        "kind-k8labs-mgmt",
        "wait",
        "--for=condition=Established",
        "customresourcedefinition/hypervisormachines.infrastructure.cluster.x-k8s.io",
        "--timeout=5m",
    ] in commands
    assert commands[-1] == [
        "kubectl",
        "--context",
        "kind-k8labs-mgmt",
        "-n",
        "hypervisor-system",
        "rollout",
        "status",
        "deployment/controller-manager",
        "--timeout=5m",
    ]


def test_patch_provider_rejects_missing_agent_tls_flags(tmp_path: Path) -> None:
    deployment = tmp_path / "config" / "manager" / "deployment.yaml"
    deployment.parent.mkdir(parents=True)
    manifest = manager_deployment()
    manifest["spec"]["template"]["spec"]["containers"][0]["args"].pop()
    deployment.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(SystemExit, match="missing agent TLS flags"):
        bootstrap_management.patch_provider(tmp_path, LOCKED_IMAGE, SETTINGS)


def test_apply_provider_excludes_only_incluster_agent(tmp_path: Path) -> None:
    release = tmp_path / "config" / "release"
    release.mkdir(parents=True)
    kustomization = release / "kustomization.yaml"
    kustomization.write_text(
        "resources:\n- ../crd\n- ../manager\n- ../agent\n- ../webhook\n",
        encoding="utf-8",
    )

    with patch.object(bootstrap_management, "run") as run:
        bootstrap_management.apply_provider("kind-k8labs-mgmt", tmp_path)

    assert "../agent" not in kustomization.read_text(encoding="utf-8")
    assert "../manager" in kustomization.read_text(encoding="utf-8")
    run.assert_called_once_with(
        [
            "kubectl",
            "--context",
            "kind-k8labs-mgmt",
            "apply",
            "--server-side",
            "--force-conflicts",
            "-k",
            str(release),
        ]
    )


def test_wait_for_management_requires_authenticated_agent_capability() -> None:
    with patch.object(bootstrap_management, "run") as run:
        bootstrap_management.wait_for_management("kind-k8labs-mgmt")

    assert run.call_args_list[-1].args[0] == [
        "kubectl",
        "--context",
        "kind-k8labs-mgmt",
        "-n",
        "hypervisor-system",
        "rollout",
        "status",
        "deployment/controller-manager",
        "--timeout=5m",
    ]
