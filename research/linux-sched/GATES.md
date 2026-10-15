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

## Transport — BLOCKED

Every transport the original design assumed is currently broken on this lab:

1. **SSH to the VMs.** TCP connects to ports 22 on all four nodes, but no SSH
   banner ever arrives (`Connection timed out during banner exchange`). The
   runner's `ssh_node` transport cannot be used.
2. **`kubectl logs` / `exec`.** Forbidden cluster-wide: the apiserver's kubelet
   client lacks `nodes/proxy`
   (`Forbidden (user=kube-apiserver, verb=get, resource=nodes,
   subresource(s)=[proxy])`), reproduced against a Cilium pod. Collecting results
   from a Pod this way is impossible.
3. **Privileged Pods.** Containers with `privileged: true` fail to start with
   `mknodat /dev/loop0: Permission denied`, so the originally planned privileged
   observer DaemonSets cannot run at all.

What *does* work: image pulls, `kubectl apply/get/delete`, and non-privileged
Pods with a read-only `hostPath` mount.

## Consequence for the design

- Observer tooling is node-native in the image; there are no privileged
  DaemonSets in this series.
- The runner needs one working transport before confirmatory runs. Candidates,
  in rough order of effort: repair the apiserver→kubelet `nodes/proxy`
  authorization, repair VM SSH, or have a node-native service publish results
  through the Kubernetes API.
