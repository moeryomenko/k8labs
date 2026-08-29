# k8labs

Kubernetes cluster lab environment for experimentation and research. k8labs provisions a disposable, reproducible Kubernetes cluster on local KVM infrastructure through a **declarative Cluster API pipeline**: the cluster is a checked-in manifest, applied against a rootless management plane, with nodes built from system-extension (sysext/confext) layered OS images and networked by Cilium CNI with L2-aware load balancing.

The lab is fully rootless: the management plane runs as podman quadlets under the systemd **user** instance, VM processes are owned by user-scope services, and no `make` target invokes `sudo`. The VM infrastructure uses **Cloud-Hypervisor** (CH) as the VMM — fast VM startup (~200--400 ms), a small dependency chain (no libvirtd), and stateless operation.

## Purpose

k8labs exists to enable practical research into Kubernetes cluster internals and Cluster API mechanics:

- **Declarative cluster lifecycle** — the workload Cluster (ClusterClass + topology) is a version-controlled manifest (`capi/cluster.yaml`); creating, inspecting, and deleting the cluster are Kubernetes API operations against the management plane, not imperative scripts.
- **Cluster API under the hood** — the management plane (capishim) exposes etcd, a kube-apiserver, and the CAPI core managers as ordinary user-scope containers; every CAPI object (Cluster, Machine, MachineDeployment, ClusterResourceSet) is directly inspectable with `kubectl`.
- **Cilium CNI networking** — installation, configuration, and exploration of Cilium's eBPF-based networking, including L2 announcements for LoadBalancer services and Gateway API for L7 traffic management.
- **System extension layering** — using systemd-sysext and systemd-confext to overlay Kubernetes and container runtime binaries and configuration onto a minimal base OS image, demonstrating immutable OS extension patterns.
- **Rootless infrastructure** — the entire stack (management plane, provider manager, network daemon, VMs) runs unprivileged in user scope; the only privileged step in the whole lab is adding your user to the `kvm` group.

## Architecture

The lab has three planes plus an image pipeline. The image pipeline produces artifacts; the planes consume them.

```
                 host (rootless, systemd --user)
 ┌────────────────────────────────────────────────────────────┐
 │  Management plane: capishim quadlet pod                    │
 │    etcd + kube-apiserver + CAPI core managers              │
 │    kubeconfig: ~/.kube/capishim.kubeconfig                 │
 │                                                            │
 │  Providers: cluster-api-hypervisor (one binary)            │
 │    infrastructure + bootstrap + control-plane providers;   │
 │    components installed by capishim's setup container;     │
 │    manager runs as a user quadlet                          │
 │                                                            │
 │  Dataplane: k8netd (user quadlet)                          │
 │    vhost-user L2 switch + IPAM/DHCP/DNS + per-VM passt WAN │
 ├────────────────────────────────────────────────────────────┤
 │  Workload cluster VMs (Cloud-Hypervisor)                   │
 │    1 control plane + 3 workers, baked base image           │
 └────────────────────────────────────────────────────────────┘
```

1. **Management plane** — [capishim](https://github.com/moeryomenko/capishim) replaces a stock Cluster API management cluster with a podman quadlet pod: etcd, kube-apiserver, and the four CAPI core managers run as per-component containers under `systemctl --user`, with a Go setup container performing initialization (CA/certs, CRDs, RBAC, admin kubeconfig). The admin kubeconfig lives at `~/.kube/capishim.kubeconfig`; all management-plane operations go through it.

2. **Providers** — [cluster-api-hypervisor](https://github.com/moeryomenko/cluster-api-hypervisor) is a single manager binary that registers three providers (infrastructure, bootstrap, control-plane). Its CRDs, RBAC, and webhook configurations are installed into the management plane by capishim's setup container at pod boot, from manifests vendored into the capishim repository; the manager itself ships **no Deployment** — it runs as a user quadlet next to the management plane (installed with `make install-quadlet`). clusterctl is optional in this lab — only an offline template renderer (`generate cluster`). It boots workload VMs through cloud-hypervisor and drives all cluster networking through k8netd.

3. **Dataplane** — [k8netd](https://github.com/moeryomenko/k8netd) is a rootless userspace vhost-user L2 switch with integrated IPAM, DHCP, DNS forwarding, and a per-VM passt WAN subprocess. It replaces the former Linux bridge + TAP + dnsmasq + nftables stack, presents a real L2 segment to the VMs (which Cilium L2 announcements require), and publishes per-VM host ports (workload apiserver, SSH) through an idempotent `PublishPort` RPC backed by a persisted allocator — distinct allocations per cluster are what allow several clusters to run concurrently on one host (`make multi-cluster-test` proves two). The provider is its only client (JSON-RPC over a Unix control socket).

4. **Declarative cluster** — `capi/cluster.yaml` contains the checked-in Cluster and everything its ClusterClass references, rendered once with concrete values: cluster name `k8labs`, 1 control-plane machine + 3 workers (`md-0`), Kubernetes v1.32.13 (matching the kubelet/kubernetes-cp sysext pins), machine sizes carried from the former tfvars defaults (control plane cpu 2 / ram 2048MiB / disk 20480MiB; workers cpu 2 / ram 4096MiB / disk 40960MiB), and the lab network values (192.168.124.0/24, gateway/DNS 192.168.124.1). `make cluster-up` server-side applies it.

5. **Addons via ClusterResourceSet** — `capi/addons/` holds three ClusterResourceSets (RBAC, Cilium, CoreDNS). Each references one or more Secrets of type `addons.cluster.x-k8s.io/resource-set` embedding the corresponding manifests from `rbac/`, `cilium/`, and `coredns/`, and selects clusters by the `cluster.x-k8s.io/cluster-name: k8labs` label. The CRS objects themselves are applied to the management plane by `make addons-up` (part of `make cluster`); once a matching Cluster exists, the CAPI addons controller reconciles each ClusterResourceSet and applies its Secret payloads to the workload cluster. Payload fidelity against the source manifests is enforced by `tests/test_capi_assets.py`.

6. **Base image pipeline** — Packer bakes the immutable node image from Fedora Cloud Base with every node prerequisite baked in: the seven systemd-sysext images (kubelet, CRI-O, crun, CNI, etcd, kubernetes-cp, perfetto) under `/var/lib/extensions/`, the three static systemd-confext images (`confext-cri-o`, `confext-kubernetes`, `confext-containers`) under `/var/lib/confexts/`, plus conmon/parted/growpart, a first-boot root-resize helper, and enabled `systemd-sysext`/`systemd-confext` services. At first boot each VM merges its extensions; the cluster-api-hypervisor controllers deliver the runtime-dependent per-node configuration (certs, kubeconfigs) as bootstrap data.

## Technologies

| Layer | Technology |
|---|---|
| Hypervisor | KVM (Cloud-Hypervisor) |
| Base OS | Fedora 44 (kernel 7.1) |
| Image baking | Packer (cloudhypervisor builder on Fedora Cloud) |
| Cluster lifecycle | Cluster API v1beta2 (ClusterClass topology) |
| Management plane | capishim — rootless podman quadlet pod (etcd + kube-apiserver + CAPI core managers), systemd --user |
| Provider install | vendored manifests applied by capishim's setup container at pod boot; manager quadlet via `make install-quadlet` |
| Infra/bootstrap/control-plane provider | cluster-api-hypervisor (one binary, three providers, manager as user quadlet) |
| Dataplane | k8netd — rootless userspace vhost-user L2 switch (IPAM, DHCP, DNS, per-VM passt WAN) |
| Container runtime | CRI-O with crun |
| CNI | Cilium (eBPF, L2 announcements, Gateway API) |
| Service discovery | etcd |
| OS extensions | systemd-sysext / systemd-confext (baked into the base image) |

### Plugins

The lab toolchain uses two third-party plugins maintained under the `moeryomenko` GitHub account:

| Plugin | Role | Wiring in k8labs |
|---|---|---|
| [packer-plugin-cloud-hypervisor](https://github.com/moeryomenko/packer-plugin-cloud-hypervisor) | Cloud-Hypervisor Packer builder that produces the base image | Used by `make base`; built from source inside the bake container at `PACKER_PLUGIN_REF` (default `main`, Makefile) by `make bake-image`, installed into the image's `PACKER_PLUGIN_PATH` |
| [packer-plugin-systemd-ext](https://github.com/moeryomenko/packer-plugin-systemd-ext) | Bakes or persists systemd extension images during a Packer build (`systemd-ext-sysext` overlays `/usr`, `systemd-ext-confext` overlays `/etc`) | Not wired into the current pipeline — k8labs builds the `.raw` images with `extensions/build.sh` (mksquashfs) and bakes them into the base image with plain Packer `file` provisioners |

## One-time setup

The management-plane components are installed **in their own repositories** — k8labs never builds or installs them. Per-repo summaries follow; the full runbook with prerequisites and verification steps is in [docs/setup.md](docs/setup.md).

**capishim** (see `capishim/docs/install.md`) — from the capishim checkout:

```sh
make images && make install-quadlet && systemctl --user daemon-reload && systemctl --user start capishim-pod
```

This builds the five capishim images, renders nine quadlet units into `~/.config/containers/systemd/`, enables lingering, and symlinks `~/.kube/capishim.kubeconfig` to the admin kubeconfig the setup container writes at boot.

**cluster-api-hypervisor** (see `cluster-api-hypervisor/docs/install-contract.md`; `docs/clusterctl.md` covers only the standalone clusterctl alternative) — from the provider checkout:

1. One-time host preparation: add your user to the `kvm` group (the single privileged step of the whole lab) and create the state directories (full runbook in [docs/setup.md](docs/setup.md)).
2. Build the local-only provider image and install the shipped quadlet: `make image && make install-quadlet && systemctl --user daemon-reload`. The quadlet (`deploy/cluster-api-hypervisor.container`) defaults to the k8labs layout; adjust the two path variables in its header if yours differs. Install k8netd's quadlet the same way from its own checkout.
3. Start everything with `make mgmt-up` in k8labs — capishim's setup container installs all three hypervisor providers at pod boot and mints the provider's webhook certificates and kubeconfig, so there is nothing manual to run against the management plane. `mgmt-up` gates on the provider's `/readyz`.
4. Optional, template rendering only: `make components` and render the committed `clusterctl.yaml` (substituting the placeholder base paths) to `~/.cluster-api/clusterctl.yaml` for `clusterctl generate cluster`.

**k8labs side** — bake what the provider consumes:

```sh
make node-tools   # uv sync -> .venv (required by make prereq)
make plugin       # alias for `make bake-image`: builds the rootless bake container (packer + CH plugin baked in)
make base         # bake build/k8labs-base.qcow2 in the rootless bake container (+ build/CLOUDHV.fd via make base-deps)
make provider-state  # publish base image/firmware/SSH key into the provider's build mount, create state dirs
```

`make base` runs Packer + the Cloud-Hypervisor plugin **inside a rootless podman
container** (`bake/Containerfile` + `bake/bake-net.sh`). The container owns a
private network namespace where the lab user is namespace-root, so
`bake-net.sh` can create the `packer-tap`, run dnsmasq, and NAT the guest to
the Fedora repos **without any host bridge/TAP/dnsmasq/NAT or root**. The
bake needs only `--device /dev/kvm --device /dev/net/tun --cap-add
NET_ADMIN,NET_RAW --sysctl net.ipv4.ip_forward=1`. `make provider-state`
publishes the artifacts into the provider's build mount
(`~/.local/state/k8slab/build/`) and creates the provider's state dirs.

## Host reachability model

Host access to the workload cluster is **API-path only**:

| Path | Reachable from host | Mechanism |
|---|---|---|
| Workload apiserver `https://127.0.0.1:<published port>` | Yes | k8netd's `PublishPort` RPC allocates the control-plane VM's published host port and the kubeconfig Secret records it; this is how `build/kubeconfig` works from the host |
| SSH to the control-plane VM | Yes | `PublishPort` allocates a host port for the VM's SSH forward (see the machine's `status.publishedPorts`) |
| LoadBalancer IPs (Cilium pool `10.0.10.0/24`) | **No** | There is no host route into the cluster's L2 segment; LB services must be probed in-cluster |

There is deliberately no bridge/TAP device or host route for LB traffic anymore. To test a LoadBalancer service, run the probe inside the cluster — the pattern `capi/smoke-test/job.yaml` uses: an in-cluster Job curling the Service's cluster-local DNS name (`http://cilium-gw.default.svc.cluster.local/`). The same approach works ad hoc:

```sh
kubectl --kubeconfig build/kubeconfig run -it --rm probe --image=curlimages/curl:8.10.1 \
  --restart=Never -- http://<service>.<namespace>.svc.cluster.local/
```

## Quick Start

Requirements: a Linux host with KVM-capable hardware and your user in the `kvm` group; `cloud-hypervisor` (>= v38), `podman` (>= 4.7, quadlet support), `packer`, `uv`, `clusterctl`, `kubectl`, `openssl`, `jq`, `mkdosfs` (dosfstools), `mcopy` (mtools). The one-time sibling-repo installs above must be done first.

```sh
make node-tools    # sync Python tooling (creates .venv; make prereq requires it)
make plugin        # alias for make bake-image (rootless bake container, plugin baked in)
make base          # bake the base image (re-run after extension changes)
make provider-state  # publish base image + firmware + SSH key into the provider's build mount
make prereq        # validate tooling, quadlet units, capishim kubeconfig, KVM readiness, baked artifacts
make cluster       # mgmt-images -> provider-state -> mgmt-up -> cluster-up -> addons-up -> wait Ready -> kubeconfig -> smoke-test
```

`make cluster` executes the full declarative pipeline: pulls the management-plane images from ghcr.io and retags them to the quadlet names (`mgmt-images`), publishes the bake artifacts into the provider's build mount (`provider-state`), starts the capishim management plane units and waits for its API plus the provider's `/readyz` endpoint (`mgmt-up`), server-side applies `capi/cluster.yaml` (`cluster-up`), server-side applies `capi/addons/` so the ClusterResourceSets exist on the management plane (`addons-up`), waits for the Cluster to become Ready (up to 30 m — the provider boots the VMs, k8netd wires them, the control plane forms, and the addons controller reconciles the ClusterResourceSets onto the workload cluster, applying RBAC/Cilium/CoreDNS), fetches the workload kubeconfig to `build/kubeconfig`, and gates the result with the in-cluster smoke-test Job.

Use the cluster:

```sh
kubectl --kubeconfig build/kubeconfig get nodes
```

Individual stages:

**Management plane and cluster lifecycle** (`CLUSTER_NAME ?= k8labs`; override with `make <target> CLUSTER_NAME=<name>`):

| Target | Description |
|--------|-------------|
| `make prereq` | Validate CAPI tooling (cloud-hypervisor, openssl, systemctl, jq, python3 venv, clusterctl, kubectl, podman), the three quadlet units under `~/.config/containers/systemd/`, `~/.kube/capishim.kubeconfig`, KVM readiness (`/dev/kvm` + `kvm` group), and the baked build artifacts |
| `make mgmt-up` | Start the capishim-pod/k8netd/provider user units and wait until the management API and the provider `/readyz` respond (idempotent) |
| `make mgmt-down` | Stop the management-plane units (never deletes management state) |
| `make cluster-up` | Server-side apply `capi/cluster.yaml` against the management plane (idempotent) |
| `make addons-up` | Server-side apply `capi/addons/` (ClusterResourceSets + resource Secrets) against the management plane (idempotent) |
| `make kubeconfig` | Fetch Secret `<cluster>-kubeconfig` from the management plane, decode `data.value` via `scripts/fetch-kubeconfig`, write `build/kubeconfig` (mode 0600) |
| `make update-kubeconfig` | Alias for `kubeconfig` — explicitly signals refresh |
| `make smoke-test` | Apply `capi/smoke-test/job.yaml` against the workload cluster (`build/kubeconfig`) and wait for Job success |
| `make cluster-down` | Delete the workload Cluster via the management plane and wait for reclamation |
| `make cluster` | Full pipeline: mgmt-images → provider-state → mgmt-up → cluster-up → addons-up → wait Cluster ready → kubeconfig → smoke-test |

**Management-plane images and provider state:**

| Target | Description |
|--------|-------------|
| `make mgmt-images` | Pull the capishim/provider/k8netd images from `ghcr.io/moeryomenko` (pushed by each sibling repo's GitHub Actions workflow) and retag them to the `localhost/*` names the quadlets reference |
| `make provider-state` | Create `~/.local/state/k8slab/build/vm-disks` and `/tmp/ch-capi`; copy the baked base image, firmware, and `build/packer-ssh-key.pub` (as `ssh-lab.pub`) into the provider's build mount |

**Image baking:**

| Target | Description |
|--------|-------------|
| `make base` | Build base image from Fedora Cloud + cloud-init in the rootless bake container (skips when newer than the newest extension image) |
| `make base-rebuild` | Force rebuild base image |
| `make bake-image` | Build the rootless bake container image (`localhost/k8labs-bake:dev`) — Fedora base, packer, cloud-hypervisor, dnsmasq/iptables/iproute2, and the CH plugin built from source at `PACKER_PLUGIN_REF` (default `main`) |
| `make plugin` | Alias for `make bake-image` (the Packer CH plugin now lives inside the bake container) |
| `make plugin-rebuild` | Alias for `make bake-image` — force refresh of the bake image (rebuilds the plugin from source) |
| `make base-deps` | Download CLOUDHV.fd firmware + Fedora Cloud Base qcow2 (checksum-verified), convert to raw |
| `make base-cloudinit` | Generate FAT16 CIDATA disk for Packer SSH key injection |
| `make base-ssh-key` | Generate SSH keypair for Packer provisioning |

**Extensions:**

| Target | Description |
|--------|-------------|
| `make sysexts` | Build all seven sysext images in parallel (kubelet, cri-o, crun, cni, etcd, kubernetes-cp, perfetto) |
| `make confexts` | Build all three configuration extension overlays (cri-o, kubernetes, containers) |
| `make extensions` | Build all extensions (sysexts + confexts) |
| `make download-sysexts` | Download pre-built sysext binaries from upstream |
| `make sysext/<name>` | Build one sysext (e.g. `sysext/etcd`, `sysext/kubernetes-cp`, `sysext/perfetto`) |
| `make confext/<name>` | Build one confext (`confext/cri-o`, `confext/kubernetes`, `confext/containers`) |

**Cluster operations** (legacy targets; they read the repo-root `kubeconfig` file — after `make cluster`, point it at the fetched kubeconfig with `ln -sf build/kubeconfig kubeconfig`):

| Target | Description |
|--------|-------------|
| `make rbac` | Apply cluster RBAC (kubelet bootstrap, `system:nodes`, admin, apiserver-proxy) |
| `make cilium` | Install Cilium from committed manifests (cilium.io CRDs + install + policies, v1.19.6) |
| `make coredns` | Deploy CoreDNS cluster DNS (kube-dns Service at 10.96.0.10) |
| `make metrics-server` | Deploy metrics-server for `kubectl top` |

These overlap with the ClusterResourceSet addons on purpose: the CRS variants apply automatically at cluster creation, while these targets allow (re-)applying the same manifests manually against a running cluster.

**Validation and maintenance:**

| Target | Description |
|--------|-------------|
| `make validate` | Run all validations (= `node-tools` + `validate-packer`) without building anything |
| `make validate-packer` | Validate Packer template syntax |
| `make test` | Run the Python test suite (pytest; includes the CAPI asset contract tests) |
| `make node-tools` | Sync Python tooling with uv (creates `.venv`, idempotent) |
| `make clean` | Remove build artifacts (`build/`, `extensions/release/*.raw`) |

Pre-built dependencies are cached: re-running `make base` skips the bake when the base image is newer than the newest extension image; use `make base-rebuild` to force it. `mgmt-up`, `cluster-up`, `addons-up`, and `kubeconfig` are idempotent, so `make cluster` converges rather than recreates.

## Cluster lifecycle and teardown

Teardown is layered — match the layer to what you want to reset:

| Layer | Command | What it does | What survives |
|-------|---------|--------------|---------------|
| Workload cluster | `make cluster-down` | Deletes the `Cluster` object via the management plane and waits (up to 10 m) for reclamation — CAPI garbage-collects the Machines and, through them, the VMs, their disks, and their k8netd ports | Management plane, provider state, baked images |
| Management plane | `make mgmt-down` | Stops the three user units (`systemctl --user stop`) | All management state by design: etcd data, PKI, kubeconfigs persist under the capishim state dir (`~/.local/share/capishim`, owned by capishim) |

Notes:

- `mgmt-down` never deletes management state — `make mgmt-up` brings the plane back with every object intact, including previously created Clusters.
- There is intentionally **no** make target that wipes management-plane state; removing the capishim state directory is a manual, out-of-band decision.
- A full rebuild cycle is `make cluster-down && make cluster-up && make kubeconfig` — the management plane keeps running throughout.
- The smoke-test Job is namespace-scoped and safe to re-run: `make smoke-test` re-applies and waits for `job/lb-smoke-test`.

## Project Structure

- `packer/` — Packer templates (`base.pkr.hcl`), cloud-init configs, and provisioning scripts for base image baking (bakes the sysext/confext images)
- `extensions/` — Declarative extension manifest (`manifest.yaml`), build scripts, and download utilities for systemd-sysext and systemd-confext packaging
- `sysext/` — Raw system extension directory structures (binaries, systemd units)
- `confext/` — Raw configuration extension directory structures (kubelet config, CRI-O config, container policy)
- `capi/` — Declarative CAPI assets: `cluster.yaml` (ClusterClass + referenced templates + topology Cluster), `addons/` (three ClusterResourceSets with their resource-set Secrets), `smoke-test/job.yaml`
- `cilium/` — Committed Cilium manifests (CRD bundle, install, LB pool, L2 announcement policy, Gateway API manifests)
- `coredns/` — CoreDNS cluster DNS manifests (Corefile ConfigMap, RBAC, Deployment, kube-dns Service)
- `rbac/` — Cluster RBAC manifests (kubelet bootstrap, `system:nodes`, admin, apiserver-proxy)
- `research/` — Scheduler research sub-project (see [Research](#research))
- `scripts/` — Helper scripts: `create-cloudinit.sh` (Packer CIDATA disk), `fetch-kubeconfig` (Secret decoder used by `make kubeconfig`), `verify/` (documentation and pipeline verification scripts)
- `tests/` — Python contract tests (pytest): CAPI asset fidelity and Makefile CAPI target contracts
- `docs/` — Supplementary documentation (setup runbook)
- `build/` — Build artifacts (base image, firmware, cloud-init disk, `kubeconfig`)

## Version Pins

Component versions are pinned in the sources below — keep them in sync when bumping.

| Component | Version | Source of truth |
|---|---|---|
| Packer | 1.11.2 | `mise.toml` |
| kubectl | 1.32.0 | `mise.toml` |
| uv | 0.12.3 | `mise.toml` |
| Base OS | Fedora 44 | `Makefile` (FEDORA_CLOUD_URL) / `packer/vars.pkrvars.hcl` |
| Kernel | 7.1 | `extensions/download-sysexts.sh` |
| kubelet | v1.32.13 | `extensions/download-sysexts.sh` |
| cri-o | v1.35.5 | `extensions/download-sysexts.sh` |
| crun | 1.28 | `extensions/download-sysexts.sh` |
| CNI plugins | v1.9.1 | `extensions/download-sysexts.sh` |
| etcd | v3.5.17 | `extensions/download-sysexts.sh` |
| kubernetes-cp | v1.32.13 | `extensions/download-sysexts.sh` |
| tracebox (perfetto) | v57.2 | `extensions/download-sysexts.sh` |
| Kubernetes (workload cluster) | v1.32.13 | `capi/cluster.yaml` (`topology.version`, matches the kubelet/kubernetes-cp pins) |
| Cilium | v1.19.6 | `cilium/install/VERSION` |
| Gateway API CRDs | v1.4.0 | `cilium/install/00-gateway-api-crds.yaml` |
| packer-plugin-cloud-hypervisor | 952aa92 | Makefile `PACKER_PLUGIN_VERSION` |

Management-plane component versions (capishim images, upstream Cluster API source ref, provider image tag) are pinned in their own repositories — see `capishim/docs/install.md` and `cluster-api-hypervisor/VERSIONS.md`.

## Design Decisions

- **Declarative cluster lifecycle**: The cluster exists as a checked-in manifest applied with `kubectl apply --server-side`. No imperative provisioning code decides what the cluster looks like — changing the cluster means changing `capi/cluster.yaml` and re-applying. Concrete values (counts, sizes, versions) are committed rather than templated, so the manifest documents exactly what runs.

- **Rootless everywhere**: The management plane, provider manager, network daemon, and VM processes all run as systemd user units backed by rootless podman. No `make` target uses `sudo`; the only privileged setup step is `kvm` group membership.

- **Sibling repos own their installs**: capishim, k8netd, and cluster-api-hypervisor are installed from their own checkouts following their own docs. k8labs drives them (quadlet units, kubeconfigs, manifests) but never builds or installs them.

- **Immutable base + sysext/confext layering**: The base OS is baked once and treated as immutable. All Kubernetes and container runtime components are delivered as systemd-sysext images and all static node configuration as systemd-confext images baked into the base image by Packer. This pattern mirrors production-ready approaches like Flatcar Linux or Fedora CoreOS; bumping a component version re-bakes the base image (`make base` depends on the extension builds).

- **Addons as ClusterResourceSets**: RBAC, Cilium, and CoreDNS ship as CRS resources selected by cluster label, so a freshly created cluster converges to a usable state without post-create steps — and the embedded payloads are contract-tested against the source manifests.

- **API-path-only host access**: The host reaches the workload apiserver (passt-forwarded :6443) and nothing else. LB IPs stay inside the L2 segment; probes run in-cluster. This keeps the dataplane entirely in userspace (k8netd) with zero host network configuration.

- **Headless Packer build**: The base image is built without a display using a Fedora Cloud Base image with cloud-init SSH key injection — no kickstart or ISO modification needed.

- **Cloud-Hypervisor VMM**: CH was adopted for its fast VM startup (~200--400 ms per VM), smaller binary, stateless operation (no libvirtd daemon), and cloud-native VMM design (virtio-only, no legacy emulation).

## Research

`research/` is a sub-project for scheduler research against the live cluster: container process lifecycle tracing, CPU throttling characterization (cgroup v2 `cpu.stat` under varying CPU limits), and CPU request/limit guidance, including EEVDF scheduler experiments. Workloads are Go services in `research/cpu-sched/workloads/` (cpu-burner, api-server, db-simulator), each with its own Makefile; analysis is Python in `research/cpu-sched/analysis/` (deps in `pyproject.toml`); Perfetto tracing uses the baked `perfetto` sysext.

Experiments are driven with namespaced `make cpu-sched-experiment-*` targets from `research/` (or `make -C research <target>` from the repo root) and write gitignored data under `research/cpu-sched/experiments/data/` and `research/cpu-sched/analysis/output/`. They require a live, healthy cluster (3 Ready nodes) and a working kubeconfig at the repo-root `kubeconfig` path (the same file the legacy cluster-ops targets use). See [research/README.md](research/README.md) for the series layout and [research/cpu-sched/README.md](research/cpu-sched/README.md) for the full experiment guide.

## Gateway API

This lab includes Cilium's built-in Gateway API controller, providing Kubernetes-native L7 traffic management. The Gateway API is a SIG-Network standard that supersedes the Ingress API with a role-oriented, portable, and extensible resource model.

### Configuration

Gateway API support is enabled by default via the committed Cilium manifests, which reach the cluster through the `cilium-crs` ClusterResourceSet at creation (or manually via `make cilium`):

1. Installs Gateway API CRDs (bundle v1.4.0: GatewayClass, Gateway, HTTPRoute, GRPCRoute, ReferenceGrant, TLSRoute)
2. Enables the Gateway API controller via `enable-gateway-api` in the Cilium ConfigMap
3. Applies a `CiliumGatewayClassConfig` that configures the generated LoadBalancer service type and external traffic policy
4. Applies example Gateway API resources (GatewayClass, Gateway, HTTPRoute)

All Gateway API manifests are in `cilium/`:

| File | Resource | Purpose |
|------|----------|---------|
| `gateway-class-config.yaml` | CiliumGatewayClassConfig | Configures LoadBalancer service for Gateway listeners |
| `gatewayclass.yaml` | GatewayClass | Defines the `cilium` GatewayClass with Cilium controller |
| `gateway.yaml` | Gateway | Example HTTP gateway with port 80 listener |
| `http-route.yaml` | HTTPRoute | Demo route routing to echo-service:80 |

### Usage

The example Gateway exposes an HTTP listener on port 80 using the L2 LoadBalancer IP pool (10.0.10.0/24). Because the host has no route into that pool (see [Host reachability model](#host-reachability-model)), exercise the Gateway from inside the cluster — `make smoke-test` does exactly this by probing `http://cilium-gw.default.svc.cluster.local/`. To add your own routes, edit `cilium/http-route.yaml` and re-apply it through the cluster (update the Secrets backing `cilium-crs`, or `kubectl --kubeconfig build/kubeconfig apply -f cilium/http-route.yaml`).

For more details, see the [Cilium Gateway API documentation](https://docs.cilium.io/en/latest/network/servicemesh/gateway-api/gateway-api/).

## Cluster DNS (CoreDNS)

CoreDNS provides in-cluster DNS resolution for the cluster. Pods resolve Kubernetes
Service names through the conventional `kube-dns` Service (clusterIP `10.96.0.10`,
matching `cluster_dns_ip`), with cluster domain `cluster.local`. The kubelet
`clusterDNS: ["10.96.0.10"]` setting on every node injects that nameserver into each
pod's `/etc/resolv.conf` together with the `cluster.local` search domains, so Service
names like `kubernetes.default.svc.cluster.local` resolve from any pod.

The Corefile (`coredns/corefile-configmap.yaml`) serves the `kubernetes cluster.local`
zone authoritatively and forwards everything else to the lab gateway DNS at
`192.168.124.1` (`forward . 192.168.124.1`) — k8netd's DNS forwarder, which resolves
upstream via its configured resolvers (1.1.1.1/8.8.8.8 by default).

### Deployment

CoreDNS reaches the cluster through the `coredns-crs` ClusterResourceSet at creation.
To deploy or re-deploy on an already-running cluster (idempotent):

```
make coredns
```

The target applies the manifests, waits for `deployment/coredns` to be `Available`,
and verifies the `kube-dns` Service clusterIP is `10.96.0.10`.

### Verification

```
# Service and deployment
kubectl --kubeconfig build/kubeconfig -n kube-system get svc kube-dns
kubectl --kubeconfig build/kubeconfig -n kube-system rollout status deployment/coredns

# In-cluster resolution (from any pod)
kubectl --kubeconfig build/kubeconfig exec -it <pod> -- getent hosts kubernetes.default.svc.cluster.local   # -> 10.96.0.1

# Negative and external checks
kubectl --kubeconfig build/kubeconfig exec -it <pod> -- nslookup does-not-exist.cluster.local              # NXDOMAIN from 10.96.0.10
kubectl --kubeconfig build/kubeconfig exec -it <pod> -- getent hosts example.com                           # external forward
```
