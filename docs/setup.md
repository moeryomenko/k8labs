# One-time setup runbook

This page is the detailed version of the README's [One-time setup](../README.md#one-time-setup) chapter. It walks through everything that must happen once per host before `make cluster` works.

**Order matters**: capishim first (it owns the management apiserver and installs every provider at pod boot), then the provider-side bits (images + quadlets), then the k8labs-side image bake. Nothing below talks to the management apiserver manually — systemd unit ordering starts the provider after the capishim pod, and `make mgmt-up` additionally gates on the provider's `/readyz` before any cluster is created.

Everything below runs as your unprivileged user except where marked. The single privileged step of the entire lab is adding your user to the `kvm` group.

## 0. Host prerequisites

| Requirement | Why |
|---|---|
| KVM-capable host, user in the `kvm` group | cloud-hypervisor needs `/dev/kvm`; group membership is granted by an administrator (`usermod -aG kvm <user>`, then log out and back in) |
| podman >= 4.7 with quadlet support | quadlet units rely on `Requires=`/`After=`, `PublishPort`, and pod-level env handling from 4.7+ |
| Rootless podman working (`subuid`/`subgid` entries) | standard on Fedora, Arch, Ubuntu 22.04+ |
| systemd user session + `loginctl` | the stack runs as user units; lingering keeps them alive after logout |
| `cloud-hypervisor` (>= v38), `openssl`, `jq`, `kubectl`, `clusterctl`, `python3`, `packer`, `uv`, `mkdosfs`, `mcopy` | checked by `make prereq` / used by the image pipeline |

Verify the tooling any time with:

```sh
make prereq
```

It fails loudly naming what is missing: tools not on PATH (including `podman`), `/dev/kvm` absent, your user not in the `kvm` group, quadlet units not installed, the capishim kubeconfig absent, `.venv` missing (run `make node-tools` first), or the baked artifacts absent (`build/k8labs-base.qcow2`, `build/CLOUDHV.fd` — run `make base` first).

## 1. capishim management plane

Source of truth: `capishim/docs/install.md`. Run these commands **in the capishim checkout**, not in k8labs:

```sh
cd /home/eryoma/workspace/capishim
make images && make install-quadlet && systemctl --user daemon-reload && systemctl --user start capishim-pod
```

What this produces:

- Five container images (`localhost/capishim-{setup,core,cabpk,kcp,capd}:v0.1.0`) plus the pinned stock control-plane images.
- Nine quadlet units (`capishim.pod` + eight `.container` units) installed into `~/.config/containers/systemd/`.
- Lingering enabled for your uid (best-effort).
- Vendored CAPI templates under `${CAPISHIM_STATE_DIR}/templates/`.
- `~/.kube/capishim.kubeconfig` symlinked to `${CAPISHIM_STATE_DIR}/kubeconfigs/admin.kubeconfig`, written by the setup container at boot.
- All three hypervisor providers (CRDs, RBAC, webhook configurations) installed into the management API by the setup container from manifests vendored in the capishim repo, plus the provider's identity material: `${CAPISHIM_STATE_DIR}/kubeconfigs/hypervisor.kubeconfig` and `${CAPISHIM_STATE_DIR}/webhook-certs/hypervisor/tls.crt`+`tls.key`, signed by the pod CA.

Optional environment overrides (all optional; see capishim's install doc): `CAPISHIM_VERSION` (default `v0.1.0`), `CAPISHIM_STATE_DIR` (default `~/.local/share/capishim`), `CAPISHIM_BIND_ADDRESS` (default `127.0.0.1:6443`), `CAPI_SOURCE_REF` (default `v1.14.0`).

Verify:

```sh
systemctl --user status capishim-pod
KUBECONFIG=~/.kube/capishim.kubeconfig kubectl get ns
```

An empty node table / namespace list is the expected healthy response — the management apiserver has no nodes.

## 2. cluster-api-hypervisor provider

Sources of truth: `cluster-api-hypervisor/docs/install-contract.md` (image, environment, quadlet, identity inputs) and `cluster-api-hypervisor/docs/clusterctl.md` (the standalone clusterctl alternative — not the k8labs flow). Run these steps **in the provider checkout**, not in k8labs.

There is nothing manual left in the provider install: the quadlet ships with the repo, and capishim's setup container installs all three hypervisor providers (CRDs, RBAC, webhook configurations) automatically when the pod boots. Providers are never initialized through clusterctl here, and no certificate patching exists.

### 2.1 State directories

```sh
mkdir -p ~/.local/state/k8slab/build/vm-disks \
         /tmp/ch-capi \
         /run/user/$(id -u)/k8snet
mkdir -p ~/.config/containers/systemd
```

Lingering is enabled by capishim's install (section 1). Adjust `/run/user/1000/...` paths if your uid is not 1000 — the same paths appear in the quadlet mounts and `HYPERVISOR_K8NETD_SOCKET`.

### 2.2 Provider image and quadlet

```sh
cd /home/eryoma/workspace/cluster-api-hypervisor
make image            # podman build -> localhost/cluster-api-hypervisor:dev (local-only, never published)
make install-quadlet  # copies deploy/cluster-api-hypervisor.container into ~/.config/containers/systemd/
systemctl --user daemon-reload
```

The committed quadlet (`deploy/cluster-api-hypervisor.container`) mounts the lab build dir at `/build`, the k8slab state root at `/state`, the k8netd runtime dir, the cloud-hypervisor socket dir, and — produced by capishim at pod boot — the webhook serving certs and `hypervisor.kubeconfig` from the capishim state subtree. Defaults match the k8labs layout (`~/.local/state/k8slab`, `~/.local/share/capishim`); adjust the two path variables documented in the unit header if your layout differs.

The k8netd side is installed the same way from its own checkout (see `k8netd/docs/install.md`): build its image with `make image`, copy `deploy/k8netd.container` into `~/.config/containers/systemd/`, and reload. Published host ports are no longer configured statically — k8netd allocates them per VM through its idempotent `PublishPort` RPC out of `K8NETD_PUBLISH_RANGE` (default 20000-21000).

### 2.3 Optional: clusterctl configuration for template rendering

clusterctl is not part of the install flow anymore — capishim owns provider installation. It remains useful as an offline template renderer (`clusterctl generate cluster`) against the provider release tree. Do this only if you intend to render workload-cluster manifests with clusterctl:

```sh
cd /home/eryoma/workspace/cluster-api-hypervisor
make components   # release tree under out/: {infrastructure,bootstrap,control-plane}-hypervisor/v0.1.0/

sed -e 's|/var/lib/k8slab/out|/home/eryoma/workspace/cluster-api-hypervisor/out|g' \
    -e 's|/var/lib/k8slab/overrides|<your-overrides-dir>|g' \
    clusterctl.yaml > ~/.cluster-api/clusterctl.yaml
```

The rendered `file://` URLs must stay absolute (clusterctl resolves `{basepath}/{provider-label}/{version}/{components.yaml}`). This configuration drives `generate cluster` only; provider installation into the management plane happens exclusively through capishim's setup container.

## 3. k8labs side

Back in k8labs, bake what the provider consumes:

```sh
make node-tools   # uv sync -> .venv (make prereq requires it)
make plugin       # alias for `make bake-image` (rootless bake container, CH plugin baked in)
make base         # build/k8labs-base.qcow2 (+ build/CLOUDHV.fd via base-deps)
make provider-state  # publish base image + firmware + SSH key into the provider build mount, create state dirs
```

### 3.1 Rootless bake container

`make base` runs Packer + the Cloud-Hypervisor plugin **inside a rootless podman
container** instead of on the host. The `bake/Containerfile` (built by `make
bake-image` into `localhost/k8labs-bake:dev`) ships Fedora, a statically-linked
packer binary (from the `hashicorp/packer` image, since HashiCorp's release
server is geo-blocked), the cloud-hypervisor static binary, dnsmasq, iproute2,
iptables, and the CH plugin built from source at `PACKER_PLUGIN_REF` (default
`main`). The `bake/bake-net.sh` entrypoint creates the `packer-tap` inside the
container's own network namespace, runs dnsmasq for DHCP+DNS, and NATs the
guest to the Fedora repos — **no host bridge/TAP/dnsmasq/NAT and no root**.
The `make bake-run` step passes `--device /dev/kvm --device /dev/net/tun
--cap-add NET_ADMIN,NET_RAW --sysctl net.ipv4.ip_forward=1` so the container
can drive KVM and own its tap inside its private netns. The old host-side
`make plugin` (building from a local checkout into `~/.packer.d/plugins/`) is
gone; `make plugin` is now an alias for `make bake-image`.

### 3.2 Publish artifacts to the provider

The provider boots machines from its `/build` mount — by default
`%h/.local/state/k8slab/build` (install-contract §5.1). `make provider-state`
creates the provider's state dirs (`build/vm-disks`, `/tmp/ch-capi`) and copies
`build/k8labs-base.qcow2`, `build/CLOUDHV.fd`, and
`build/packer-ssh-key.pub` (as `build/ssh-lab.pub`, the key the provider
injects into every machine's cloud-init) into that mount. Run it after every
`make base` / extension bump — a rebuilt base image does not propagate to
already-created machines.

### 3.3 Management-plane images (CI → ghcr.io)

The capishim, cluster-api-hypervisor, and k8netd images are built and pushed
to `ghcr.io/moeryomenko/*` by each sibling repo's GitHub Actions workflow
(`.github/workflows/push-images.yml`) on push to `main` / tags. The lab pulls
them with `make mgmt-images` (part of `make cluster`), which retags them to
the `localhost/*` names the installed quadlets reference, so the quadlet
`Image=` lines stay unchanged.

### 3.4 Workload-cluster bootstrap is fully automated

No manual steps are required after `make cluster` applies the workload
Cluster. The provider now handles every bootstrap dependency that previously
needed an operator:

- **Per-VM internet egress** — k8netd spawns an egress passt for every
  attached port (not just control-plane machines), so workers can pull the
  pause/Cilium/curl images that the control plane and the smoke-test Job
  need. k8netd classifies WAN egress by the gateway MAC, so pod-CIDR traffic
  between VMs stays on the L2 fabric (what lets Cilium route cross-node
  pod traffic).
- **Initial RBAC** — after the workload apiserver is healthy, the provider
  grants the kubeconfig user (`cluster-ca`) `cluster-admin`, so the
  management-plane ClusterResourceSet controller can connect and apply the
  RBAC/Cilium/CoreDNS addons.
- **Deterministic cluster DNS** — the provider pre-allocates the `kube-dns`
  Service at `10.96.0.10` before node configs render, so the kubelet
  `clusterDNS` is stable; it also points Cilium's agent at the control-plane
  apiserver directly (`k8sServiceHost`/`k8sServicePort`).
- **Apiserver Service-IP DNAT** — every node's confext carries a narrow nft
  rule DNATing only `10.96.0.1:443` to the workload apiserver (loopback on
  the control plane, the CP VM IP on workers). Cilium's config-init runs in
  the host namespace and dials the apiserver via the in-cluster
  `KUBERNETES_SERVICE_HOST` before its own datapath exists; the rule is kept
  narrow (apiserver endpoint only) so Cilium's BPF Service load-balancer
  handles every other Service IP.
- **Bootstrap-data ordering** — a machine whose confext data (PKI, kubelet
  config) is not yet rendered waits before its VM first boots, so workers are
  never created with a missing confext disk.

## 4. Verify

```sh
make prereq       # all checks green
make mgmt-up      # three units active, management API + provider /readyz respond
make cluster      # mgmt-images -> provider-state -> mgmt-up -> cluster-up -> addons-up -> wait Ready -> kubeconfig -> smoke test
kubectl --kubeconfig build/kubeconfig get nodes   # 1 control plane + 3 workers, Ready
```
