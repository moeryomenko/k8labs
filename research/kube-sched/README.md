# kube-sched — Kubernetes placement research series

Studies **kube-scheduler placement** on the lab cluster: the queue → filter →
score → bind path and how scoring/topology policy changes placement latency and
placement correctness. The guest Linux scheduler is held fixed on default
EEVDF, so this series measures *placement*, not CPU scheduling.

Independent of `linux-sched`: separate commands, data, profiles, and analysis.

## Scope

- Kubernetes pinned to the k8labs lock (`v1.37.0`).
- Small-cluster placement behavior on the one-control-plane/three-worker lab —
  **not** large-cluster scheduler throughput. `percentageOfNodesToScore` is
  deliberately not varied; local scheduler performance docs say it has little
  effect below 100 feasible nodes.
- Sources of truth: `k8s-site/content/en/docs/reference/scheduling/config.md`,
  `.../reference/config-api/kube-scheduler-config.v1.md`, and
  `kubernetes/pkg/scheduler/`.

## Placement matrix (one factor at a time)

| Profile | Change |
|---|---|
| `least-allocated` | control: `NodeResourcesFit` default `LeastAllocated` scoring |
| `most-allocated` | `NodeResourcesFit` scoring `MostAllocated` |
| `topology-spread` | `PodTopologySpread` with `DoNotSchedule`, `maxSkew: 1`, zone domain |

The capacity-boundary case uses three homogeneous 2-vCPU/4-GiB workers, an empty
cluster, and identical Pods requesting 500m CPU / 256MiB. `N_fit` is the sum of
per-node slots; `N_fit + 3` Pods are submitted so exactly three exceed aggregate
request capacity.

## Commands

```bash
task kube-sched-setup                                    # prerequisites
task kube-sched-validate                                 # validate profiles (no cluster)
task kube-sched-plan                                     # seeded block schedule (no cluster)
task kube-sched-experiment MATRIX=most-allocated         # run placement blocks
task kube-sched-analyze                                  # decision-rule summary
task kube-sched-test                                     # pytest + bats
```

## Outcomes

Primary (frozen): `submit_to_scheduled_p99_ms` (lower better) and
`correct_placement_rate` (higher better). Secondary: placement distribution,
filter/unschedulable reasons, requested CPU fragmentation, scheduler metrics.

`submit_to_scheduled` is measured from Pod `creationTimestamp` to the
`PodScheduled` condition transition. Pods within a burst are **not** independent
replicates: the paired unit is one fresh burst per profile per block.

Define `correct_placement`: a Pod that fits is scheduled, every Pod that fits
binds to a constraint-satisfying node, and the deliberately-unschedulable Pods
stay Pending.

## Data layout

```
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/kube-sched/
├── pods.csv                       # block_id,profile,pod,latency_ms,unschedulable,correct
└── analysis/summary.{csv,json}
```

## Gate

`kube-sched-experiment` switches the control-plane kube-scheduler profile by
writing `KUBE_SCHED_SCHEDULER_CONFIG` (default `/etc/kubernetes/kube-scheduler.yaml`)
on the control-plane node and restarting `kube-scheduler`. Verify that path on
the lab image before running; a wrong path fails loudly rather than producing a
silent no-op.
