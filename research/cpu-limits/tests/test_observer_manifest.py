from pathlib import Path

import yaml

MANIFEST = (
    Path(__file__).resolve().parents[1] / "observers" / "collector-daemonset.yaml"
)
REQUIRED_MOUNTS = {
    "/host/sys/fs/cgroup",
    "/host/sys/kernel/debug",
    "/host/sys/kernel/tracing",
    "/sys/kernel/tracing",
    "/host/sys/kernel/btf",
    "/host/boot",
    "/host/lib/modules",
    "/host/usr/src/kernels",
    "/fixtures",
    "/opt/linux-sched/bin",
    "/opt/cpu-limits",
}


def load_resources() -> list[dict]:
    with MANIFEST.open(encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


def resource_by_kind(resources: list[dict], kind: str) -> dict:
    return next(resource for resource in resources if resource["kind"] == kind)


def test_resources_use_the_cpu_limits_namespace() -> None:
    resources = load_resources()

    assert resources
    assert {resource["metadata"]["namespace"] for resource in resources} == {
        "cpu-limits"
    }


def test_daemonset_is_restricted_to_collector_nodes() -> None:
    daemonset = resource_by_kind(load_resources(), "DaemonSet")
    pod_spec = daemonset["spec"]["template"]["spec"]

    assert pod_spec["nodeSelector"] == {"cpu-limits/research": "collector"}
    assert pod_spec["hostPID"] is True
    assert pod_spec["hostNetwork"] is True


def test_collector_is_not_privileged_and_uses_read_only_host_mounts() -> None:
    daemonset = resource_by_kind(load_resources(), "DaemonSet")
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    security_context = container["securityContext"]
    mounts = container["volumeMounts"]

    assert security_context["privileged"] is False
    assert security_context["allowPrivilegeEscalation"] is False
    # The collector installs the node-native tracing tools at start, so its
    # own root is writable. Every host path stays read-only.
    assert security_context["readOnlyRootFilesystem"] is False
    assert "DAC_READ_SEARCH" in security_context["capabilities"]["add"]
    assert all(mount["readOnly"] is True for mount in mounts)
    assert {mount["mountPath"] for mount in mounts} == REQUIRED_MOUNTS


def test_collector_code_is_a_configmap_volume_and_the_image_is_pinned() -> None:
    daemonset = resource_by_kind(load_resources(), "DaemonSet")
    pod_spec = daemonset["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}

    assert "@sha256:" in container["image"]
    assert volumes["collector"]["configMap"]["name"] == "cpu-limits-collector"
    assert any(
        mount["name"] == "collector" and mount["readOnly"] is True
        for mount in container["volumeMounts"]
    )


def test_rbac_only_lists_pods_in_the_namespace() -> None:
    resources = load_resources()
    role = resource_by_kind(resources, "Role")
    binding = resource_by_kind(resources, "RoleBinding")

    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "cpu-limits-collector",
    }
    assert all(
        resource["kind"] not in {"ClusterRole", "ClusterRoleBinding"}
        for resource in resources
    )
    assert all("cluster-admin" not in str(resource).lower() for resource in resources)
