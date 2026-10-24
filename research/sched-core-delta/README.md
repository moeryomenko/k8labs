# sched-core-delta — stock 7.3 versus the custom kernel

Controlled comparison of the stock Fedora kernel and the k8labs comparison
kernel for the scheduler changes in the `sched-core-2026-10-18` series. The
question is not "is the custom kernel faster" in general, but which of the
series' mechanisms produce a measurable effect under CPU contention, and
whether the effect matches the source analysis.

## Arms

| arm | kernel | source | scheduler series |
|---|---|---|---|
| `stock` | `7.3.0-65.fc46.x86_64` | Fedora rawhide, pinned RPMs | none |
| `custom` | `7.3.0-k8labs` | local linux-next tree `734bef548a96e8774d6eef5dfa2026c6edc4e80b` (`v7.3-7363-g734bef548a96`) | `sched-core-2026-10-18`, merge `cb95904b470d` |
| `custom-nogov` | same custom kernel | same | series, `steal_governor` unloaded |
| `custom-gov` | same custom kernel | same | series, `steal_governor` loaded with the requested parameters |

The custom tree is **not** stock 7.3 plus one series. It contains 7,363 commits
after v7.3, including the 7.4 merge window. The `sched-core-2026-10-18` merge is
the only scheduler series identified by the tree analysis; other subsystems may
also differ. That is why the campaign measures mechanisms, not just a composite
kernel-to-kernel score.

The scheduler-relevant configuration is identical between the two kernels
apart from the two new options:

| option | stock | custom |
|---|---|---|
| `CONFIG_HZ` | 1000 | 1000 |
| `CONFIG_PREEMPT_LAZY` | y | y |
| `CONFIG_PREEMPT_DYNAMIC` | y | y |
| `CONFIG_SCHED_CORE` | y | y |
| `CONFIG_CFS_BANDWIDTH` | y | y |
| `CONFIG_CGROUP_SCHED` | y | y |
| `CONFIG_FAIR_GROUP_SCHED` | y | y |
| `CONFIG_SCHEDSTATS` | y | y |
| `CONFIG_SCHED_CLASS_EXT` | y | y |
| `CONFIG_PREFERRED_CPU` | - | y |
| `CONFIG_STEAL_GOVERNOR` | - | m |

The stock config hash is `e7b7b37657c723efc94afa7576ef44885b7ed5c54a589782cfd97c61ba7c3f5b`;
the custom config hash is `b016c966fdae345871a8e8b8a708226f818c8146b6b97c384f77b774f43a22fc`.
The runner records both and fails closed when the arm's kernel release or
preferred-CPU ABI does not match.

## What is measured

| scenario | workload | mechanism it targets | primary metrics |
|---|---|---|---|
| `wakeup` | two full-time hogs (weight 400) and a 1 ms timer probe (weight 100) | EEVDF slice protection, timer restart, candidate selection | probe p99/p99.9 wakeup delay, hog throughput |
| `slice` | two hogs plus a 500 us burst every 5 ms | short-slice protection | burst p99 start delay, missed periods (descriptive) |
| `fairness` | two hogs plus four equal-weight 2 ms periodic tasks | equal-slice candidate selection | p99 delay, per-thread coefficient of variation |
| `quota` | a 0.5 CPU `cpu.max` group with two hogs and a timer probe, next to an uncapped group | CFS bandwidth (commits 21 and 63) | delivered CPU, throttled periods, probe p99 |
| `churn` | two hogs plus 200 short-lived tasks/s | scheduler constant overhead (rbtrees, static keys) | task start p99, tasks/s |
| `steal` | an interacting pipe pair hard-pinned one thread per vCPU plus two unpinned burners, under host CPU contention | steal governor (commits 34-47), including the affinity-preserving branch | pair p99 round-trip, pair throughput, independent throughput |

`quota` is an explicit **null control**. The series removed a queueing branch
and restored it inside the same series; it must not grant extra CPU or remove
throttling. A difference there is evidence of an unintended change or a
confound, not evidence of an improvement.

`steal` is a **governor contrast**, not a stock-versus-custom contrast. It runs
only on the custom kernel with the module unloaded (`custom-nogov`) and loaded
(`custom-gov`). This separates the new feature from the rest of the series.

## Protocol

- One **block** runs the same scenario matrix once per arm. The per-repetition
  median is the block value.
- Three or more complete blocks are required for any verdict. A pilot is
  labelled as a pilot and never promoted into a confirmatory claim.
- The arm order alternates across blocks so time trends and host load do not
  consistently favour one arm.
- The estimator is the geometric mean paired ratio with a 95 % paired
  block-bootstrap interval. The practical-effect band is +/-5 %.
  `improved`/`regressed` requires the interval to exclude 1 and the point
  estimate to pass the band; `equivalent` requires the whole interval inside
  the band; everything else is `inconclusive`. For a metric declared
  `equivalent`, an interval entirely outside the band is reported as
  `different` and is a failure of the null control.
- Every claim is predeclared in `experiments/scenarios.py`. The analysis does
  not choose a metric after seeing the data.

The default campaign is **cross-node**: the stock kernel runs on `md-0` and
the custom kernel on `md-custom`. The two workers are separate VMs on the same
host. This is a paired comparison, but the kernel is confounded with VM
identity and host placement. The analyzer prints that warning. The same-node
design is documented below and is the confirmatory design when the lab can
afford the reboot.

## Commands

```bash
task sched-core-delta-check                  # both workers, kernel identity and tools
task sched-core-delta-plan                   # seeded plan, no changes
task sched-core-delta-pilot                  # 3 blocks, 1 rep x 3 s
task sched-core-delta-run                    # 3 blocks, 3 reps x 8 s
task sched-core-delta-analyze                # latest run
task sched-core-delta-report                 # analyze and write a report (OUT, default RESULTS-generated.md)
task sched-core-delta-steal                  # governor contrast under host steal
task sched-core-delta-test                   # pytest, py_compile, bash -n, shellcheck
```

Direct invocation, with a fixed run id so the arms land in one tree:

```bash
RUN_ID=20261024T120000Z research/sched-core-delta/experiments/run-campaign.sh \
    --blocks 3 --profile full --run-id "$RUN_ID"
```

The runner can also execute one arm at a time:

```bash
research/sched-core-delta/experiments/run-matrix.sh \
    --arm custom --block 01 --profile pilot --run-id demo
```

Environment overrides: `SCHED_CORE_DELTA_DATA_ROOT` (or
`K8LABS_RESEARCH_DATA_ROOT`), `RUN_ID`, `BLOCKS`, `REPS`, `DURATION`,
`WARMUP`, `CPUS`, `HOGS`, `CP_HOST`, `CP_PORT`, `CP_KEY`, `WORKER_KEY`.

## Host steal procedure

The steal scenario needs real steal time, not a timer-only simulation. The
helper pins the target VM's vCPU threads to two host CPUs and starts one
spinner per CPU:

```bash
# Inspect first; this changes nothing.
research/sched-core-delta/experiments/host-steal.sh \
    --node k8labs-md-custom-... --status

# Start and stop explicitly. The state file records the original affinity.
research/sched-core-delta/experiments/host-steal.sh \
    --node k8labs-md-custom-... --cpus 2,4 --hogs 2 --start
research/sched-core-delta/experiments/host-steal.sh \
    --node k8labs-md-custom-... --stop
```

`task sched-core-delta-steal` wraps the start, each block's two guest runs, and
the stop in one script with an `EXIT` trap. The helper never reboots the VM and never
changes the cluster configuration. If it is interrupted, run `--stop`; the
state directory is `/tmp/sched-core-delta-steal/state-<node>/`.

The default governor thresholds are `interval_ms=1000`, `high_threshold=500`
(5 %) and `low_threshold=200` (2 %). The runner loads the module with those
values. `--gov-high 1 --gov-low 0` is available for a mechanism check at a
lower steal level; it is not the deployed policy and is reported separately.

## Same-node confirmation

Both kernels and both UKIs are installed on both worker images. The confirmatory
design is therefore one VM booted into each kernel, in alternating blocks:

1. Cordon and drain the research worker; confirm no research pods remain.
2. Write the target UKI to `/boot/efi/loader/loader.conf`:

   ```text
   default k8labs-stock-7.3.0-65.fc46.x86_64.efi
   timeout 5
   editor no
   ```

   or `k8labs-custom-7.3.0-k8labs.efi`.
3. Reboot, wait for the node to return Ready, and verify `uname -r`.
4. Run the block, then switch back.

This is deliberately not automated. The lab has a known first-boot confext /
SELinux race in the guest; a reboot of a research worker must be watched by an
operator and the node must rejoin before the next block. The cross-node campaign
does not reboot anything and is the safe default.

## Data layout

```
build/research/sched-core-delta/<run-id>/
├── campaign.json
├── block-01/
│   ├── stock/
│   │   ├── run.json         # arm, block, node, kernel, settings
│   │   ├── meta.json        # kernel config, governor, schedstats, boot id
│   │   ├── matrix.jsonl     # group and system rows, one per rep
│   │   └── run.log
│   └── custom/...
└── analysis/
    ├── summary.md
    ├── summary.csv
    └── summary.json
```

`campaign.json` is written by `run-campaign.sh`; the governor driver writes the
same block layout and a campaign summary for a new run. `matrix.jsonl` rows
carry the raw counters and the delay distributions. The `system` row carries
steal time, the preferred-CPU mask over time, PSI, and the context-switch rate.
`meta.json` carries the kernel config hash and the governor/schedstats state
before and after the run.

## Interpretation limits

- **Userspace mutexes do not use kernel proxy execution.** The proxy-execution
  fixes (commits 1-6, 29-33) affect kernel mutexes and scheduler state. A
  pthread mutex chain is not a proxy-execution workload. This series does not
  claim a userspace lock speedup.
- **Core-scheduling cookie fixes** require core scheduling and proxy execution
  to be active together. Container boundaries alone do not enable either.
- **The 2 vCPU worker** cannot resolve LLC/NUMA or SMT placement effects. The
  custom kernel's NUMA and SMT changes are out of scope here.
- **Raw cgroups, not pods.** The kubelet/CRI-O/crun path maps CPU requests to
  `cpu.weight` and limits to `cpu.max`; the raw cgroup matrix exercises the
  same kernel interface with less runtime noise. The Kubernetes QoS sanity
  check in the sibling `sched-core-modes` series remains the end-to-end check.
- **Cross-node blocks** are a paired but confounded design. Use the same-node
  procedure for a causal claim.
- **Steal is induced, not natural.** The governor result describes the
  mechanism under controlled contention; it is not a production capacity
  statement.
- **The steal pair is hard-pinned.** It tests the affinity-preserving branch
  of the preferred-CPU contract; the governor moves the unpinned burners, not
  the pair. Full pair consolidation needs an unpinned variant.

## Result

The integrated analysis is in [RESULTS.md](RESULTS.md). The raw generated
reports are [RESULTS-kernel.md](RESULTS-kernel.md) for the stock-versus-custom
campaign and [RESULTS-steal.md](RESULTS-steal.md) for the governor contrast.
The hypothesis map and the expected direction of each metric are in
[HYPOTHESES.md](HYPOTHESES.md).
