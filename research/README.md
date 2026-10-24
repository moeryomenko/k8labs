# k8labs — Research

This directory hosts experiment *series* for k8labs. Each series is a
self-contained sub-project with its own directory, its own Taskfile
(`<series>/Taskfile.yml`), and namespaced tasks (`<series>-*`). Shared
cross-series components live in `research/common/`.

## Series layout

```
research/
├── Taskfile.yml        # pure aggregator: flattens every <series>/Taskfile.yml
├── README.md           # this file
├── common/             # shared cross-series components (placeholder)
├── linux-sched/        # Linux CPU scheduling: EEVDF vs sched_ext
├── kube-sched/         # Kubernetes placement: scoring/topology policy
└── sched-core-modes/   # cgroup_mode: up, smp, concur, max, tasks
```

## Series

- **linux-sched** (`research/linux-sched/`) — compares the default EEVDF
  scheduler with sched_ext implementations (`scx_bpfland`, `scx_lavd`,
  experimental `scx_mavd`) on a pinned worker VM via a randomized, seeded
  paired-block crossover, with the full BCC/bpftrace/`perf sched` observer
  stack on every sample. See `research/linux-sched/README.md`.
- **kube-sched** (`research/kube-sched/`) — studies kube-scheduler placement
  (queue → filter → score → bind) on the three-worker lab while holding the
  guest Linux scheduler fixed on EEVDF. See `research/kube-sched/README.md`.
- **sched-core-modes** (`research/sched-core-modes/`) — controlled cgroup-v2
  study of the five `cgroup_mode` values with weighted threads, nested QoS,
  quota and wakeup workloads. See `research/sched-core-modes/README.md`.

The series are independent: they own separate commands, data roots,
configuration, and analysis.

## How to run

```bash
task linux-sched-list-scenarios            # from this directory
task kube-sched-plan
task linux-sched-test
task kube-sched-test
task sched-core-modes-test
```

Tasks are also available from the repo root: `task <task-name>`. Run
`task --list` for the full index.

## How to add a new series

1. Create `research/<series>/`.
2. Create `research/<series>/Taskfile.yml` with series-prefixed variables and
   `<series>-` tasks.
3. Add the series to `research/Taskfile.yml` under `includes:` with
   `flatten: true`; the root Taskfile includes `research` the same way, so
   `task <series>-<task>` works from the repo root.
4. Put shared cross-series components in `research/common/`.

## Prerequisites

- A healthy k8labs workload cluster: `kubectl --kubeconfig build/kubeconfig get
  nodes` returns the expected nodes.
- Series-specific tooling is documented in each series README.
