# k8labs

Kubernetes cluster lab environment for experimentation and research. k8labs provisions disposable, reproducible Kubernetes clusters on local KVM/libvirt infrastructure using a "Kubernetes the Hard Way" approach, layered with system extensions (sysext/confext) and Cilium CNI with L2-aware load balancing.

The project is designed for hands-on exploration of Kubernetes internals — cluster bootstrapping, certificate management, CNI configuration, service load balancing, and OS-level extension mechanics — without depending on cloud providers.

## Purpose

k8labs exists to enable practical research into Kubernetes cluster internals. Rather than abstracting away complexity with managed services or `kubeadm`, k8labs builds each cluster component explicitly, giving full visibility into every layer: from the boot ISO and base OS image, through certificate generation and etcd clustering, to kubelet registration, kubeconfig distribution, and CNI plugin configuration.

Lab scenarios include:

- **Kubernetes the Hard Way** — manual bootstrapping of control plane components (etcd, API server, controller manager, scheduler) and worker node registration.
- **Cilium CNI networking** — installation, configuration, and exploration of Cilium's eBPF-based networking, including L2 announcements for LoadBalancer services.
- **System extension layering** — using systemd-sysext and systemd-confext to overlay Kubernetes and container runtime binaries and configuration onto a minimal base OS image, demonstrating immutable OS extension patterns.
- **Certificate lifecycle** — generation and distribution of TLS certificates for etcd, kubelet, API server, and service accounts using Ansible community.crypto modules.
- **Infrastructure as Code** — full pipeline from Packer image baking through Terraform/OpenTofu VM provisioning to Ansible configuration management, all running locally on KVM.

## Architecture

The cluster environment is built in stages, each producing an artifact consumed by the next:

1. **Base OS image** (Packer) — a minimal Fedora installation built from a netinstall ISO via kickstart, with a pinned kernel version and SSH access configured. The resulting qcow2 image serves as the immutable base for all cluster VMs.

2. **System extensions** (sysext/confext) — Kubernetes and container runtime binaries (kubelet, CRI-O, crun, CNI plugins, etcd, API server, controller manager, scheduler, kubectl) are packaged as systemd-sysext images and dropped into `/var/lib/extensions/`. Corresponding configuration overlays (sysctl parameters, module loading, kubelet config, CRI-O config, etcd config) are built as systemd-confext images layered over `/etc/`. This keeps the base OS pristine and allows atomic updates of individual components without re-baking the VM image.

3. **Ansible runner container** (Podman/Containerfile) — a self-contained execution environment with Ansible, community.crypto, community.general, kubectl, and Cilium CLI. All Ansible operations run inside this container for reproducibility.

4. **VM provisioning** (OpenTofu/Terraform) — a libvirt provider configuration that clones the base image for each node, attaches cloud-init metadata (hostname, SSH keys), and provisions control plane and worker VMs on a dedicated NAT network. VM specifications (CPU, RAM, disk) are configurable per node.

5. **Cluster bootstrapping** (Ansible) — Ansible playbooks orchestrate the full cluster bring-up: distributing system extensions, generating and deploying TLS certificates, bootstrapping etcd, initializing the Kubernetes control plane, configuring kubelet on workers, deploying Cilium CNI, and configuring Layer 2 load balancer IP pools. The bootstrap follows the KTHW service sequence (etcd -> API server -> controller manager -> scheduler -> kubelet -> kube-proxy -> CNI).

6. **Load balancing pool** — Cilium L2 announcements provide LoadBalancer service IPs from a dedicated `10.0.10.0/24` pool. A host route on the management machine bridges traffic into the cluster's virtual network.

## Technologies

| Layer | Technology |
|---|---|
| Hypervisor | KVM/QEMU via libvirt |
| Base OS | Fedora (kernel 7.1) |
| VM provisioning | OpenTofu / Terraform (terraform-provider-libvirt) |
| Image baking | Packer (QEMU builder, kickstart) |
| Configuration management | Ansible (community.crypto, community.general) |
| Container runtime | CRI-O with crun |
| CNI | Cilium (eBPF, L2 announcements) |
| Service discovery | etcd |
| OS extensions | systemd-sysext / systemd-confext |
| Runner container | Podman |

## Quick Start

Requirements: a Linux host with KVM-capable hardware, `virsh`, `tofu` (or `terraform`), `packer`, `podman`, and `openssl`.

The full cluster build pipeline is driven through a single Makefile:

```
make cluster
```

This executes the complete sequence: validate prerequisites, bake the base OS image, build all system and configuration extensions, build the Ansible runner container, provision VMs via OpenTofu, wait for DHCP leases and SSH connectivity, bootstrap Kubernetes with Ansible, configure Cilium and L2 announcements, and fetch a working kubeconfig.

Individual pipeline stages can be run separately:

- `make base` — build the base OS qcow2 image
- `make sysexts` — build all system extension images
- `make confexts` — build all configuration extension overlays
- `make extensions` — build all extensions (both sysext and confext)
- `make container` — build the Ansible runner container
- `make deploy` — provision cluster VMs via OpenTofu
- `make wait-ips` — poll until all VMs have DHCP-assigned IPs
- `make wait-ssh` — poll until SSH is reachable on all VMs
- `make bootstrap` — run the full Ansible cluster bootstrap
- `make smoke-test` — validate cluster health (nodes Ready, pods Running, Cilium healthy, test pod scheduling)
- `make destroy` — tear down all VMs
- `make destroy-full` — destroy VMs, certs, kubeconfig, and inventory
- `make start` / `make stop` — gracefully start/stop all cluster VMs

Pre-built dependencies are cached: re-running `make cluster` skips stages whose artifacts already exist. Use `make base-rebuild` to force re-baking the base image.

## Project Structure

The repository is organized by concern, not by lifecycle stage:

- `packer/` — Packer template, kickstart configuration, and provisioning scripts for the base OS image
- `terraform/` — OpenTofu/Terraform configuration for libvirt networking, storage pools, and VM definitions
- `ansible/` — Ansible playbooks, roles (certs, etcd, kubernetes-cp, kubelet, cilium), and dynamic inventory script
- `container/` — Containerfile for the Ansible runner
- `extensions/` — Build scripts and download utilities for systemd-sysext and systemd-confext packaging
- `sysext/` — Raw system extension directory structures (binaries, systemd units)
- `confext/` — Raw configuration extension directory structures (config files, sysctl, sysfs params)
- `certs/` — Generated TLS certificates (output artifact, gitignored except `.gitkeep`)
- `cilium/` — Cilium manifest templates (L2 announcement policy, LB IP pool)
- `scripts/` — Standalone helper scripts
- `build/` — Build artifacts (base image, extensions, temporary files)

## Design Decisions

- **KTHW-style bootstrap**: Every Kubernetes component is configured explicitly (certificates, kubeconfigs, systemd units, manifests). This is deliberate — the goal is to understand how the components interconnect, not to minimize keystrokes.

- **Immutable base + sysext layering**: The base OS is baked once and treated as immutable. All Kubernetes and container runtime components are delivered as system extensions. This pattern mirrors production-ready approaches like Flatcar Linux or Fedora CoreOS and allows iterating on cluster component versions without re-baking images.

- **Podman-based Ansible runner**: Ansible runs inside a container rather than directly on the host. This keeps the host clean of Python/Ansible dependencies and ensures the execution environment matches the tested configuration.

- **MAC-based IP resolution**: The Makefile uses virsh DHCP lease information with MAC address matching (rather than relying solely on Terraform outputs) to reliably resolve VM IPs after provisioning.

- **Headless Packer build**: The base image is built without a display, using a kickstart file delivered via virtual OEMDRV CD-ROM and a modified boot ISO with serial console and SSH in the installer environment.
