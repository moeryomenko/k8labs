# k8labs — Research

This directory hosts experiment *series* for k8labs. Each series is a
self-contained sub-project with its own directory, its own series makefile
(`<series>/<series>.mk`), and namespaced targets (`<series>-*`). Shared
cross-series components live in `research/common/`.

## Series layout

```
research/
├── Makefile            # pure aggregator: includes every <series>/*.mk
├── README.md           # this file
├── common/             # shared cross-series components (placeholder)
├── linux-sched/        # Linux CPU scheduling: EEVDF vs sched_ext
└── kube-sched/         # Kubernetes placement: scoring/topology policy
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

The two series are independent: they own separate commands, data roots,
configuration, and analysis. Linux scheduling is studied first; the Kubernetes
placement series reuses the data-schema conventions but not the Linux runner.

## How to run

```bash
make linux-sched-list-scenarios            # from this directory
make kube-sched-plan
make linux-sched-test
make kube-sched-test
```

Targets are also available from the repo root: `make -C research <target>`.

## How to add a new series

1. Create `research/<series>/`.
2. Create `research/<series>/<series>.mk` with series-prefixed variables and
   `<series>-` targets.
3. The root `Makefile` picks it up automatically via
   `include $(wildcard */*.mk)`; `make help` lists the new targets.
4. Put shared cross-series components in `research/common/`.

## Prerequisites

- A healthy k8labs workload cluster: `kubectl --kubeconfig build/kubeconfig get
  nodes` returns the expected nodes.
- Series-specific tooling is documented in each series README.
