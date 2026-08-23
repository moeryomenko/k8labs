# One-time setup runbook

This page is the detailed version of the README's [One-time setup](../README.md#one-time-setup) chapter. It walks through everything that must happen once per host before `make cluster` works.

**Order matters**: capishim first (it owns the management apiserver), then the cluster-api-hypervisor provider bits, then the k8labs-side image bake. The provider's `clusterctl init` talks to the management apiserver, so the plane must be running when you initialize the providers.

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

It fails loudly naming what is missing: tools not on PATH, quadlet units not installed, the capishim kubeconfig absent, or `.venv` missing (run `make node-tools` first).

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

Optional environment overrides (all optional; see capishim's install doc): `CAPISHIM_VERSION` (default `v0.1.0`), `CAPISHIM_STATE_DIR` (default `~/.local/share/capishim`), `CAPISHIM_BIND_ADDRESS` (default `127.0.0.1:6443`), `CAPI_SOURCE_REF` (default `v1.14.0`).

Verify:

```sh
systemctl --user status capishim-pod
KUBECONFIG=~/.kube/capishim.kubeconfig kubectl get ns
```

An empty node table / namespace list is the expected healthy response — the management apiserver has no nodes.

## 2. cluster-api-hypervisor provider

Sources of truth: `cluster-api-hypervisor/docs/install-contract.md` (image, environment, quadlets, webhook certs) and `cluster-api-hypervisor/docs/clusterctl.md` (components packaging, clusterctl init, CA-bundle patch). Run these steps **in the provider checkout**, not in k8labs.

### 2.1 State directories

```sh
loginctl enable-linger
mkdir -p ~/.local/state/k8slab/build/vm-disks \
         ~/.local/state/k8slab/webhook-certs \
         ~/.local/state/k8slab/kubeconfigs \
         /tmp/ch-capi \
         /run/user/$(id -u)/k8snet
mkdir -p ~/.config/containers/systemd
```

### 2.2 User quadlets

Write the two unit files into `~/.config/containers/systemd/` exactly as specified by install-contract §5.0.1/§5.0.2 (the reference units live at `test/e2e/mgmt/units/` in the provider repo):

- `k8netd.container` — the rootless network daemon (`localhost/k8netd:dev`, state volume, `K8NETD_*` env incl. `K8NETD_PASST_FORWARDS=6443,22`)
- `cluster-api-hypervisor.container` — the provider manager (`localhost/cluster-api-hypervisor:dev`, host network, `/dev/kvm` device, `/build` + `/state` + socket mounts, `HYPERVISOR_*` env, `--kubeconfig` pointing at the management admin kubeconfig)

Then reload:

```sh
systemctl --user daemon-reload
```

Adjust `/run/user/1000/...` paths if your uid is not 1000 (both the mounts and `HYPERVISOR_K8NETD_SOCKET`).

### 2.3 Provider image and release tree

```sh
cd /home/eryoma/workspace/cluster-api-hypervisor
make image        # podman build -t cluster-api-hypervisor:dev (local-only, never published)
make components   # release tree under out/: {infrastructure,bootstrap,control-plane}-hypervisor/v0.1.0/
```

### 2.4 clusterctl configuration

The committed `clusterctl.yaml` carries placeholder base paths; substitute them with your real layout and install where clusterctl reads it:

```sh
sed -e 's|/var/lib/k8slab/out|/home/eryoma/workspace/cluster-api-hypervisor/out|g' \
    -e 's|/var/lib/k8slab/overrides|<your-overrides-dir>|g' \
    clusterctl.yaml > ~/.cluster-api/clusterctl.yaml
```

The rendered `file://` URLs must stay absolute (clusterctl resolves `{basepath}/{provider-label}/{version}/{components.yaml}`).

### 2.5 Webhook certificates

The five admission webhooks are served over TLS from static material — no cert-manager. Before starting the provider, provision into `~/.local/state/k8slab/webhook-certs/`:

1. A self-signed CA (`ca.key`, `ca.crt`) — the trust root for both the serving certs and the later caBundle patch.
2. A serving certificate signed by that CA with SANs covering at least `127.0.0.1` and `localhost`, written as `tls.crt`/`tls.key`.

### 2.6 Initialize the providers

With the management plane running (from k8labs: `make mgmt-up`, or `systemctl --user start capishim-pod`):

```sh
clusterctl init --infrastructure hypervisor --bootstrap hypervisor --control-plane hypervisor --skip-cert-manager
```

`--skip-cert-manager` is required — the webhooks use static certificates, so cert-manager must not be installed.

Then patch the management CA into every webhook entry of both configurations (the components ship them with an empty `caBundle` and `failurePolicy: Fail`; until patched, the first Hypervisor admission fails):

```sh
CA=$(base64 -w0 <path-to-mgmt-ca.pem>)
kubectl --kubeconfig ~/.kube/capishim.kubeconfig patch mutating-webhook-configuration --type=json \
  -p '[{"op":"replace","path":"/webhooks/0/clientConfig/caBundle","value":"'"$CA"'"}]'
kubectl --kubeconfig ~/.kube/capishim.kubeconfig patch validating-webhook-configuration --type=json \
  -p '[{"op":"replace","path":"/webhooks/0/clientConfig/caBundle","value":"'"$CA"'"}]'
```

Patching an identical bundle is a no-op, so the step is idempotent.

## 3. k8labs side

Back in k8labs, bake what the provider consumes:

```sh
make node-tools   # uv sync -> .venv (make prereq requires it)
make plugin       # build/install the Packer Cloud-Hypervisor plugin (pinned commit)
make base         # build/k8labs-base.qcow2 (+ build/CLOUDHV.fd via base-deps)
```

The provider boots machines from its `/build` mount — by default `%h/.local/state/k8slab/build` (install-contract §5.1). Copy or symlink the baked artifacts there:

```sh
cp build/k8labs-base.qcow2 build/CLOUDHV.fd ~/.local/state/k8slab/build/
```

Re-copy after every `make base` / extension bump — a rebuilt base image does not propagate to already-created machines.

## 4. Verify

```sh
make prereq       # all checks green
make mgmt-up      # three units active, management API responds
make cluster      # mgmt-up -> cluster-up -> addons-up -> wait Ready -> kubeconfig -> smoke test
kubectl --kubeconfig build/kubeconfig get nodes   # 1 control plane + 3 workers, Ready
```
