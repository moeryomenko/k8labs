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

## Transport — PARTIALLY BLOCKED

The originally planned privileged observer DaemonSets cannot run, but the reason
turned out to be narrower than it first appeared.

**Capability-scoped Pods work.** A Pod with `hostPID: true` and capabilities
`BPF`, `PERFMON`, `SYS_ADMIN`, `SYS_RESOURCE`, `SYS_PTRACE`, `IPC_LOCK` starts
normally — unlike a `privileged: true` Pod, which fails with
`mknodat /dev/loop0: Permission denied`. In that Pod, `bpftrace` attaches probes
and observed host scheduling activity (8598 `sched_switch` events in 3 s). So
BPF tracing does **not** require the privileged flag on this lab; the observer
can be a capability-scoped DaemonSet.

Still broken:

1. **SSH to the VMs.** TCP connects to ports 22 on all four nodes, but no SSH
   banner ever arrives (`Connection timed out during banner exchange`). Any
   SSH-driven transport cannot be used.
2. **`kubectl logs` / `exec`.** Forbidden cluster-wide: the apiserver's kubelet
   client lacks `nodes/proxy`
   (`Forbidden (user=kube-apiserver, verb=get, resource=nodes,
   subresource(s)=[proxy])`), reproduced against a Cilium pod. Pod stdout cannot
   be read this way, which is what a full tracing agent would naturally use.

What works today: image pulls, `kubectl apply/get/delete`, capability-scoped and
non-privileged Pods, read-only host mounts, and the container termination
message as a small (≈4 KiB) result channel.

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

