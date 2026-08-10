# k8labs

Kubernetes cluster lab environment for experimentation and research. k8labs provisions disposable, reproducible Kubernetes clusters on local KVM infrastructure using a "Kubernetes the Hard Way" approach, layered with system extensions (sysext/confext) and Cilium CNI with L2-aware load balancing.

The project is designed for hands-on exploration of Kubernetes internals — cluster bootstrapping, certificate management, CNI configuration, service load balancing, and OS-level extension mechanics — without depending on cloud providers.

The VM infrastructure uses **Cloud-Hypervisor** (CH) as the VMM — offering fast VM startup (~200--400 ms), a small dependency chain (no libvirtd), and a stateless architecture where CH processes are managed directly by the Terraform provider.

## Purpose

k8labs exists to enable practical research into Kubernetes cluster internals. Rather than abstracting away complexity with managed services or `kubeadm`, k8labs builds each cluster component explicitly, giving full visibility into every layer: from the boot ISO and base OS image, through certificate generation and etcd clustering, to kubelet registration, kubeconfig distribution, and CNI plugin configuration.

Lab scenarios include:

- **Kubernetes the Hard Way** — manual bootstrapping of control plane components (etcd, API server, controller manager, scheduler) and worker node registration.
- **Cilium CNI networking** — installation, configuration, and exploration of Cilium's eBPF-based networking, including L2 announcements for LoadBalancer services and Gateway API for L7 traffic management.
- **System extension layering** — using systemd-sysext and systemd-confext to overlay Kubernetes and container runtime binaries and configuration onto a minimal base OS image, demonstrating immutable OS extension patterns.
- **Certificate lifecycle** — generation and distribution of TLS certificates for etcd, kubelet, API server, and service accounts using the OpenTofu `tls` provider during the phase-B `make configure` step.
- **Infrastructure as Code** — full pipeline from Packer image baking through OpenTofu VM provisioning (phase A) and tofu-generated runtime configuration (phase B), all running locally on KVM.

## Architecture

The cluster environment is built in stages, each producing an artifact consumed by the next:

1. **Base OS image** (Packer) — a minimal Fedora image built from a Fedora Cloud Base qcow2 using the [cloudhypervisor builder plugin](https://github.com/moeryomenko/packer-plugin-cloud-hypervisor), with a pinned kernel version and SSH access configured via cloud-init. The image bakes every node prerequisite: the seven systemd-sysext images (kubelet, CRI-O, crun, CNI, etcd, kubernetes-cp, perfetto) under `/var/lib/extensions/`, the three static systemd-confext images (`confext-cri-o`, `confext-kubernetes`, `confext-containers`) under `/var/lib/confexts/`, the `conmon`/`parted`/`growpart` packages, a first-boot root-resize helper (`/usr/local/sbin/resize-rootfs.sh`), the SELinux `ebpf-fix` policy module, and enabled `systemd-sysext`/`systemd-confext` services. The resulting qcow2 image serves as the immutable base for all cluster VMs.

2. **System extensions** (sysext/confext) — Kubernetes and container runtime binaries are packaged as systemd-sysext images that overlay `/usr/`; runtime-independent node configuration (kubelet config, CRI-O config, container policy) is packaged as systemd-confext images that overlay `/etc/`. Both are baked into the base image by Packer and merged at first boot by the enabled `systemd-sysext`/`systemd-confext` services. This keeps the base OS pristine and delivers node configuration as immutable, versioned extension images rather than mutating the live filesystem.

3. **VM provisioning, phase A** (OpenTofu) — a [cloudhypervisor provider](https://github.com/moeryomenko/tf-provider-cloud-hypervisor) configuration provisions control plane and worker VMs from the base image, with cloud-init metadata (hostname, SSH keys, first-boot root resize) attached as a FAT16 CIDATA disk. VM specifications (CPU, RAM, disk) are configurable per node. VMs use TAP devices on a shared Linux bridge (`k8sbr0`) with DHCP from systemd-networkd and DNS forwarding via dnsmasq. At first boot the baked sysext/confext images are merged; no Kubernetes service is enabled yet.

4. **Runtime configuration, phase B** (`make configure`) — a second OpenTofu root module (`terraform/runtime/`) discovers each node's DHCP IP, generates the cluster PKI (CA, apiserver, kubelet, etcd, service-account, front-proxy certs), and renders role-split systemd-confext images (`z-etcd`, `z-kubernetes-cp`, `z-kubelet-<node>`) containing the runtime-dependent configuration (certs, kubeconfigs, CP-IP-embedded config). It pushes them to `/var/lib/confexts/` on each node over SSH, runs `systemd-confext refresh`, and enables/starts the Kubernetes services in dependency order (crio -> etcd -> apiserver -> controller-manager -> scheduler; workers crio -> kubelet). The push is hash-conditional, so re-runs skip unchanged nodes.

5. **Cluster operations** — host `kubectl` applies cluster-level resources: `make rbac` (kubelet bootstrap, `system:nodes`, admin bindings), `make cilium` (committed Cilium manifests, Gateway API CRDs, LB pool, L2 policy), and `make coredns` (cluster DNS). `make smoke-test` gates the whole result.

6. **Load balancing pool** — Cilium L2 announcements provide LoadBalancer service IPs from a dedicated `10.0.10.0/24` pool. A declarative host route on the management machine (`10.0.10.0/24 dev k8sbr0`, declared in `network/k8sbr0.network` as `[Route] Destination=10.0.10.0/24`) bridges traffic into the cluster's virtual network.

## Technologies

| Layer | Technology |
|---|---|
| Hypervisor | KVM (Cloud-Hypervisor) |
| Base OS | Fedora (kernel 7.1) |
| VM provisioning | OpenTofu / Terraform (cloudhypervisor provider) |
| Image baking | Packer (cloudhypervisor builder on Fedora Cloud) |
| Node configuration | Image-baked systemd-sysext / systemd-confext + tofu-pushed runtime confexts |
| PKI | OpenTofu `tls` provider (phase B) |
| Container runtime | CRI-O with crun |
| CNI / Service Mesh | Cilium (eBPF, L2 announcements, Gateway API) |
| Service discovery | etcd |
| OS extensions | systemd-sysext / systemd-confext |

## Quick Start

Requirements: a Linux host with KVM-capable hardware, `cloud-hypervisor` (>= v38), `tofu` (or `terraform`), `packer`, `openssl`, `mkdosfs` (dosfstools), `mcopy` (mtools), and `dnsmasq`. Run `make prereq` to validate core tools. The Packer plugin must be built from source with `make plugin`.

The full cluster build pipeline is driven through a single Makefile:

```
make cluster
```

This executes the complete sequence: bake the base OS image with the system and configuration extensions baked in, provision VMs via OpenTofu (phase A), wait for DHCP leases and SSH connectivity, generate PKI and push role confexts via `make configure` (phase B), apply RBAC/Cilium/CoreDNS, and fetch a working kubeconfig before running the smoke test.

Individual pipeline stages can be run separately:

**Image baking:**

| Target | Description |
|--------|-------------|
| `make base` | Build base image from Fedora Cloud + cloud-init |
| `make base-rebuild` | Force rebuild base image |
| `make base-deps` | Download CLOUDHV.fd firmware + Fedora Cloud Base qcow2 |
| `make base-cloudinit` | Generate FAT16 CIDATA disk for Packer SSH key injection |
| `make plugin` | Build and install [cloudhypervisor Packer plugin](https://github.com/moeryomenko/packer-plugin-cloud-hypervisor) from source |

**Extensions:**

| Target | Description |
|--------|-------------|
| `make sysexts` | Build all system extension images |
| `make confexts` | Build all configuration extension overlays |
| `make extensions` | Build all extensions (both sysext and confext) |

**Networking:**

| Target | Description |
|--------|-------------|
| `sudo make network-up` | Create bridge `k8sbr0` + TAP devices + DHCP via systemd-networkd |
| `sudo make network-down` | Tear down TAPs, bridge, and remove k8slab nftables table |

**VM provisioning:**

| Target | Description |
|--------|-------------|
| `make deploy` | Phase A: provision cluster VMs via OpenTofu (cloudhypervisor provider) |
| `make wait-ips` | Poll until all VMs have DHCP-assigned IPs |
| `make wait-ssh` | Poll until SSH is reachable on all VMs |
| `make configure` | Phase B: generate PKI + role confexts and activate services (`terraform/runtime`) |
| `make destroy` | Tear down all VMs |
| `make destroy-full` | Destroy VMs, runtime state, certs, and kubeconfig |

**Cluster operations:**

| Target | Description |
|--------|-------------|
| `make rbac` | Apply cluster RBAC (kubelet bootstrap, `system:nodes`, admin, apiserver-proxy) |
| `make cilium` | Install Cilium from committed manifests (Gateway API CRDs, LB pool, L2 policy) |
| `make smoke-test` | Validate cluster health (nodes Ready, pods Running, Cilium healthy, Gateway API resources, test pod scheduling, CoreDNS DNS regression) |
| `make coredns` | Deploy CoreDNS cluster DNS (kube-dns Service at 10.96.0.10) |
| `make metrics-server` | Enable `kubectl top` (metrics API via aggregation layer) |
| `make start` / `make stop` | Start/stop VMs via ch-remote API (ACPI shutdown) |
| `make kubeconfig` | Fetch DHCP-resistant kubeconfig from control-plane |
| `make update-kubeconfig` | Refresh kubeconfig after DHCP IP change |

Pre-built dependencies are cached: re-running `make cluster` skips stages whose artifacts already exist. Use `make base-rebuild` to force re-baking the base image.

## Project Structure

The repository is organized by concern, not by lifecycle stage:

- `packer/` — Packer templates (`base.pkr.hcl`), cloud-init configs, and provisioning scripts for base image baking (bakes the sysext/confext images)
- `terraform/` — OpenTofu/Terraform configuration for VM definitions (cloudhypervisor provider, phase A), bridge TAP networking, and cloud-init
- `terraform/runtime/` — Phase-B root module: PKI generation (tls provider) and role-confext rendering/pushing
- `extensions/` — Build scripts and download utilities for systemd-sysext and systemd-confext packaging
- `sysext/` — Raw system extension directory structures (binaries, systemd units)
- `confext/` — Raw configuration extension directory structures (kubelet config, CRI-O config, container policy)
- `certs/` — Generated TLS certificates (output artifact, gitignored except `.gitkeep`)
- `cilium/` — Cilium manifest templates (L2 announcement policy, LB IP pool, Gateway API manifests)
- `coredns/` — CoreDNS cluster DNS manifests (Corefile ConfigMap, RBAC, Deployment, kube-dns Service)
- `scripts/` — Standalone helper scripts
- `build/` — Build artifacts (base image, extensions, temporary files)

## Design Decisions

- **KTHW-style bootstrap**: Every Kubernetes component is configured explicitly (certificates, kubeconfigs, systemd units, manifests). This is deliberate — the goal is to understand how the components interconnect, not to minimize keystrokes.

- **Immutable base + sysext/confext layering**: The base OS is baked once and treated as immutable. All Kubernetes and container runtime components are delivered as systemd-sysext images and all static node configuration as systemd-confext images baked into the base image by Packer; runtime-dependent configuration (PKI, kubeconfigs, CP-IP-embedded config) arrives later as tofu-pushed role confexts. This pattern mirrors production-ready approaches like Flatcar Linux or Fedora CoreOS; bumping a component version re-bakes the base image (`make base` depends on the extension builds).

- **Two-phase tofu delivery**: Phase A boots immutable VMs that merge only the baked static extensions; phase B (`make configure`) generates the runtime PKI and pushes role-split confexts, so no post-boot configuration mutates the node filesystem and the host needs no configuration-management runtime beyond `tofu`, `ssh`/`scp`, and `kubectl`.

- **MAC-based IP resolution**: The Makefile reads the systemd-networkd DHCP server lease file (`/var/lib/systemd/network/dhcp-server-lease/k8sbr0`) with MAC address matching to reliably resolve VM IPs after provisioning — no reliance on Terraform outputs or libvirt. A `scripts/vm-ip.sh` helper provides the lookup for both systemd-networkd and legacy dnsmasq lease formats.

- **Headless Packer build**: The base image is built without a display using a Fedora Cloud Base image with cloud-init SSH key injection — no kickstart or ISO modification needed.

- **Cloud-Hypervisor VMM**: CH was adopted for its fast VM startup (~200--400 ms per VM), smaller binary, stateless operation (no libvirtd daemon), and cloud-native VMM design (virtio-only, no legacy emulation).

## Gateway API

This lab includes Cilium's built-in Gateway API controller, providing Kubernetes-native L7 traffic management. The Gateway API is a SIG-Network standard that supersedes the Ingress API with a role-oriented, portable, and extensible resource model.

### Configuration

Gateway API support is enabled by default via the committed Cilium install manifests (`make cilium`):

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

Gateway API resources are applied automatically during `make cluster` or `make cilium`. To add your own routes:

```
kubectl apply -f cilium/http-route.yaml
```

The example Gateway exposes an HTTP listener on port 80 using the L2 LoadBalancer IP pool (10.0.10.0/24). Gateway API resources are verified during `make smoke-test`.

For more details, see the [Cilium Gateway API documentation](https://docs.cilium.io/en/latest/network/servicemesh/gateway-api/gateway-api/).

## Cluster DNS (CoreDNS)

CoreDNS provides in-cluster DNS resolution for the cluster. Pods resolve Kubernetes
Service names through the conventional `kube-dns` Service (clusterIP `10.96.0.10`,
matching `cluster_dns_ip`), with cluster domain `cluster.local`. The kubelet
`clusterDNS: ["10.96.0.10"]` setting on every node injects that nameserver into each
pod's `/etc/resolv.conf` together with the `cluster.local` search domains, so Service
names like `kubernetes.default.svc.cluster.local` resolve from any pod.

The Corefile (`coredns/corefile-configmap.yaml`) serves the `kubernetes cluster.local`
zone authoritatively and forwards everything else to the lab DNS forwarder — the host
dnsmasq on the bridge at `192.168.124.1` (`forward . 192.168.124.1`), which forwards
upstream to 1.1.1.1/8.8.8.8.

### Deployment

CoreDNS is deployed during `make cluster`/`make coredns`, which applies the
`coredns/` manifests with host `kubectl`. To deploy or re-deploy on an
already-running cluster (idempotent):

```
make coredns
```

The target applies the manifests, waits for `deployment/coredns` to be `Available`,
and verifies the `kube-dns` Service clusterIP is `10.96.0.10`.

### Verification

```
# Service and deployment
kubectl -n kube-system get svc kube-dns
kubectl -n kube-system rollout status deployment/coredns

# In-cluster resolution (from any pod)
kubectl exec -it <pod> -- getent hosts kubernetes.default.svc.cluster.local   # -> 10.96.0.1

# Negative and external checks
kubectl exec -it <pod> -- nslookup does-not-exist.cluster.local              # NXDOMAIN from 10.96.0.10
kubectl exec -it <pod> -- getent hosts example.com                           # external forward
```

`make smoke-test` includes these DNS regression checks (coredns deployment
`Available`, `kube-dns` clusterIP `10.96.0.10`, in-cluster FQDN resolution, NXDOMAIN
negative, external forward) alongside the existing cluster health checks.
