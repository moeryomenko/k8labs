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
- **Certificate lifecycle** — generation and distribution of TLS certificates for etcd, kubelet, API server, and service accounts using Ansible community.crypto modules.
- **Infrastructure as Code** — full pipeline from Packer image baking through Terraform/OpenTofu VM provisioning to Ansible configuration management, all running locally on KVM.

## Architecture

The cluster environment is built in stages, each producing an artifact consumed by the next:

1. **Base OS image** (Packer) — a minimal Fedora image built from a Fedora Cloud Base qcow2, with a pinned kernel version and SSH access configured via cloud-init. The resulting qcow2 image serves as the immutable base for all cluster VMs.

2. **System extensions** (sysext/confext) — Kubernetes and container runtime binaries (kubelet, CRI-O, crun, CNI plugins, etcd, API server, controller manager, scheduler, kubectl) are packaged as systemd-sysext images and dropped into `/var/lib/extensions/`. Corresponding configuration overlays (sysctl parameters, module loading, kubelet config, CRI-O config, etcd config) are built as systemd-confext images layered over `/etc/`. This keeps the base OS pristine and allows atomic updates of individual components without re-baking the VM image.

3. **Ansible runner container** (Podman/Containerfile) — a self-contained execution environment with Ansible, community.crypto, community.general, kubectl, and Cilium CLI. All Ansible operations run inside this container for reproducibility.

4. **VM provisioning** (OpenTofu/Terraform) — a cloudhypervisor provider configuration that provisions control plane and worker VMs from the base image, with cloud-init metadata (hostname, SSH keys) attached as a FAT16 CIDATA disk. VM specifications (CPU, RAM, disk) are configurable per node. VMs use TAP devices on a shared Linux bridge (`k8sbr0`) with DHCP from a standalone dnsmasq.

5. **Cluster bootstrapping** (Ansible) — Ansible playbooks orchestrate the full cluster bring-up: distributing system extensions, generating and deploying TLS certificates, bootstrapping etcd, initializing the Kubernetes control plane, configuring kubelet on workers, deploying Cilium CNI, and configuring Layer 2 load balancer IP pools. The bootstrap follows the KTHW service sequence (etcd -> API server -> controller manager -> scheduler -> kubelet -> kube-proxy -> CNI).

6. **Load balancing pool** — Cilium L2 announcements provide LoadBalancer service IPs from a dedicated `10.0.10.0/24` pool. A host route on the management machine bridges traffic into the cluster's virtual network.

## Technologies

| Layer | Technology |
|---|---|
| Hypervisor | KVM (Cloud-Hypervisor) |
| Base OS | Fedora (kernel 7.1) |
| VM provisioning | OpenTofu / Terraform (cloudhypervisor provider) |
| Image baking | Packer (cloudhypervisor builder on Fedora Cloud) |
| Configuration management | Ansible (community.crypto, community.general) |
| Container runtime | CRI-O with crun |
| CNI / Service Mesh | Cilium (eBPF, L2 announcements, Gateway API) |
| Service discovery | etcd |
| OS extensions | systemd-sysext / systemd-confext |
| Runner container | Podman |

## Quick Start

Requirements: a Linux host with KVM-capable hardware, `cloud-hypervisor` (>= v38), `tofu` (or `terraform`), `packer`, `podman`, `openssl`, `mkdosfs` (dosfstools), `mcopy` (mtools), and `dnsmasq`. Run `make prereq` to validate core tools. The Packer plugin must be built from source with `make plugin`.

The full cluster build pipeline is driven through a single Makefile:

```
make cluster
```

This executes the complete sequence: validate prerequisites, bake the base OS image, build all system and configuration extensions, build the Ansible runner container, provision VMs via OpenTofu, wait for DHCP leases and SSH connectivity, bootstrap Kubernetes with Ansible, configure Cilium and L2 announcements, and fetch a working kubeconfig.

Individual pipeline stages can be run separately:

**Image baking:**

| Target | Description |
|--------|-------------|
| `make base` | Build base image from Fedora Cloud + cloud-init |
| `make base-rebuild` | Force rebuild base image |
| `make base-deps` | Download CLOUDHV.fd firmware + Fedora Cloud Base qcow2 |
| `make base-cloudinit` | Generate FAT16 CIDATA disk for Packer SSH key injection |
| `make base-tap` | Create TAP device for Packer build VM |
| `make plugin` | Build and install cloudhypervisor Packer plugin from source |

**Extensions and container:**

| Target | Description |
|--------|-------------|
| `make sysexts` | Build all system extension images |
| `make confexts` | Build all configuration extension overlays |
| `make extensions` | Build all extensions (both sysext and confext) |
| `make container` | Build the Ansible runner container |

**Networking:**

| Target | Description |
|--------|-------------|
| `sudo ./scripts/create-taps.sh N` | Create bridge `k8sbr0` + TAP devices + start dnsmasq for N workers |
| `sudo ./scripts/destroy-taps.sh N` | Tear down TAPs, bridge, and stop dnsmasq |

**VM provisioning:**

| Target | Description |
|--------|-------------|
| `make deploy` | Provision cluster VMs via OpenTofu (cloudhypervisor provider) |
| `make wait-ips` | Poll until all VMs have DHCP-assigned IPs |
| `make wait-ssh` | Poll until SSH is reachable on all VMs |
| `make destroy` | Tear down all VMs |
| `make destroy-full` | Destroy VMs, certs, kubeconfig, and inventory |

**Cluster operations:**

| Target | Description |
|--------|-------------|
| `make bootstrap` | Run the full Ansible cluster bootstrap |
| `make smoke-test` | Validate cluster health (nodes Ready, pods Running, Cilium healthy, Gateway API resources, test pod scheduling) |
| `make start` / `make stop` | Start/stop VMs via ch-remote API (ACPI shutdown) |
| `make kubeconfig` | Fetch DHCP-resistant kubeconfig from control-plane |
| `make update-kubeconfig` | Refresh kubeconfig after DHCP IP change |

Pre-built dependencies are cached: re-running `make cluster` skips stages whose artifacts already exist. Use `make base-rebuild` to force re-baking the base image.

## Project Structure

The repository is organized by concern, not by lifecycle stage:

- `packer/` — Packer templates (`base.pkr.hcl`), cloud-init configs, and provisioning scripts for base image baking
- `terraform/` — OpenTofu/Terraform configuration for VM definitions (cloudhypervisor provider), bridge TAP networking, and cloud-init
- `ansible/` — Ansible playbooks, roles (certs, etcd, kubernetes-cp, kubelet, cilium), and dynamic inventory script
- `container/` — Containerfile for the Ansible runner
- `extensions/` — Build scripts and download utilities for systemd-sysext and systemd-confext packaging
- `sysext/` — Raw system extension directory structures (binaries, systemd units)
- `confext/` — Raw configuration extension directory structures (config files, sysctl, sysfs params)
- `certs/` — Generated TLS certificates (output artifact, gitignored except `.gitkeep`)
- `cilium/` — Cilium manifest templates (L2 announcement policy, LB IP pool, Gateway API manifests)
- `scripts/` — Standalone helper scripts
- `build/` — Build artifacts (base image, extensions, temporary files)

## Design Decisions

- **KTHW-style bootstrap**: Every Kubernetes component is configured explicitly (certificates, kubeconfigs, systemd units, manifests). This is deliberate — the goal is to understand how the components interconnect, not to minimize keystrokes.

- **Immutable base + sysext layering**: The base OS is baked once and treated as immutable. All Kubernetes and container runtime components are delivered as system extensions. This pattern mirrors production-ready approaches like Flatcar Linux or Fedora CoreOS and allows iterating on cluster component versions without re-baking images.

- **Podman-based Ansible runner**: Ansible runs inside a container rather than directly on the host. This keeps the host clean of Python/Ansible dependencies and ensures the execution environment matches the tested configuration.

- **MAC-based IP resolution**: The Makefile reads `/var/lib/misc/dnsmasq/k8sbr0.leases` with MAC address matching to reliably resolve VM IPs after provisioning — no reliance on Terraform outputs or libvirt.

- **Headless Packer build**: The base image is built without a display using a Fedora Cloud Base image with cloud-init SSH key injection — no kickstart or ISO modification needed.

- **Cloud-Hypervisor VMM**: CH was adopted for its fast VM startup (~200--400 ms per VM), smaller binary, stateless operation (no libvirtd daemon), and cloud-native VMM design (virtio-only, no legacy emulation).

## Gateway API

This lab includes Cilium's built-in Gateway API controller, providing Kubernetes-native L7 traffic management. The Gateway API is a SIG-Network standard that supersedes the Ingress API with a role-oriented, portable, and extensible resource model.

### Configuration

Gateway API support is enabled by default during the Ansible bootstrap. The Cilium role:

1. Installs Gateway API v1.1.1 CRDs (GatewayClass, Gateway, HTTPRoute, GRPCRoute, ReferenceGrant, TLSRoute)
2. Enables the Gateway API controller via `gatewayAPI.enabled=true` in Cilium
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

Gateway API resources are applied automatically during `make cluster` or `make bootstrap`. To add your own routes:

```
kubectl apply -f cilium/http-route.yaml
```

The example Gateway exposes an HTTP listener on port 80 using the L2 LoadBalancer IP pool (10.0.10.0/24). Gateway API resources are verified during `make smoke-test`.

For more details, see the [Cilium Gateway API documentation](https://docs.cilium.io/en/latest/network/servicemesh/gateway-api/gateway-api/).
