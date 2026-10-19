# k8labs setup

This runbook configures the Kind-based Cluster API management plane and its separate host-side Hypervisor Agent. The management cluster never receives host KVM, D-Bus, k8netd, VM-state, firmware, or artifact mounts.

## 1. Host prerequisites

Use a KVM-capable machine with rootless Podman, a running systemd user instance, and a user in the `kvm` group. Required tools are checked by:

```sh
make prereq
```

The command also requires the baked workload artifacts:

```text
build/k8labs-base.qcow2
build/CLOUDHV.fd
```

Build them if necessary:

```sh
make base
```

Ensure k8netd is available in the user runtime directory before starting the HostAgent. The Agent uses the current user’s D-Bus and k8netd socket; do not configure fixed UIDs or home-directory paths.

## 2. Populate the verified cache

While connected, acquire every locked release and OCI image:

```sh
make prepare
make prepare-check
```

The cache is keyed by the canonical lock digest. `prepare-check` validates every raw artifact checksum and OCI layout before Kind creation, image import, HostAgent installation, or Kubernetes changes.

For an offline run, populate the cache first, then run the same management commands with `OFFLINE=1`. The lifecycle has no source-build fallback, sibling provider checkout, mutable image tag, or implicit pull path.

## 3. Start the HostAgent

The HostAgent is a user Quadlet service. It is deliberately separate from Kind and is the only process that receives the required bounded host inputs.

```sh
make host-agent-install
make host-agent-up
make host-agent-check
```

Installation:

- stages the baked base image and firmware beneath the configured HostAgent state directory;
- publishes host-real staged paths in the Agent manifest while retaining `/host-state/images/k8labs-custom-kernel.qcow2` as the manager-facing image key;
- creates a local CA, an Agent server certificate, and the manager client certificate;
- renders a single `Exec=` Quadlet invocation using the locked provider image digest;
- mounts only the Agent TLS files, Agent state, `/dev/kvm`, user D-Bus/k8netd interfaces, user systemd-unit directory, and bounded VM/image/firmware roots.

The server certificate has SANs for `hypervisor-agent` and the dedicated HostAgent address: the first usable IPv4 address after the discovered Kind/Podman gateway. The manager leaf carries the URI SAN `spiffe://k8labs/provider/manager`. Generated keys and Agent configuration are local state and must not be committed.

To use a different address, set `HOST_AGENT_ADDRESS` to a non-gateway IPv4 address in the Kind network before installation.

## 4. Bootstrap the management plane

```sh
make kind-up
make mgmt-bootstrap
make mgmt-check
```

Or run the composed target:

```sh
make mgmt-up
```

`kind-up` imports the verified Kind node image and creates/reuses the owned `k8labs-mgmt` cluster. Its configuration keeps Kind’s default kindnet CNI and has no host paths, device mounts, special node labels, or Agent DaemonSet.

`mgmt-bootstrap` imports the verified CAPH image into Kind, then applies cached cert-manager and Cluster API core components. It safely extracts the pinned CAPH v0.3.15 source and applies its release Kustomization after removing only the upstream in-cluster `../agent` resource. CRDs, RBAC, webhook, and manager remain in the release.

The in-cluster manager is patched to use the locked image digest and the external Agent mTLS endpoint. It runs non-root with a read-only root filesystem, no privilege escalation, all Linux capabilities dropped, and runtime-default seccomp. The Agent client certificate and Agent CA are separate read-only Secrets; the upstream webhook CA remains independent.

A manager NetworkPolicy selects only the controller-manager pods. It allows webhook ingress on TCP 9443; egress to the discovered HostAgent gateway only on TCP 9444; CoreDNS TCP/UDP 53; and Kubernetes API TCP 443/6443. It does not grant Kind nodes access to the host filesystem or sockets.

Use the management context explicitly:

```sh
kubectl --context kind-k8labs-mgmt get clusters.cluster.x-k8s.io
kubectl --context kind-k8labs-mgmt -n hypervisor-system get deployment/controller-manager
```

## 5. Provision workload clusters

```sh
make cluster
```

The workflow applies `capi/cluster.yaml` to the Kind management context, applies workload-only Cilium/RBAC/CoreDNS resources through ClusterResourceSets, waits for CAPI readiness, fetches the workload kubeconfig to `build/kubeconfig`, and runs the smoke test.

The image path in the `HypervisorClusterTemplate` is an exact Agent artifact-manifest key. Keep it equal to:

```yaml
path: /host-state/images/k8labs-custom-kernel.qcow2
```

The custom-kernel image is used for future machine replacements created from
this Cluster template. It does not replace or restart the running control
plane; rollback remains available through the staged
`/host-state/images/k8labs-base.qcow2` image.

Cilium belongs only to workload clusters; do not replace Kind’s management-cluster CNI.

To exercise concurrent workload clusters:

```sh
make multi-cluster-test
```

It verifies that the published workload API endpoints differ and cleans up both clusters.

## 6. Safe teardown

```sh
make cluster-down
make mgmt-down
```

`mgmt-down` aliases guarded `kind-down`. It refuses to remove `k8labs-mgmt` while CAPI `Cluster` objects remain. Use `MGMT_FORCE=1 make mgmt-down` only when intentionally discarding the management cluster despite remaining CAPI objects.

Management teardown deletes Kind Kubernetes state only. It does not remove the HostAgent service, its TLS material, Agent inventory, VM artifacts, firmware, or baked image copies. Preserve those state boundaries when performing manual maintenance.
