# Hypotheses and commit map

The source analysis covers 64 non-merge commits under the merge tag
`sched-core-2026-10-18` (`cb95904b470d`). This document maps each group to an
experiment, or states why the group is out of scope. The primary metrics and
their expected directions are predeclared in `experiments/scenarios.py`; this
table is the human-readable rationale.

## Commit groups

| group | commits | mechanism | experiment | status |
|---|---|---|---|---|
| Proxy-execution state and migration | 1-6, 29-33 | donor/owner migration, core-scheduling cookie, sched_ext preparation | none directly; `churn` is a no-regression check | not directly measurable with userspace workloads |
| Dynamic-preemption simplification | 7-12 | deferred preemption, direct calls, arch flags | `churn` | no application-level effect expected |
| Static keys, timer, small fixes | 13-19 | steal-time static key, timer deadline comparison, NUMA counter clear, PREEMPT naming | `churn`, `wakeup` | mostly no-op for these workloads |
| FAIR timer restart and CPU selection | 20-24 | restart the timer after re-picking the same task, random tie-break, best candidate on equal wakeup | `wakeup`, `fairness`, `slice` | primary |
| EEVDF slice protection | 25-28 | bound the current entity's protection to the minimum slice, update protection, keep the best equal-slice candidate, `WF_SYNC` contract | `wakeup`, `slice` | primary |
| Preferred CPUs and steal governor | 34-47 | `cpu_preferred_mask`, wakeup/balance/tick preference, the governor driver and its parameters | `steal` | primary, isolated with `custom-nogov` / `custom-gov` |
| Reschedule tracing and SMT idle choice | 48-50 | resched flag before tracing, SMT priority for idle CPU selection, asymmetric packing override | none | 2 vCPU guest exposes no SMT topology |
| CPU hotplug on PREEMPT_RT | 51-53 | pending IRQ work before parking, RT ordering | none | no RT kernel, no CPU hotplug |
| Augmented rbtree | 54-57 | declarative augmented fields, aggregates during descent, shared insert helpers | `churn` | no asymptotic change; constant-overhead check |
| Correctness and final fixes | 58-64 | wait-queue rejection, capability recheck, PSI RCU wait, tickless accounting, LLC stats, restore the CFS-bandwidth enqueue branch, boolean helper | `quota`, `churn` | `quota` is the null control for 21/63 |

The CFS-bandwidth pair is important: commit 21 removed a branch the author
believed unreachable, and commit 63 restored it because it is reachable. The
net series therefore preserves the existing `cpu.max` contract. It does not
fix CPU throttling, and a quota result that moves is a regression or a
confound, not a win.

## Predictions

| id | prediction | metric | falsified when |
|---|---|---|---|
| H1 | The custom kernel lowers the 99.9th wakeup delay under CPU pressure. | `wakeup/probe/p999_us` | The interval excludes a >5 % improvement in the other direction, or the interval is inconclusive after the planned blocks. |
| H2 | A short periodic burst sees a lower 99th start delay without more missed periods. | `slice/burst/p99_us`; missed periods are reported descriptively | The interval shows a >5 % regression, or missed periods increase materially. |
| H3 | Equal-weight periodic threads keep similar completion counts and do not see a worse tail. | `fairness/fair/p99_us`, `fairness/fair/thread_cv` | The interval shows a >5 % regression or a larger dispersion. |
| H4 | The `cpu.max` contract is unchanged: delivered CPU and the throttle ratio stay inside +/-5 %. | `quota/limited/delivered_cpu_pct`, `quota/limited/throttled_period_pct` | Either metric moves past the band with an interval that supports the move. |
| H5 | Scheduler constant overhead falls slightly, without a throughput loss. | `churn/churn/p99_us`, `churn/churn/tasks_per_s` | The start delay or task rate regresses past the band. |
| H6 | The governor contracts the preferred mask under host steal, frees the pinned responder's non-preferred vCPU from the unpinned burners, and reduces independent throughput when those burners consolidate. | `steal/pair/p99_us`, `steal/pair/round_trips_per_s`, `steal/burn/iterations_per_s` | The preferred mask never changes, or no effect is measurable with the host steal active. |
| H0 | The latency work does not cost throughput. | `wakeup/load/iterations_per_s` | The throughput interval falls below 0.95x. |

H6 is a trade-off, not a pure improvement. A regression in
`steal/burn/iterations_per_s` is the expected cost of consolidation. It is
reported as `PASS` when the expected regression is observed, and as `FAIL`
when it is absent.

## Null controls

- `quota/limited/delivered_cpu_pct` and `throttled_period_pct` are the
  equivalence controls for the CFS-bandwidth changes.
- `wakeup/load/iterations_per_s` and `churn/churn/tasks_per_s` are the
  throughput controls for the latency work.
- The stock arm exposes no `/sys/devices/system/cpu/preferred`; the runner
  fails closed if it does. The custom arm must expose it; the runner fails
  closed if it does not.

## Out of scope

The following claims in the source analysis cannot be tested here, and the
series does not make them:

- userspace mutex or Java monitor speedup from proxy execution;
- a general application-lock improvement from core scheduling;
- LLC or NUMA placement gains on this single-NUMA 2 vCPU guest;
- SMT sibling selection inside the guest (the guest does not see host SMT);
- PREEMPT_RT CPU-hotplug completion;
- an asymptotic improvement from the augmented rbtree (it remains O(log n));
- a fix for `cpu.max` throttling;
- a sched_ext proxy-execution gain (the series only prepares it).

## Decision rule

For each primary metric the analyzer forms a treatment/control ratio per block
and reports the geometric mean with a 95 % block-bootstrap interval. The
practical-effect band is +/-5 %. A verdict needs at least three complete
blocks:

- `improved`: the interval is entirely on the better side of 1 and the point
  estimate is past the band;
- `regressed`: the interval is entirely on the worse side of 1 and the point
  estimate is past the band;
- `equivalent`: the whole interval is inside 0.95..1.05;
- `different`: for an equivalence metric, the whole interval is outside
  0.95..1.05 (this is a failure of the null control, not a free pass);
- `inconclusive`: anything else.

The `status` column compares the decision with the predeclared expectation:
`PASS` means the data shows what the mechanism analysis predicted, `FAIL` means
it does not, and `INCONCLUSIVE` means the campaign could not decide. A `FAIL`
for H1-H3 is a substantive finding about the custom kernel, not a harness
error.
