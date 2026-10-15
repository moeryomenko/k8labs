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


## BCC — RESOLVED via the CO-RE tools

BCC 0.35 cannot compile against the 7.2 headers:

```
include/linux/fs.h:2440:15: error: static assertion failed due to requirement
'sizeof(struct filename) % 64 == 0'
```

No newer BCC is published for Fedora 44 (only `0.35.0-5.fc44`). The fix is not a
newer BCC but the **CO-RE** variants shipped in `libbpf-tools`
(`0.35.0-5.fc44`), which use libbpf and the kernel's BTF instead of compiling
against kernel headers:

| Tool | Provides |
|---|---|
| `bpf-runqlat` | `/usr/bin/bpf-runqlat` |
| `bpf-runqlen` | `/usr/bin/bpf-runqlen` |

Verified in an observer on kernel `7.2.10`: `bpf-runqlat 1 3` produced a
run-queue latency histogram.

`libbpf-tools` is installed by the node image and by the observer, and the
capture path prefers `bpf-runqlat` over the BCC `runqlat`. `kernel-devel` is
still installed so the legacy BCC tools remain usable where they work.

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

Workaround (committed, re-runnable): `make provider-loopback-forward`

`scripts/provider_loopback_forward.py` reads the control-plane
HypervisorMachine's `status.publishedPorts` and installs a `loopback-forward`
socat sidecar in the provider Deployment that forwards the published apiserver
and etcd ports from the Pod loopback to `169.254.1.2`. It is idempotent, so it
can be re-run after any management-plane rebootstrap. The ports are read at run
time because they are allocated per cluster.

**Durable fix:** release a provider image from `cluster-api-hypervisor` HEAD
(contains `21ac0d1`), update `versions.lock.yaml` (source commit + image
digest), `make prepare`, redeploy, then delete the workaround.

## HostAgent image loss (environment gotcha)

While debugging, the HostAgent entered a restart loop (`restart counter is at
482`) with `ghcr.io/moeryomenko/cluster-api-hypervisor@sha256:e0fc1a9a…: image
not known` — the agent's container image was gone from local Podman storage. The
provider manager then crashlooped with `dial tcp 10.89.0.2:9444: connect: no
route to host`, because it checks agent health at startup.

`make host-agent-install` reloads the image and restores the agent; the manager
recovers. Worth knowing because the symptom (provider crashloop) looks like the
endpoint bug but is a different failure.

## HostAgent restart loop — two causes

The agent flapped with `restart counter` in the hundreds, which also made the
provider manager crashloop (`dial tcp 10.89.0.2:9444`). There were two causes,
and fixing only one was not enough.

1. **Buffered artifact digests (OOM).** `internal/agent/executor/executor.go`
   hashed artifacts with `os.ReadFile` + `sha256.Sum256`, so hashing the 4.1 GiB
   base image spiked the agent's resident set by that much. On a host with
   2.7 GiB free the OOM killer took it out. Fixed at the source
   (`cluster-api-hypervisor` `9633fed`): digests are streamed; the digest is
   unchanged. **Needs a rebuilt agent image to take effect** — the running agent
   still has the defect.
2. **Missing host path.** The quadlet binds `/tmp/ch-capi`
   (`scripts/host_agent.py` creates it at install), but `/tmp` had been cleaned,
   so the container failed with `statfs /tmp/ch-capi: no such file or
   directory`. Recreating the directory restores the agent.

Contributing factor: the host was out of memory (30 GiB RAM, 2.7 GiB free,
swap 100% full). Two stray `/usr/bin/clangd` processes held ~12.8 GiB of swap;
stopping them freed ~5 GiB of RAM and ~10 GiB of swap and let the agent finish
startup.

Open gap: nothing recreates `/tmp/ch-capi` after a `/tmp` cleanup, so this
recurs. A user `tmpfiles.d` entry, or moving the socket root out of `/tmp`,
would close it.

## sched_ext arms — RESOLVED

Attaching now works. Three separate causes had to be fixed together; each one
alone still produced `EINVAL`.

1. **Kernel ABI gap.** The compiled object's `sched_ext_ops` must match the
   running kernel's:

   | Build | `struct sched_ext_ops` |
   |---|---|
   | Fedora `7.2.10-200.fc44` | 472, vlen=45 |
   | `scx` v1.1.3 (dump `v7.2-rc6`) | 512, vlen=51 |
   | `scx` main (dump `v7.3-rc5`) | 528, vlen=53 |

   The scx dumps come from the sched_ext development tree, which is ahead of
   mainline, so Fedora 44's kernel was behind every recent scx release. Fix:
   boot a **7.3 kernel** and **rebuild scx against that kernel's BTF** — then the
   match is by construction (verified: object 512/51 == kernel 512/51).
   The node image now installs `7.3.0-0.rc7…fc46` from a pinned rawhide repo.
2. **Missing `/boot` in the observer.** libbpf resolves `kcfg` externs from the
   kernel config; without it the load failed with `EINVAL` and the only error
   line was `failed to open system Kconfig`. Fix: mount the host `/boot`
   read-only.
3. **Stale stats socket.** A socket at `/var/run/scx/root/stats` blocks the next
   attach (`Error: deleting …stats: Permission denied`). Root cannot unlink the
   socket, but renaming its directory works: `mv /var/run/scx /var/run/scx.stale`.

Verified in an observer on `7.3.0-0.rc7…fc46`: `scx_bpfland` attaches, holds for
30 s, and detaches cleanly (`EXIT: unregistered from user space`,
`Unregister scx_bpfland scheduler`). `scx_lavd` verifies the same way through the
runner.

Two further things were needed for the runner to drive the arms, both found by
running it: a process started through `kubectl exec` dies with that exec, so the
scheduler is supervised by a long-lived local `kubectl exec`; and the binary is
exec'd through `sh -c` because the runtime refuses a hostPath binary directly.

### Rebuilding the schedulers against the pinned kernel

The binaries must be built against the running kernel's BTF, or the struct_ops
match fails. Do this in a **worktree**, so the user's scx checkout stays clean
(`<ver>` is the kernel version, `<sha12>` the first 12 hex of the kernel commit;
on `7.3.0-0.rc7…fc46` that is `7.3.0-rc7` and `4056b3c9d3c9`):

```sh
git worktree add /tmp/scx-kern <rev> && cd /tmp/scx-kern

# 1. Dump the running kernel's BTF. bpftool and the BTF mount live in the
#    observer, so run this there and copy the result out.
bpftool btf dump file /sys/kernel/btf/vmlinux format c > /tmp/vmlinux-<ver>.h

# 2. scx needs struct cpumask's bits array at a fixed width.
python3 scripts/fixup_vmlinux_h.py /tmp/vmlinux-<ver>.h

# 3. Install it beside the vendored dumps and repoint the symlink.
install -m 0644 /tmp/vmlinux-<ver>.h "scheds/vmlinux/arch/x86/vmlinux-v<ver>-g<sha12>.h"
ln -sfT "vmlinux-v<ver>-g<sha12>.h" scheds/vmlinux/arch/x86/vmlinux.h

# 4. Rebuild the tarball the build actually consumes, then force a FULL
#    rebuild: the compiler BPF object is cached, and `cargo clean -p` does not
#    remove it, so a partial clean silently keeps the old dump's object.
tar --zstd -cf rust/scx_utils/vmlinux.tar.zst -C scheds vmlinux
cargo clean
cargo build --release -p scx_bpfland -p scx_lavd
```

The result is a container-side `sched_ext_ops` of 512/51 against the kernel's
512/51. Stage the binaries with that worktree as the source:

```sh
LINUX_SCHED_SCX_ROOT=/tmp/scx-kern make -C research linux-sched-scx-stage
```

The tarball mirrors `scheds/vmlinux/` (25 members, every arch's dump), which is
why it is rebuilt from that directory rather than assembled by hand.

## `scx_mavd` — blocked: needs a 7.4 kernel

`scx_mavd` is the only arm on the **cid** path, and on this kernel it fails to
load with `EINVAL` and no verifier log:

```
libbpf: prog 'scx_lib_init_probe': BPF program load failed: -EINVAL
Error: Failed to load BPF program
Caused by: Invalid argument (os error 22)
```

This is not the kernel ABI and not the kcfg problem. `scx_mavd/build.rs` passes
`-DSCX_ARENA_SCALAR`, so the object carries the `__SCX_MARKER_arena_scalar`
rodata marker; the cid open path reads that marker and sets `BPF_F_ARENA_SCALAR`
(`1 << 8`) on **every** program. The running kernel predates that flag:

- `kernel/bpf/syscall.c:2948` returns `-EINVAL` for any `BPF_PROG_LOAD`
  `prog_flags` bit outside its known set, before the verifier runs — which is why
  there is no verifier log to read.
- In this tree the known set tops out at bit 7, with
  `BPF_F_TOKEN_FD = (1U << 16)`. Bit 8 is not in it.
- The flag itself (`4c651a91bdfc` in bpf-next) is not in `include/uapi/linux/bpf.h`
  and is due in v7.4.

It is the flag and not the probe's attach target: bpftrace attaches
`fentry:bpf_scx_reg_cid` on this kernel without error, and both `bpf_scx_reg` and
`bpf_scx_reg_cid` are present as functions in kallsyms and in the kernel BTF.

The marker cannot simply be dropped, because `scx_mavd`'s sources now compile
only under scalar-arena semantics — removing `-DSCX_ARENA_SCALAR` fails to build:

```
src/bpf/lavd.bpf.h:366:9: error: returning '__arena_global struct cpdom_ctx *'
    from a function with result type 'struct cpdom_ctx *' changes address space of pointer
```

Loading the scalar-built object *without* the flag is not an option either: the
JIT adds the arena base only under the flag, so the scheduler would dereference
ordinary addresses and corrupt memory. The change that introduced this for
`scx_mavd` is `6b9fe0d69` ("scx_mavd: Drop the __arena tags"), 2026-10-07.

So `scx_mavd` runs in this lab only under a kernel that has the flag (v7.4), or
as the pre-`6b9fe0d69` revision built in the classic arena form.

Decision: `scx_mavd` is **deferred** from the series. The series runs three arms
(EEVDF, `scx_bpfland`, `scx_lavd`) so that every arm shares one scx revision and
one kernel ABI; `scx_mavd` rejoins when a kernel with the flag is available. The
runner refuses the arm with a pointer here until then, so it cannot be started by
accident.

Note the BCC situation is unrelated: `libbpf-tools` CO-RE binaries cover the
run-queue metrics, and `kernel-devel` is deliberately not installed when the
kernel comes from a different release's repository (it dragged that release's
dependency set into the image).

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

**Protocol deviation — no per-arm reboot.** The frozen protocol reboots the
worker between scheduler arms to clear state. Rebooting needs SSH (or an
equivalent host hook) that this lab does not have, so each arm instead starts
from a killed-observer, quiescent guest: leftover `scx_` processes are killed
and the guest must sit below the idle threshold for the hold period before the
arm begins. Weaker than a reboot; recorded here rather than silently changed.

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

| Tool | Package | Path |
|---|---|---|
| `pgbench` | `postgresql-contrib` (**not** `postgresql`) | `/usr/sbin/pgbench` |
| `wrk2` | built at bake time from `gcc`, `make`, `openssl-devel`, `zlib-devel` | `/var/lib/linux-sched/bin/wrk2` |

## Workload fixtures

The workload inputs are baked onto the node image rather than shipped in a
container image: the lab has no PVC and no SSH to the VMs, and the workload Pods
and the observer mount the same host paths, so every arm reads byte-identical
inputs.

| On the node | Mounted at | Used by |
|---|---|---|
| `/var/lib/linux-sched/fixtures/input.mp4` | `/fixtures/input.mp4` | `ffmpeg`, `mixed` |
| `/var/lib/linux-sched/fixtures/pgbench.sql` | `/docker-entrypoint-initdb.d/01-pgbench.sql` | `postgres` |
| `/var/lib/linux-sched/fixtures/transactions.sql` | `/fixtures/transactions.sql` | the `pgbench` load |
| `/var/lib/linux-sched/bin/wrk2` | `/usr/local/bin/wrk2` | the `wrk2` load |

- **Clip.** Big Buck Bunny 320x180 (CC-BY 3.0, Blender Foundation), verified by
  SHA-256 as the archive and again as the extracted file, so a truncated or
  substituted download cannot reach a run.
- **Snapshot.** pgbench at scale 10, taken with the node's PostgreSQL 18.6 and
  dumped plain so the pinned image's entrypoint replays it. The Pod keeps no
  state, so every arm re-initialises from the same data — that is what makes the
  arms comparable. The image is pinned to the same major version, because a dump
  cannot be restored into an older server: the original `postgres:17.2-alpine`
  could not have read this dump at all.
- **wrk2.** Built from commit `44a94c1`, because Fedora packages `wrk`, which
  cannot hold a fixed offered rate (`-R`) and so cannot measure latency under a
  fixed load.
- **pgbench.** Installed into the observer with dnf instead of mounted: the load
  runs inside the observer container, and a client from the container's own
  repositories matches its libpq.

Both fragile steps were verified in a throwaway Fedora 44 pod using exactly the
packages the bake installs, rather than assumed:

```
PG_SERVER=18.6      PGBENCH=/usr/sbin/pgbench      WRK2_BYTES=627048
wrk 4.0.0 [epoll]
DUMP_BYTES=95993179 DUMP_TABLES=4                  PROBE_OK
```

Three things that check turned up, each of which breaks the bake:

- `zlib-devel` is required: `wrk2`'s `hdr_histogram.c` includes `zlib.h`, and the
  build stops there rather than degrading.
- `pgbench` lives in **`postgresql-contrib`** on Fedora, not in `postgresql`.
  With only `postgresql`, the snapshot step fails *and still writes a
  valid-looking dump* of the empty database, so the mistake survives a careless
  check. The probe asserts the dump's table count for that reason.
- `wrk2` links `libssl.so.3`/`libcrypto.so.3`, so the observer image needs
  `openssl-libs`; mounting the binary alone is not enough.

The snapshot is ~96 MB of plain SQL (pgbench scale 10: 1M accounts plus the
branches, tellers and history tables) and the clip is ~65 MB, so the node image
grows by roughly 160 MB. `pgbench_scale` in the packer variables changes that.

The observer's fixture mounts are `type: Directory`, not `DirectoryOrCreate`: an
observer without fixtures can only produce empty samples, so it should refuse to
start rather than run.

## Observer processes died with their exec

The capture started each observer by backgrounding it inside the Pod through
`kubectl exec` and returning immediately. A process started that way is killed
when the exec session ends — the same behaviour that killed the sched_ext
schedulers — so all three observers were dead within a second and the capture
either failed its own liveness check or copied empty artifacts. That is why
`runqlat.txt` was always empty.

Each observer is now supervised by a long-lived local `kubectl exec` that stays
open for as long as the observer should run, and its remote pid is recorded for
a belt-and-braces kill on cleanup. Verified against a live worker: a capture
that previously produced nothing now yields an 8 KB runqlat histogram and a
4.8 MB `perf.data`, with no observer processes left behind on the node.

## Workload lifecycle in a capture

Two lifecycle steps were missing, and both produced misleading samples rather
than visible failures:

- The load was started against a Pod that was still initialising, so a sample
  could measure nginx coming up or PostgreSQL replaying its snapshot instead of
  the workload. The capture now waits for the arm's workload Pod to report Ready
  before the load begins.
- A podlog-sourced metric (`ffmpeg`, `stress-ng`) was read from the workload log
  right after the load, while the job was still running, so the log held only a
  partial run. `--await-workload-completion` makes the capture wait for the Pods
  to stop first. `run` passes it for podlog scenarios only: a load-sourced
  scenario's nginx keeps serving after its load ends, and waiting there would
  stall for nothing.

`make linux-sched-overhead` also passed `--instrumentation instruments-enabled`,
which the capture compares against `enabled`, so **both** variants ran
uninstrumented and the overhead comparison measured nothing. The variants are
`enabled`/`disabled` now.

## Consequence for the design

- Observer tooling is provisioned into the node image (node-native) and can also
  run from a capability-scoped Pod.
- `logs`/`exec` work, so the Kubernetes API is the result transport; SSH to the
  VMs is still unusable and is not used.
- Before the first block: re-bake the image so the fixtures exist (the observer
  will not start without them), then `make linux-sched-calibrate SCENARIO=http`
  to freeze the offered rate the measured blocks then share.

