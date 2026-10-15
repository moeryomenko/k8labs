# linux-sched — environment gate findings

Recorded Phase 2 (kernel and tooling compatibility) results for the lab this
series runs on. A gate that fails stops the series before any workload run.

Measured on the live cluster (`build/kubeconfig`), worker
`k8labs-md-0-2fsd4-s5k98-pzps7`, by loading a read-only host mount into a
non-privileged Pod and reading the results from the container termination
message (see "Transport" below).

## Kernel — PASS

| Check | Value |
|---|---|
| Kernel release | `7.2.7-200.fc44.x86_64` |
| `CONFIG_SCHED_CLASS_EXT` | `y` |
| `CONFIG_BPF_SYSCALL` / `CONFIG_BPF_JIT` | `y` / `y` |
| `CONFIG_DEBUG_INFO_BTF`, BTF blob | `y`, `/sys/kernel/btf/vmlinux` present |
| `/sys/kernel/sched_ext` | present |
| CID-form sched_ext (`scx_bpf_cid` kfuncs, `sched_ext_ops_cid`) | present |
| Worker vCPU | 2 |

The stock Fedora kernel already exposes the **CID-form** sched_ext API that
`scx_mavd` needs, so no custom kernel build is required for that reason.

Note: `versions.lock.yaml` pins kernel `7.2.5-200.fc44` while the running nodes
report `7.2.7-200.fc44`; the lock and the image need reconciling before
confirmatory runs.

## Observer tooling — node-native

`scx-scheds` is **not** in the Fedora 44 repositories. The scheduler binaries are
therefore built from the local `scx/` checkout (the build host has clang 23,
above the clang 22 floor for `scx_mavd`).

The observer tools *are* packaged and are installed into the node image by
`packer/scripts/01-configure-system.sh`:

| Package | Provides |
|---|---|
| `bpftrace` | the `sched_*` trace probe |
| `kernel-tools` | `perf sched` |
| `bcc-tools` | `runqlat`, `runqlen` |
| `python3-bcc` | BCC Python bindings |
| `bpftool` | BTF inspection |

`linux-sched-observer-check` fails if any of these leave the image provisioner
or if a privileged observer manifest reappears.

## Scheduler builds — PASS

The three sched_ext schedulers build from the local `scx/` checkout:

| Item | Value |
|---|---|
| Source revision | `f396a010f873bea5cb4f9f1d621a25e3eecd087f` |
| Build host toolchain | cargo/rustc 1.99.0, clang 23.1.3 |
| `scx_bpfland`, `scx_lavd`, `scx_mavd` | all build |
| `bpftool`, `pahole` | not required (scx uses the vendored `vmlinux.tar.zst`) |

`scx_mavd` builds with clang 23, above the clang 22 floor the cid form requires.
Performance measurements must use **release** builds; debug builds are only for
the build gate. Release artifacts (`target/release/`):

| Scheduler | Bytes | sha256 |
|---|---|---|
| `scx_bpfland` | 9046038 | `b13a1365583d2c5456a2f6dc3a992ca07bf633308bc2920223195818e0ae48f0` |
| `scx_lavd` | 12862251 | `6ceff4e0f369e06fd9d9d69e7f4cbb7626151f918bce929aa805c1dffe98245f` |
| `scx_mavd` | 13239568 | `0d89e6b1270e8feb228db23fdee4098af7c9306a0a390331215a2e3e47f8c981` |

## Observer DaemonSet — deployed and verified

A capability-scoped observer DaemonSet (`observers/observer-daemonset.yaml`)
runs one Pod per research worker. Verified on the live cluster:

- 3/3 observers Ready on the three `k8labs-md-0-*` workers; the control-plane
  node is excluded because it is not labelled.
- Inside an observer: `bpftrace` counts host scheduling activity
  (`tracepoint:sched:sched_switch` → 5925 events in 2 s).

**Node roles are unlabelled.** Every node reports `ROLES <none>` and the
control-plane node carries neither `node-role.kubernetes.io/control-plane` nor a
taint. Any `!node-role.kubernetes.io/control-plane` selector therefore matches
*all* nodes. Membership is expressed by the opt-in label
`linux-sched/research=observer` instead.

## Kernel pin — VERIFIED on the live cluster

The redeployed cluster runs the pinned kernel on every node:

| Node | Kernel |
|---|---|
| control plane | `7.2.10-200.fc44.x86_64` |
| 3 workers | `7.2.10-200.fc44.x86_64` |

Inside an observer pod: `uname -r` → `7.2.10-200.fc44.x86_64`,
`/lib/modules/7.2.10-200.fc44.x86_64/build` **present**, `/usr/src/kernels/`
holds the matching tree. The `kernel-devel` install and the read-only
`/lib/modules` + `/usr/src/kernels` mounts do what they were meant to.

## BCC — still blocked, for a different reason

With headers now available, BCC fails to *compile* instead of failing to find
headers. Fedora 44 ships BCC 0.35, which predates changes in the 7.2 headers:

```
include/linux/fs.h:2440:15: error: static assertion failed due to requirement
'sizeof(struct filename) % 64 == 0'
```

This is a version incompatibility, not a missing-package problem, so pinning the
kernel was necessary but not sufficient. Options: a newer BCC (none available
for Fedora 44 at the time of writing), building BCC from source, or replacing
the BCC run-queue tools with bpftrace equivalents.

`bpftrace` is unaffected — it uses BTF and works on the new kernel
(`tracepoint:sched:sched_switch` → 6309 events in 2 s).

## Management-plane provider defect (workaround applied)

The pinned provider image cannot reach the workload apiserver, so a cluster can
never finish provisioning:

| Fact | Value |
|---|---|
| Pinned/ live image | `ghcr.io/moeryomenko/cluster-api-hypervisor@sha256:e0fc1a9a…` |
| Built from source commit | `4206c463` (v0.3.15) |
| `21ac0d1 fix: use published management endpoint` in that commit? | **No** |
| Present in `cluster-api-hypervisor` HEAD (`8a11cc4`)? | **Yes** |

The pre-fix image calls the healthz seam with a literal `127.0.0.1`, which
inside the provider pod is *the pod's own* loopback, so the probe always fails,
`ControlPlaneInitialized` stays false, and the `<cluster>-kubeconfig` secret is
never published. The source fix passes `host.containers.internal`, which a
management pod resolves to `169.254.1.2` and can reach.

`hostNetwork: true` does **not** help: the Kind node container uses
`NetworkMode=bridge`, so its loopback is not the lab host's either.

Workaround applied (stopgap, not committed): a `loopback-forward` socat sidecar
in the provider Deployment forwards `127.0.0.1:20009`→`169.254.1.2:20009` and
`127.0.0.1:20011`→`169.254.1.2:20011`, the published apiserver and etcd ports.

**Durable fix:** release a provider image from `cluster-api-hypervisor` HEAD
(contains `21ac0d1`), update `versions.lock.yaml` (source commit + image
digest), `make prepare`, redeploy. Until then, any management-plane rebootstrap
reverts the sidecar and re-breaks cluster creation.

## Transport — FIXED (logs/exec), SSH not used

**Capability-scoped Pods work.** A Pod with `hostPID: true` and capabilities
`BPF`, `PERFMON`, `SYS_ADMIN`, `SYS_RESOURCE`, `SYS_PTRACE`, `IPC_LOCK` starts
normally — unlike a `privileged: true` Pod, which fails with
`mknodat /dev/loop0: Permission denied`. In that Pod, `bpftrace` attaches probes
and observed host scheduling activity (8598 `sched_switch` events in 3 s). So
BPF tracing does **not** require the privileged flag on this lab.

**`kubectl logs` / `exec` were forbidden, now fixed.** The apiserver's kubelet
client authenticates as `kube-apiserver`, but
`rbac/03-kube-apiserver-proxy.yaml` only bound `kubernetes`, so every call failed
with `Forbidden (user=kube-apiserver, ... resource=nodes, subresource=[proxy])`.
Adding the `kube-apiserver` subject to `system:kube-apiserver-proxy` repairs it:

```
kubectl -n kube-system logs cilium-8mf8v --tail=2   # now returns log lines
kubectl -n kube-system exec cilium-8mf8v -- true     # now succeeds
```

The manifest change is committed so a rebuilt cluster keeps the fix.

Still broken: **SSH to the VMs.** TCP connects to ports 22 on all four nodes, but
no SSH banner ever arrives (`Connection timed out during banner exchange`), so
any SSH-driven transport remains unusable.

What works today: image pulls, `kubectl apply/get/delete/logs/exec`,
capability-scoped and non-privileged Pods, read-only host mounts, and the
container termination message as a small (≈4 KiB) result channel.

## Packaging notes (Fedora 44)

| Tool | Package | Path |
|---|---|---|
| `bpftrace` | `bpftrace` | `/usr/bin/bpftrace` |
| `perf` | `perf` (not `kernel-tools`) | `/usr/bin/perf` |
| `runqlat`, `runqlen` | `bcc-tools` | `/usr/share/bcc/tools/` |
| BCC Python bindings | `python3-bcc` | — |
| `bpftool` | `bpftool` | `/usr/bin/bpftool` |

`runqlat`/`runqlen` are **not** on `$PATH`; they live under
`/usr/share/bcc/tools/`.

## Consequence for the design

- Observer tooling is provisioned into the node image (node-native) and can also
  run from a capability-scoped Pod.
- The runner still needs one working **result transport** before confirmatory
  runs: repair the apiserver→kubelet `nodes/proxy` authorization (restores
  `logs`/`exec`), repair VM SSH, or publish results from the node through the
  Kubernetes API.

