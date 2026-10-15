# linux-sched — Linux CPU-scheduler research series

Compares the default **EEVDF** scheduler with sched_ext implementations
(`scx_bpfland`, `scx_lavd`, experimental `scx_mavd`) on a pinned worker VM, using
a randomized, seeded **paired-block** crossover and the frozen decision rule
from the approved plan.

This series owns *in-guest* CPU scheduling only. Kubernetes pod placement is
studied separately in the sibling `kube-sched` series.

## Protocol (frozen)

- One **paired block** runs one workload scenario once per scheduler arm on the
  same `w1`, host, guest image, and kernel. A block is not an individual
  request or trace sample.
- At least five complete blocks; a pilot estimates log-scale variance and the
  confirmatory block count is frozen before confirmatory runs.
- Arms run in a pre-generated, balanced, seeded order that rotates across
  blocks. The seed and order are recorded per block.
- The primary estimator is the geometric mean paired ratio
  (back-transformed mean log ratio) with a 95% paired-t interval plus a paired
  block-bootstrap interval. Decisions: `improvement`, `regression`,
  `equivalent` (whole interval inside ±5%), else `inconclusive`.
- Every comparison sample runs the full observer stack (BCC `runqlat`/`runqlen`,
  the `sched_*` bpftrace probe, `perf sched`). Results are reported as
  **instrumented**; a separate EEVDF calibration quantifies observer overhead.
- Raw data lives outside the Git worktree under a host-owned data root.

## Commands

```bash
make linux-sched-setup                                   # prerequisites
make linux-sched-validate                                # validate scenario configs (no cluster)
make linux-sched-list-scenarios                          # list scenarios
make linux-sched-pilot        SCENARIO=http              # pilot blocks
make linux-sched-experiment   SCENARIO=http              # confirmatory blocks
make linux-sched-overhead     SCENARIO=http              # observer-overhead calibration
make linux-sched-analyze                                 # decision-rule summary
make linux-sched-observer-check                          # node-native observer tooling gate
make linux-sched-test                                    # pytest + bats
```

The seeded schedule can be inspected without a cluster:

```bash
research/linux-sched/experiments/run-block.sh plan \
    --scenario http --mode pilot \
    --configs-dir research/linux-sched/experiments/scenarios
```

## Layout

```
linux-sched/
├── linux-sched.mk                 # series makefile (linux-sched-* targets)
├── experiments/
│   ├── run-block.sh               # paired-block driver
│   ├── lib.sh                     # shared helpers (cluster/transport/protocol)
│   ├── metrics.py                 # primary-outcome parsers
│   ├── observer_check.py          # node-native tooling gate
│   ├── scenarios/*.conf           # workload scenarios
│   └── tests/                     # parser + gate tests
├── capture/
│   ├── capture-block.sh           # one measured sample per arm
│   └── probes/sched-trace.bt      # sched_wakeup/switch/migrate trace
├── workloads/*.yaml               # pinned workload manifests
├── GATES.md                       # recorded Phase 2 environment findings
└── analysis/                      # paired analysis core + tests
```

## Data layout (host-owned root)

```
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/linux-sched/
├── blocks.csv                     # block_id,scheduler,workload,metric,value
├── <scenario>/block-NN/
│   ├── metadata.json
│   ├── <arm>.txt                  # raw workload/load output
│   ├── <arm>.metric               # extracted primary value
│   └── runqlat.txt, sched-trace.txt, perf.data, perf.log, <arm>.capture-status
└── analysis/summary.{csv,json}
```

## Pre-implementation gates

The execution path refuses to run until these are satisfied (it fails loudly
rather than reporting a fake success):

1. A kernel build with the required BTF and sched_ext interfaces, used for
   **all** arms (including EEVDF). `scx_mavd` needs the CID-form sched_ext API
   and clang 22+.
2. `scx_bpfland`/`scx_lavd`/`scx_mavd` binaries staged on the worker, with
   verifier/attach/progress/detach checks passing.
3. `bpftrace`, `perf`, and the BCC tools installed on the worker image. These
   are node-native: the image bake installs them (see `GATES.md` for the exact
   packages and paths), and the series ships no privileged observer Pod.
4. Pinned workload images plus the licensed FFmpeg fixture.
5. A working node transport for the runner. SSH to the worker VMs and the
   cluster's `kubectl logs`/`exec` path are both currently broken in this lab
   (see `GATES.md`); the runner refuses to run until one is repaired.
