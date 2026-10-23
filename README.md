# k8labs

k8labs provisions reproducible local Kubernetes workload clusters on KVM through Cluster API. The management plane is an owned Kind cluster; Cloud Hypervisor VMs and host integration remain outside it.

## Architecture

```text
Lab host
├── Kind management cluster: k8labs-mgmt
│   ├── kindnet and CoreDNS
│   ├── cert-manager
│   ├── Cluster API core
│   └── cluster-api-hypervisor v0.3.15 manager
│       └── mTLS connection to the host Agent
└── HostAgent (rootless Podman/systemd user service)
    ├── /dev/kvm and Cloud Hypervisor
    ├── user D-Bus and k8netd
    ├── VM, firmware, and base-image state
    └── TLS listener on a dedicated Kind/Podman address
```

Kind is **management-plane-only**. It retains Kind's default `kindnet` CNI. Cilium is installed only in workload clusters through ClusterResourceSets.

The provider manager is a hardened in-cluster Deployment. It has no host networking, host paths, KVM device, D-Bus, k8netd socket, firmware, VM-state, or artifact mounts. The separately managed HostAgent is the only component with the bounded host inputs necessary to create VMs.

## Supply-chain and transport boundaries

`versions.lock.yaml` is authoritative for the Kind node, CAPH source release, provider image, cert-manager, Cluster API, and other cached inputs. Images are digest-only and the cache validates source SHA-256s and OCI-layout identity before management-plane mutation.

The HostAgent generates a local CA and distinct server/client leaves. The manager presents a client certificate carrying `spiffe://k8labs/provider/manager`; its client key and the Agent CA are mounted read-only into the manager. The Agent listener is bound to the first usable address after the discovered Kind/Podman gateway, not to the gateway, Kind nodes, or a Kubernetes Service.

The workload image reference in `capi/cluster.yaml` is an Agent-visible artifact key:

```text
/host-state/images/k8labs-custom-kernel.qcow2
```

It must match the generated HostAgent artifact manifest exactly. Do not substitute a host filesystem path or a provider image reference.

## Prerequisites

- Rootless Podman and a working systemd user instance.
- A KVM-capable host with `/dev/kvm`; the current user belongs to `kvm`.
- `kind`, `kubectl`, `skopeo`, `podman`, `cloud-hypervisor`, `openssl`, `python3`, and the tools checked by `make prereq`.
- `guestfs-tools` (`virt-resize`) and `dosfstools` (`mkfs.vfat`) for the two-UKI comparison image's 256 MiB ESP.
- A baked `build/k8labs-base.qcow2` and `build/CLOUDHV.fd`.
- A populated verified cache. Populate it while connected:

```sh
make prepare
```

No sibling capishim or provider checkout, source build fallback, mutable image tag, or implicit management-plane image pull is used.

## Common workflow

```sh
make kernels           # fetch stock 7.3.0-65.fc46 and build kernel-k8labs
make base              # creates the baked workload image and firmware
make prepare           # downloads and verifies the locked cache while connected
make mgmt-up           # starts the host Agent and bootstraps Kind/CAPI/CAPH
make cluster           # applies workload Cluster, addons, waits, fetches kubeconfig, smoke-tests
```

`make kernels` is required after a clean checkout: `stock-kernel` downloads and
verifies the four Fedora rawhide RPMs pinned in `versions.lock.yaml`, and
`custom-kernel` builds `kernel-k8labs` from the local linux checkout at the
commit recorded in the lock. The base image installs both kernels; the stock
kernel is the default boot entry and the custom kernel is selectable from the
systemd-boot menu.

The comparison image needs a 256 MiB ESP for the two signed UKIs. Prepare
`build/fedora-cloud-base-esp.qcow2` once from the Fedora Cloud source (resize
`/dev/sda2` to 256M, format it, and update the `/boot/efi` UUID in
`/etc/fstab`), then:

```sh
make base-esp-raw
make bake-run VAR_FILE=vars.scratch.pkrvars.hcl
```

Management API operations use `kind-k8labs-mgmt`:

```sh
kubectl --context kind-k8labs-mgmt get clusters.cluster.x-k8s.io
```

`make kubeconfig` writes the workload kubeconfig to `build/kubeconfig`. `make cluster-down` deletes the workload cluster and waits for reclamation. `make multi-cluster-test` validates two concurrent workload clusters with distinct published API endpoints.

## Lifecycle commands

| Command | Effect |
|---|---|
| `make prepare` | Populate the lock-keyed cache and verify immutable artifacts. |
| `make host-agent-install` | Stage baked artifacts, render the HostAgent Quadlet, and create local TLS material. |
| `make host-agent-up` | Install then start the separate HostAgent user service. |
| `make kind-up` | Import the verified Kind image and create/reuse `k8labs-mgmt`. |
| `make mgmt-up` | Preflight the host, start the Agent, import the provider image, and bootstrap cert-manager, CAPI, and CAPH. |
| `make cluster-up` | Server-side apply `capi/cluster.yaml` to the Kind management context. |
| `make addons-up` | Generate and apply workload-only ClusterResourceSet addons. |
| `make cluster-down` | Delete the workload Cluster and wait for its CAPI resource to disappear. |
| `make mgmt-down` / `make kind-down` | Delete only the owned Kind management cluster after verifying no CAPI Cluster objects remain. |

`make mgmt-down` is destructive for management Kubernetes state but intentionally preserves HostAgent state and TLS material. To override its CAPI Cluster guard, use `MGMT_FORCE=1`; clean up workload clusters first whenever possible.

## Development checks

```sh
uv run pytest -q
uv run ruff check scripts tests
```

The focused migration tests cover cache identity, no-host-resource Kind configuration, HostAgent artifact/Quadlet boundaries, provider manager patching, NetworkPolicy, management lifecycle targets, and multicluster management-context use.

For detailed host setup and offline operation, see [docs/setup.md](docs/setup.md).
