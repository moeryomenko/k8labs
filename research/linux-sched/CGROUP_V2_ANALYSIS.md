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

## 6. Reproduction

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
