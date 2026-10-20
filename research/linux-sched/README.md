# linux-sched — Linux CPU-scheduler research series

Compares the default **EEVDF** scheduler with sched_ext implementations
(`scx_bpfland`, `scx_lavd`) on a pinned worker VM, using
a randomized, seeded **paired-block** crossover and the frozen decision rule
from the approved plan.

`scx_mavd` needs the arena-scalar prog flag (`BPF_F_ARENA_SCALAR`) and
`scx_nitosis` needs `sched_ext_ops.sub_cid_sched_updated()`. Both are present in
the pinned linux-next source `next-20261016`
(`2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64`), so the custom kernel build needs no
private patches. See `GATES.md`.

Quota enforcement differs per arm: the fair class enforces `cpu.max`, while a
sched_ext scheduler honours it only if it implements
`ops.cgroup_set_bandwidth`. `CGROUP_V2_ANALYSIS.md` maps that per arm and
records what it means for the confirmatory family.

This series owns *in-guest* CPU scheduling only. Kubernetes pod placement is
studied separately in the sibling `kube-sched` series.

## Protocol (frozen)

- One **paired block** runs one workload scenario once per scheduler arm on the
  same `w1`, host, guest image, and kernel. A block is not an individual
  request or trace sample. The measured target is the worker with the most free
  space, chosen once and held fixed with the other worker as load generator.
- At least five complete blocks; a pilot estimates log-scale variance and the
  confirmatory block count is frozen before confirmatory runs, rounded up to a
  multiple of three so the arm order stays position-balanced.
- Arms run in a pre-generated, balanced, seeded order that rotates across
  blocks, so every three-block cycle places each arm in each position once. The
  seed and order are recorded per block.
- The primary estimator is the geometric mean paired ratio
  (back-transformed mean log ratio) with a 95% paired-t interval plus a paired
  block-bootstrap interval. Decisions are corrected across the ten predeclared
  primary contrasts (two sched_ext arms x five workloads) with Holm step-down:
  `improvement`/`regression` need corrected evidence *and* a point estimate past
  the ±5% band, `equivalent` needs the corrected equivalence test, else
  `inconclusive`. Verdicts are per workload; there is no composite score.
- Every comparison sample runs the full observer stack (`runqlat`/`runqlen` over
  the whole window, the `sched_*` bpftrace probe and `perf sched` as bounded
  slices inside it). Results are reported as **instrumented**; a separate EEVDF
  calibration quantifies observer overhead for every scenario.
- Raw data lives outside the Git worktree under a host-owned data root, in
  separate pilot and confirmatory trees; a sample only starts when the target
  worker passes the documented free-space gate.

## Commands

```bash
make linux-sched-setup                                   # prerequisites
make linux-sched-validate                                # validate scenario configs (no cluster)
make linux-sched-list-scenarios                          # list scenarios
make linux-sched-pilot        SCENARIO=http              # pilot blocks (never analysed)
make linux-sched-sample-size                             # freeze confirmatory N from pilot variance
make linux-sched-experiment   SCENARIO=http              # confirmatory blocks
make linux-sched-calibrate    SCENARIO=http              # freeze the offered rate (http, mixed)
make linux-sched-overhead     SCENARIO=http              # observer-overhead calibration
make linux-sched-analyze                                 # decision-rule summary
make linux-sched-plots                                   # SVG figures for the data root
make linux-sched-data-usage                              # report what the data root costs
make linux-sched-prune       KEEP_BLOCKS=5               # drop raw evidence from older blocks
make linux-sched-observer-check                          # node-native observer tooling gate
make linux-sched-workers-label                           # label worker nodes for the observers
make linux-sched-observers-apply                         # deploy the capability-scoped observers
make linux-sched-observers-test                          # validate observer manifests (no cluster)
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
├── observers/                     # capability-scoped observer DaemonSet + gate
├── GATES.md                       # recorded environment findings
└── analysis/                      # paired analysis core + tests
```

## Data layout (host-owned root)

Pilot and confirmatory data are separate: pilot blocks estimate variance and must
never enter a final verdict, and their block numbers would otherwise collide with
confirmatory artifacts. Pipeline smokes have their own tree for the same reason:
a smoke is one block and would leave the pilot unable to size anything.

```
${K8LABS_RESEARCH_DATA_ROOT:-<repo>/build}/research/linux-sched/
├── smoke/blocks.csv               # pipeline checks, never analysed
├── pilot/blocks.csv               # pilot results, excluded from verdicts
├── confirmatory/blocks.csv        # the only file the analysis reads
├── sample-size.json               # frozen per-scenario confirmatory count
├── <phase>/<scenario>/block-NN/
│   ├── metadata.json              # scenario, block, phase, seed, arm order
│   ├── <arm>.txt                  # raw load output
│   ├── <arm>.warmup.txt           # discarded warmup output, kept as evidence
│   ├── <arm>.metric               # extracted primary value
│   ├── <arm>.log                  # workload pod log
│   ├── <arm>.capture-status       # complete | incomplete | failed
│   └── observer-<arm>/            # instrumentation for this arm alone
│       ├── runqlat.txt, runqlen.txt        # histograms, whole window
│       ├── sched-trace.csv.gz              # bpftrace probe, a bounded slice
│       └── perf-latency.txt, perf-sched.txt
├── <phase>/secondary-<scenario>.csv   # secondary outcomes, never in verdicts
└── analysis/summary.{csv,json}
```

The run-queue histograms cover the whole measured window. The trace and
`perf sched` are bounded slices inside it, because the worker filesystem cannot
hold a full-window recording; `GATES.md` records why, and the windows used.
```

## Factorial cgroup-v2 quota campaign (versioned)

A separate, versioned analysis track for the six-arm factorial quota campaign.
It does not alter the scheduler-only protocol above.

- Analysis: `analysis/factorial_analysis.py` (interaction + throttling contrasts,
  global Holm, 5 % performance / 10 % throttling margins).
- Sample sizing: `analysis/factorial_sample_size.py` (90 % power, 12-block balance).

```bash
make linux-sched-factorial-sample-size
make linux-sched-factorial-analyze
```

## Pre-implementation gates

The execution path refuses to run until these are satisfied (it fails loudly
rather than reporting a fake success):

1. A kernel build with the required BTF and sched_ext interfaces, used for
   **all** arms (including EEVDF). All six arms, including `scx_mavd`, are gated
   only by the runtime build/verifier/attach/progress/detach checks (see
   `GATES.md`).
2. Every sched_ext arm binary staged on the worker, with
   verifier/attach/progress/detach checks passing.
3. `bpftrace`, `perf`, and the BCC tools installed on the worker image. These
   are node-native: the image bake installs them (see `GATES.md` for the exact
   packages and paths), and the series ships no privileged observer Pod.
4. Pinned workload images plus the licensed FFmpeg fixture. The images are
   digest-pinned per the repo's `images: digest-only` policy, and the clip,
   the PostgreSQL snapshot and `wrk2` are baked onto the node and checksummed
   before use (see `GATES.md` for the fixture table).
5. A node transport for the runner. SSH to the worker VMs is still broken, but
   `kubectl logs`/`exec` work again after the `system:kube-apiserver-proxy`
   subject fix (see `GATES.md`); the capture path must be driven through the
   Kubernetes API.
6. A frozen offered rate for `http` and `mixed`, whose load arguments carry an
   `__RPS__` placeholder. `make linux-sched-calibrate SCENARIO=http` finds it on
   the control arm by raising the rate until p99 crosses a target and freezing
   the highest rate that stayed under it; `run` refuses to start without it.

Kernel-image contract: the resolved source pin is linux-next `next-20261016`
(`2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64`), which contains both
`BPF_F_ARENA_SCALAR` and `sched_ext_ops.sub_cid_sched_updated()`. Historical
context: the earlier `sched_ext/for-7.4` pin `1f5dde349ac2fca1f119ae8de3f0d2f1c2a8d338`
did not carry the arena-scalar flag (see `GATES.md`).

## Runtime integration gates

```bash
python3 research/linux-sched/experiments/runtime_gate.py   # build/verifier/attach/progress/detach
python3 research/linux-sched/experiments/staging_gate.py     # staging inputs
python3 research/linux-sched/experiments/observer_gate.py    # observer gate
python3 research/linux-sched/experiments/cgroup_manifest.py  # nitosis cgroup parent
```
