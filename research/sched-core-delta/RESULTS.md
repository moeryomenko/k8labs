# Stock 7.3 versus the custom kernel: what the scheduler series changes

This is the integrated result of the `sched-core-delta` series. It combines a
three-block stock-versus-custom campaign with a three-block steal-governor
contrast on the custom kernel. The raw generated reports are
[RESULTS-kernel.md](RESULTS-kernel.md) and [RESULTS-steal.md](RESULTS-steal.md).

## Bottom line

1. **The custom kernel does not show a greater-than-5 % wakeup, slice or
   fairness improvement that this 2 vCPU, three-block campaign can resolve.**
   The wakeup p99.9 point estimate is 4.5 % lower on the custom kernel, but the
   interval `[0.826x, 1.193x]` includes no effect and the predeclared band is
   +/-5 %. That is `inconclusive`, not an improvement.
2. **The custom kernel preserves CFS bandwidth enforcement.** The delivered-CPU
   and throttle-ratio point estimates are within 0.1 % of 1, and the in-quota
   wakeup-tail interval is `[0.993x, 1.005x]`. The `cpu.max` contract is
   preserved; the custom kernel is not a throttling fix.
3. **The steal governor is a large, causal effect when it is exercised.** On
   the same custom kernel, loading the governor under ~50 % host steal
   contracts the preferred mask from two CPUs to one, cuts the interacting
   pair's p99 round trip from 5.77 ms to 3.66 ms (-37 %), raises pair
   throughput 3.4x, and reduces independent burner throughput to 64 %. The
   trade-off is exactly the one the source analysis describes.
4. **A short-task start-latency regression is a candidate kernel effect, not a
   confirmed one.** The custom arm's churn p99 is 1.43x the stock arm with a
   block-bootstrap interval `[1.218x, 1.685x]`, consistent across all three
   blocks. Both arms ran on different VMs, so the kernel is confounded with VM
   identity and host placement. The stock arm actually saw slightly more steal
   time, so steal does not explain the difference, but the cross-node design
   cannot close the attribution.

## Campaigns

| campaign | contrast | blocks | reps x window | node(s) |
|---|---|---:|---|---|
| kernel | `custom` vs `stock` | 3 | 3 x 8 s | `md-custom` vs `md-0` |
| steal | `custom-gov` vs `custom-nogov` | 3 | 3 x 8 s | `md-custom` only |

Both campaigns use the same image and the same 2 vCPU worker shape. The kernel
pins and scheduler-relevant config are in [README.md](README.md#arms). The
stock config hash is `e7b7b37...`; the custom config hash is `b016c96...`.
`CONFIG_HZ`, `CONFIG_PREEMPT_LAZY`, `CONFIG_SCHED_CORE`,
`CONFIG_CFS_BANDWIDTH`, `CONFIG_CGROUP_SCHED`, `CONFIG_FAIR_GROUP_SCHED` and
`CONFIG_SCHEDSTATS` are identical. The custom kernel adds
`CONFIG_PREFERRED_CPU=y` and `CONFIG_STEAL_GOVERNOR=m`.

The estimator is the geometric mean paired ratio with a 95 % block-bootstrap
interval. `improved`/`regressed` requires the interval to exclude 1 and the
point estimate to pass +/-5 %; `equivalent` requires the whole interval inside
+/-5 %; everything else is `inconclusive`.

## Stock versus custom

| scenario | metric | stock | custom | ratio | 95 % CI | decision |
|---|---|---:|---:|---:|---:|---|
| wakeup | probe p99.9 (us) | 2597 | 2146 | 0.955x | 0.826-1.193 | inconclusive |
| wakeup | probe p99 (us) | 992 | 1192 | 1.038x | 0.725-1.285 | inconclusive |
| wakeup | load throughput (iter/s) | 192869 | 192661 | 1.002x | 0.993-1.006 | equivalent |
| slice | burst p99 (us) | 1573 | 1594 | 0.998x | 0.803-1.195 | inconclusive |
| fairness | periodic p99 (us) | 2737 | 2717 | 1.023x | 0.972-1.125 | inconclusive |
| fairness | thread CV | 0.01 | 0.01 | 0.868x | 0.656-1.325 | inconclusive |
| quota | delivered CPU (%) | 50.05 | 50.10 | 1.000x | 0.998-1.001 | equivalent |
| quota | throttled periods (%) | 100.0 | 100.0 | 1.000x | 1.000-1.000 | equivalent |
| quota | probe p99 (us) | 60092 | 60035 | 0.999x | 0.993-1.005 | equivalent |
| churn | task start p99 (us) | 2121 | 3054 | 1.429x | 1.218-1.685 | regressed |
| churn | tasks/s | 199.99 | 200.08 | 1.000x | 1.000-1.001 | equivalent |

The per-block values are in [RESULTS-kernel.md](RESULTS-kernel.md). The ratio
column is the paired geometric mean, not the ratio of the two displayed
medians; the per-block pairs are in the generated report. The
wakeup p99.9 point estimate is 4.5 % better on the custom kernel; it is just
inside the band and the interval is wide, so the campaign does not claim the
effect. The pilot (1 rep x 3 s) had shown a regression; the full campaign
reversed the point estimate. The pilot is not used as evidence.

### Feature-by-feature

| feature | measured result | verdict |
|---|---|---|
| EEVDF slice protection and timer restart | p99.9 0.955x, p99 1.038x, both intervals include 1 | **not observed** at >5 % |
| EEVDF short-slice burst | p99 0.998x, missed periods 1/1 median | **inconclusive** |
| EEVDF candidate selection / fairness | p99 1.023x, CV 0.868x, intervals wide | **inconclusive** |
| CFS bandwidth branch (commits 21/63) | delivered CPU, throttle ratio, tail all equivalent | **observed as intended null** |
| scheduler constant overhead (rbtree/static keys) | task rate equivalent, start tail 1.429x | **candidate regression** |
| wakeup throughput cost | 1.002x, interval inside +/-1 % | **no throughput cost** |
| steal governor | mask 2 -> 1, pair p99 0.631x, pair rate 3.397x, burner rate 0.644x | **observed, including the trade-off** |

The wakeup and fairness data are consistent with the source analysis's own
caveat: the EEVDF changes are conditional, the effects are small, and a short
request does not automatically become a short-slice entity. A 2 vCPU guest
under a 1 ms timer probe is not a favourable setting for resolving a bound on
slice protection.

The churn regression is the one result that goes in the unexpected direction.
Per block, stock/custom task-start p99 is `1812/3054 us`, `2121/2584 us`,
`2173/3086 us`. The custom arm is worse in every block. The stock arm saw
4.62 % steal in the churn scenario and the custom arm 4.44 %, so host steal
does not explain it. Candidate explanations are a wakeup-placement change in
the EEVDF path, the preferred-CPU checks that run even with a full preferred
mask, or another 7.4 tree change. The cross-node design cannot separate those
from VM identity. This is the strongest reason to run the same-node phase.

## The steal governor

The governor is a custom-kernel feature; the correct contrast is module off
versus module on, on the same node and kernel. The host helper pinned the
worker's two vCPUs to two host CPUs and ran one spinner per CPU, producing
about 50 % steal in the control arm.

| metric | `custom-nogov` | `custom-gov` | ratio | 95 % CI | decision |
|---|---:|---:|---:|---:|---|
| interacting pair p99 (us) | 5774 | 3661 | 0.631x | 0.611-0.642 | improved |
| interacting pair p99.9 (us) | 6555 | 5407 | 0.818x | 0.795-0.841 | descriptive |
| interacting pair round trips/s | 433 | 1472 | 3.397x | 3.321-3.456 | improved |
| independent burner iter/s | 98006 | 63375 | 0.644x | 0.640-0.647 | regressed (expected) |
| steal, % of total CPU time | 50.63 | 22.04 | - | - | mechanism |
| steal, % of busy CPU time | 50.63 | 40.42 | - | - | mechanism |
| preferred CPUs, minimum | 2 | 1 | - | - | mechanism |
| preferred-mask changes per run | 0 | 1 | - | - | mechanism |
| context switches/s | 3704 | 7445 | - | - | descriptive |

The preferred mask contracted to one CPU in every governor repetition. The
first repetition of each block captures `0-1 -> 0`; later repetitions start
with the module still loaded from the previous repetition, so their first
sample is already `0`. Two later repetitions briefly increase the mask back to
`0-1` and contract again, which is the controller's hysteresis responding to a
short drop in measured steal. The sampler starts before the warmup, so the
first transition is captured rather than inferred.

The total steal fraction falls because the guest's non-preferred vCPU is idle
or waiting more often, not because the host became less contended; the
busy-time fraction (50.6 % -> 40.4 %) is the honest comparison.

The interacting pair is a pipe ping-pong hard-pinned one thread per vCPU, plus
a 20 us burn on the responder. With two preferred CPUs and 50 % steal, the
round trip waits for both vCPUs to be scheduled, and each vCPU is shared with
an independent burner. When the governor contracts the preferred mask to one
CPU, the pair threads keep their hard affinity: the responder on the
non-preferred vCPU has no allowed preferred alternative, so it stays where it
is. What moves are the unpinned burners, which are consolidated onto the
preferred CPU. The responder's vCPU is consequently free of burner
competition, and the round-trip tail and throughput improve sharply. This is
the affinity-preserving branch of the contract, not pair consolidation; the
source analysis's `8-11` row is the relevant one. The independent burners lose
parallelism, which is the documented cost. This is not a free capacity
increase: the guest is doing less parallel work, and the winner is a workload
whose responder has a dedicated non-preferred vCPU.

The governor was loaded with the default parameters `interval_ms=1000`,
`high_threshold=500` (5 %), `low_threshold=200` (2 %). The result therefore
applies to the deployed defaults under strong steal, not to every production
steal level. At or below 5 % steal the controller would not contract the mask.

## What this says about the commit groups

| group | evidence |
|---|---|
| Proxy-execution fixes (1-6, 29-33) | Not directly exercised. The workloads use userspace mutexes and pipes, not kernel mutexes. No application-lock speedup is claimed. |
| Dynamic-preemption simplification (7-12) | No measurable application effect; throughput parity and the churn tail are the only signals. |
| Static keys, timer, small fixes (13-19) | No independent signal; the timer deadline comparison is inside the wakeup path but not separable from the rest of the series here. |
| FAIR timer restart and CPU selection (20-24) | Wakeup p99.9 0.955x and fairness p99 1.023x, both inconclusive. No >5 % effect resolved. |
| EEVDF slice protection (25-28) | Slice p99 0.998x; no effect resolved. The 1 ms probe tail is 4.5 % better at the point estimate but inside the band. |
| Preferred CPUs and steal governor (34-47) | Strong, causal on/off effect under induced steal, including the expected independent-throughput regression. |
| Reschedule tracing and SMT selection (48-50) | Out of scope on a 2 vCPU guest with no exposed SMT topology. |
| CPU hotplug on PREEMPT_RT (51-53) | Out of scope; no RT kernel and no hotplug. |
| Augmented rbtree (54-57) | Task rate equivalent; task-start tail regressed on the custom arm, attribution unresolved. |
| Correctness and final fixes (58-64) | Quota null control preserved; no regression observed in the capped group. |

## What is not shown

- **No userspace lock or proxy-execution speedup.** The series' proxy
  execution is a kernel-mutex mechanism. Pthread mutexes and pipe wakeups do
  not enter it.
- **No core-scheduling cookie effect.** The cookie fixes require core
  scheduling and proxy execution to be active together; container boundaries
  alone do not enable either.
- **No LLC or NUMA placement gain.** The guest is single-NUMA with two vCPUs.
- **No CPU-throttling fix.** The quota campaign is an equivalence check and it
  passes as equivalence.
- **No claim that the custom kernel is generally faster.** The only confirmed
  positive effect is the governor under strong host steal. The churn
  task-start tail is a measured negative whose attribution is unresolved.
- **No test of full pair consolidation.** The interacting pair is hard-pinned,
  so the governor cannot migrate it. The result covers the
  affinity-preserving branch; an unpinned pair is a follow-up.

## Confounds and limits

- **Cross-node kernel campaign.** `stock` ran on `md-0` and `custom` on
  `md-custom`. The two VMs are on the same host and use the same image, but
  their vCPUs float independently. Kernel is confounded with VM identity and
  host placement. The steal campaign is same-node and does not have this
  confound.
- **The custom tree is not stock plus one series.** It is
  `v7.3-7363-g734bef548a96`, including the 7.4 merge window. The
  `sched-core-2026-10-18` merge is the scheduler series under study, but other
  subsystems differ.
- **Three blocks.** Three is the predeclared floor. It is enough for a verdict
  under the rule, but the intervals are wide; five or more blocks would
  tighten the EEVDF and churn estimates.
- **Two vCPUs.** The guest cannot express LLC, NUMA or SMT placement, and the
  queue depth is small.
- **Raw cgroups, not pods.** The kernel path is the same (`cpu.weight`,
  `cpu.max`, `cpuset.cpus`), but the kubelet, CRI-O and crun add their own
  weights and scheduling. The sibling `sched-core-modes` series keeps the
  Kubernetes QoS sanity check.
- **Induced steal.** The governor result is conditional on about 50 % steal.
  At the default 5 % high threshold it would not engage in a quiet cluster.

## Next steps

1. **Same-node A/B for the kernel contrast.** Boot the same worker into stock
   and custom in alternating blocks, as described in
   [README.md](README.md#same-node-confirmation). This removes the VM-identity
   confound and is the only way to attribute the churn regression.
2. **Five blocks for the EEVDF and churn metrics.** The p99.9 interval is
   `[0.826, 1.193]`; three blocks cannot resolve a 4-5 % effect. Five blocks
   would either tighten it or confirm that the effect is below the band.
3. **A lower-steal governor run.** Repeat the governor contrast at 5-10 %
   steal to find the engagement boundary and to check whether consolidation
   still helps when the host is only mildly oversubscribed.
4. **A Kubernetes end-to-end probe.** Run the same timer probe as a pod with a
   CPU limit and a CPU-burner neighbour, and compare the pod-level tail. The
   raw cgroup result should transfer, but the runtime adds weights and
   scheduling that the raw test does not.

## Reproduction

```bash
# Kernel campaign
RUN_ID=<id> task sched-core-delta-run
task sched-core-delta-report RUN_DIR=build/research/sched-core-delta/<id> \
    OUT=RESULTS-kernel.md

# Governor campaign (pins the custom worker's vCPUs for the duration)
RUN_ID=<id> task sched-core-delta-steal
task sched-core-delta-report RUN_DIR=build/research/sched-core-delta/<id> \
    OUT=RESULTS-steal.md
```

Data lives under `build/research/sched-core-delta/<run-id>/`; the analyzer
writes `analysis/summary.{md,csv,json}` next to each run.
