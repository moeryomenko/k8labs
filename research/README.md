# k8labs — Research

This directory hosts experiment *series* for k8labs. Each series is a
self-contained sub-project with its own directory, its own series makefile
(`<series>/<series>.mk`), and namespaced targets (`<series>-*`). Shared
cross-series components live in `research/common/` — currently an empty
placeholder; content is promoted there when more than one series needs it.

## Series layout

```
research/
├── Makefile            # pure aggregator: includes every <series>/*.mk
├── README.md           # this file
├── common/             # shared cross-series components (placeholder)
└── cpu-sched/          # CPU scheduling / EEVDF experiments
    ├── cpu-sched.mk    # series makefile (all cpu-sched-* targets)
    └── README.md       # series documentation
```

## Current series

- **cpu-sched** (`research/cpu-sched/`) — reproducible research on CPU
  throttling and the EEVDF scheduler: container process lifecycle tracing,
  cgroup v2 throttling characterization, and CPU request/limit guidance.
  See `research/cpu-sched/README.md` for the full series documentation.

## How to run

All targets are namespaced per series. From this directory:

```bash
cd research
make cpu-sched-experiment-weight-share   # run one experiment
make cpu-sched-test                      # run the series test suites
make cpu-sched-perfetto-view             # view the newest Perfetto trace
```

The root `research/Makefile` is a pure aggregator: it includes every
`<series>/*.mk` automatically, so every series target is available from
`research/` (and equivalently via `make -C research <target>` from the repo
root). `make help` lists every series target.

## How to add a new series

1. Create a directory `research/<series>/`.
2. Create `research/<series>/<series>.mk` with:
   - variables namespaced after the series (e.g. `CPU_SCHED_`-style
     prefixes), so series never collide when included together;
   - targets prefixed `<series>-` (e.g. `pod-sched-experiment-foo`).
3. The root `Makefile` picks the new makefile up automatically via
   `include $(wildcard */*.mk)`; `make help` lists the new targets.
4. Place shared cross-series components in `research/common/` once more than
   one series needs them.

## Prerequisites

- A healthy k8labs cluster: `kubectl --kubeconfig kubeconfig get nodes`
  returns 3 Ready nodes, plus kubectl and tofu on the host.
- Series-specific tooling is documented in each series README
  (e.g. `research/cpu-sched/README.md`).
- The CPU parameter selection framework is documented in
  `research/cpu-sched/FRAMEWORK.md`.
