# Schedulers and cgroup v2: what the kernel actually delegates

This note answers one question that decides whether the six-by-two quota design
measures what it claims: **when a workload runs under `cpu.max`, which schedulers
enforce that limit?**

## 1. The enforcement model

The fair-class scheduler owns CPU-controller enforcement. sched_ext does not
inherit it; the core forwards controller settings to the BPF scheduler and the
scheduler may ignore them. From `Documentation/scheduler/sched-ext.rst` (pinned
source):

> The fair-class scheduler enforces CPU controller settings such as `cpu.max`,
> `cpu.weight` and `cpu.idle`. For sched_ext tasks, the scheduler core
> communicates these settings to the BPF scheduler through `ops.cgroup_init()`
> and reports subsequent changes through the corresponding `ops.cgroup_set_*()`
> callbacks. … Each BPF scheduler is responsible for implementing the scheduling
> semantics of these settings and **may choose to ignore them**.

The kernel side is `scx_group_set_bandwidth()` in `kernel/sched/ext/ext.c`, which
calls `SCX_CALL_OP(sch, cgroup_set_bandwidth, …)` only for schedulers that
declare the op. With no op, nothing throttles the cgroup's sched_ext tasks.

The pinned kernel enables the machinery (`CONFIG_EXT_GROUP_SCHED=y`,
`CONFIG_EXT_SUB_SCHED=y`, `CONFIG_CFS_BANDWIDTH=y`). The running lab kernel
(`7.3.0-0.rc7…fc46`) does **not**: `bpftool btf dump` finds no
`cgroup_set_bandwidth`, so on the current cluster every sched_ext arm ignores
`cpu.max`; only EEVDF enforces it.

## 2. Per-arm capability at scx `36dfc94194d719d1c4f5d01938b33ea9bfd92a97`

| Arm | Path at the pin | `cgroup_init` | `cgroup_set_bandwidth` | Opt-in | Other cgroup use |
|---|---|---|---|---|---|
| EEVDF | kernel fair class | — | — | — | full `cpu.max` enforcement |
| `scx_bpfland` | `scheds/rust` | no | no | — | none |
| `scx_lavd` | `scheds/rust` | yes | **yes** | `--enable-cpu-bw` | — |
| `scx_mavd` | `scheds/experimental` | yes | **yes** | `--enable-cpu-bw` | throttle checks in enqueue/dispatch, `scx_cgroup_bw_reenqueue()` |
| `scx_mlfq` | `scheds/experimental` | no | no | — | none |
| `scx_nitosis` | `scheds/experimental` | yes | no | — | cells per cgroup child via `sub_cgroup_id` / `--cell-parent-cgroup` |

`scx_mavd` and `scx_lavd` are derived from the same design; both check
`CgroupBwSupport::Unsupported` at startup and refuse `--enable-cpu-bw` on a
kernel without the callback. `scx_nitosis` uses cgroups for **partitioning**
(one cell per child of `/kubepods.slice`), not for bandwidth.

## 3. Consequence for the frozen six-by-two design

The confirmatory family is 25 performance-interaction contrasts (5 non-control
schedulers × 5 workloads) plus 50 throttling contrasts. The quota factor exists
for only three arms:

- EEVDF — always enforces (control).
- `scx_lavd`, `scx_mavd` — enforce only with `--enable-cpu-bw` on a kernel that
  exposes `ops.cgroup_set_bandwidth`.
- `scx_bpfland`, `scx_mlfq`, `scx_nitosis` — cannot enforce it at all.

So 15 of the 25 interaction contrasts and 30 of the 50 throttling contrasts are
null **by construction**, not by measurement. Under a single global Holm family
that also inflates the correction applied to the contrasts that can show an
effect.

## 4. Execution path (protocol v3)

The runner now produces the data the analyser needs:

1. `append_block_row` writes `block_id,scheduler,workload,metric,value,direction,policy`.
2. `run_block SCENARIO ARM BLOCK DIR POLICY` sets `LINUX_SCHED_CPU_POLICY` and,
   for `limited`, reads the workload's quota from the frozen manifest through
   `frozen_quota_for`; a missing manifest or workload is fatal.
3. `capture/capture-block.sh` renders the manifest through
   `experiments/cpu_limit_policy.py`: `unlimited` removes `limits.cpu`, `limited`
   applies the frozen quantity. One rendered manifest is kept per policy.
4. Confirmatory v3 runs every arm under both policies, arm outer and policy
   inner, with the same `block_id` so the analyser can pair them.
5. The quota campaign writes to its own tree and protocol version (`v3`).

Known property: a block interrupted between its two policy runs leaves a partial
pair. Resume numbering continues after the highest block id, so the partial pair
is not retried; its contrasts are reported as incomplete rather than silently
treated as measured.

## 5. Decision: split the family (adopted)

Protocol `v3` separates three pre-registered families instead of one 75-contrast
global family:

| Family | Contrasts | Arms |
|---|---|---|
| `scheduler_unlimited` | 25 | all five non-control schedulers × 5 workloads, no quota |
| `quota_interaction` | 10 | `scx_lavd`, `scx_mavd` × 5 workloads |
| `throttling` | 20 | `scx_lavd`, `scx_mavd` × 5 workloads × 2 throttle outcomes |

Holm step-down runs inside each family against its **planned** size, so a missing
contrast cannot make its siblings easier to reject. EEVDF remains the control in
all three. The campaign writes to its own data tree,
`build/research/linux-sched-cgroup-v2-quota`, so v3 pilot and confirmatory blocks
never mix with the scheduler-only series.

The rejected alternatives were: keeping 6×2 with pre-registered nulls (45 of 75
contrasts could never reject and Holm was diluted), and implementing bandwidth in
the other three schedulers upstream (out of scope for this series).

## 6. Quota calibration (EEVDF pilot)

`make linux-sched-quota-calibrate SCENARIO=<name>` starts at 1 CPU on EEVDF and
tightens by the frozen step (0.9) until the measured window reaches the 10%
throttling target. The ratio comes from differencing the container's cgroup v2
`cpu.stat` across the window (`nr_throttled / nr_periods`), not from a whole-pod
snapshot that would include the warmup.

Two facts shaped the implementation:

* Kubernetes/CRI-O applies `limits.cpu` on the **pod sandbox scope**
  (`crio-*.scope` under the pod slice), not on the container cgroup, and removes
  it when the container exits. The observer therefore mounts the host
  `/sys/fs/cgroup` read-only and reads the scope by pod UID; podlog-sourced
  workloads are sampled once a second while they run, because their cgroup is
  gone once they finish.
* Workload manifests request 100m instead of 1 CPU. A request above the quota is
  illegal (`requests <= limits`), and keeping it fixed at a low value lets the
  sweep test quotas below one CPU while holding the cgroup weight constant
  across both policies.

Measured on the live 7.3 cluster:

| Workload | Limit | Throttled periods | Result |
|---|---|---|---|
| `ffmpeg` | 1 | 73.2% | frozen |
| `postgres` | 1 | 11.6% | frozen |

Still blocked:

* `http` and `mixed` need an offered rate, and the rate calibration cannot hold
  the 10 ms p99 target: the mean is ~5 ms but roughly 1% of requests land near
  200 ms at both 100 and 500 rps. That tail is consistent with TCP
  retransmission, not quota, and needs its own investigation before those quotas
  can be frozen.
* `stress-ng` is a saturation control: any limit below demand throttles near
  100%, so the 90% healthy cap rejects it. Decide whether the control is exempt
  from the cap or the cap is relaxed for it.

### Request, weight and the latency tail

Sub-CPU quotas require `requests <= limits`, and the request also sets the cgroup
CPU weight. The chosen policy is a fixed **500m request** for every workload,
with quotas calibrated down to a 500m floor. That is high enough to carry a real
share of CPU on a shared node and low enough to test the quotas that matter.

Measured before the change, at 100 rps with a 1 CPU limit:

| nginx request | Retransmits / segments | Max latency |
|---|---|---|
| 100m (weight 102) | 99 / 6379 (~1.5%) | 954 ms |
| 1 CPU (weight 1024) | 0 / 6249 | 31.9 ms |

At a 500m request a single 500 rps probe reported p99 13.26 ms, but repeated
probes are not stable: 400 rps reported 1660 ms and 300 rps 797 ms for the same
manifest and limit. A controlled study at 400 rps, 1 CPU limit and no quota
isolated the cause:

| Run | p50 | p90 | p99 | retransmits load | retransmits server | CPU throttled (load/server) |
|---|---|---|---|---|---|---|
| 1 | 1.1 | 670 | 2700 | 0 | 118 | 0 / 0 |
| 2 | 1.0 | 2.2 | 279 | 42 | 296 | 0 / 0 |
| 3 | 1.1 | 1.8 | 12 | 0 | 90 | 0 / 0 |

Neither pod hit its CPU quota, so this is not quota or CPU starvation. The tail
tracks **server-side retransmissions**: nginx retransmits 90-296 segments while
the client retransmits few or none. Cilium reports no TCP drops (only
"Unsupported L3 protocol"), and raising the load generator from 100m/1 CPU to
500m/2 CPU did not reduce the retransmissions. The loss is therefore below
Cilium — the virtual NIC, bridge or qdisc on the hypervisor host.

That host is not the workstation running these commands: it exposes no bridge or
tap interfaces and the cluster is remote, so the next counters (`ip -s link`,
`tc -s qdisc`, `ethtool -S`, `softnet_stat`) need access to the hypervisor host
itself.

Moving the load generator to `hostNetwork` did not help: with the observer on the
node network and two CPUs available, three 400 rps probes still produced p99 of
240, 768 and 1490 ms, with retransmits on both sides (load 123/38/279, server
274/390/535) and no CPU throttling. The loss is therefore not in the CNI pod path
either; it is on the virtual network below both the pod and host namespaces.

Until that is fixed, `http` and `mixed` stay uncalibrated rather than frozen on a
meaningless rate.

Frozen quotas under the 500m request:

| Workload | Limit | Throttled periods | Note |
|---|---|---|---|
| `ffmpeg` | 1 | 81.9% | frozen |
| `postgres` | 900m | 25.2% | one tightening step |
| `stress-ng` | 1 | 93.5% | saturation control, exempt from the 90% band |

`stress-ng` is exempt from the healthy band because it makes progress under any
quota; the exemption is recorded in `freeze_quota.py` and the sweep.

## 7. Reproduction

```sh
# capability per arm at the pinned commit
for s in bpfland lavd mavd mlfq nitosis; do
  git -C scx grep -c "cgroup_set_bandwidth" 36dfc941 -- "scheds/**/scx_$s" 
done

# kernel support on a node
kubectl -n linux-sched exec <observer-pod> -- \
  bpftool btf dump file /sys/kernel/btf/vmlinux format c | grep cgroup_set_bandwidth

# which schedulers the runner starts with bandwidth enabled
grep -n "enable-cpu-bw" research/linux-sched/experiments/run-block.sh
```
