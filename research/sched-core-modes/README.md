# sched-core-modes — cgroup-v2 scheduler mode study

Controlled experiment for the five `cgroup_mode` values introduced by the
sched-core-2026-08-17 series:

| mode | effective group weight | meaning of `cpu.weight` |
|---|---|---|
| `up` | `1` per CPU (`F_g_n = 1`) | ignore SMP; full group weight on every CPU |
| `smp` | `F_g_n` (group total = `weight`) | traditional total weight from kubelet requests |
| `concur` | `min(tasks, cpus) * weight` | weight per active CPU |
| `max` | `cpus * weight` (clipped at nice -20) | weight per allowed CPU |
| `tasks` | `tasks * weight` | weight per runnable task |

The target is a lab worker with two vCPUs. The runner sets `cpu.weight` and
`cpu.max` directly on raw cgroup-v2 groups, so the measurement is not filtered
through the kubelet or CRI-O. A Kubernetes manifest in `workloads/` is provided
for a secondary QoS sanity check.

## Scenarios

| scenario | groups | what it separates |
|---|---|---|
| `weighted_threads` | `400/1t`, `200/2t`, `100/4t` | thread-count amplification: the inverse weight/thread pairs make `smp`, `concur`, `max` and `tasks` predict different shares |
| `qos_nested` | Guaranteed-shaped parent `100/1t` vs Burstable-shaped parent `100` with two `100/2t` pod children | QoS-level amplification and pod-internal fairness |
| `quota` | two `100/2t` groups, one with `cpu.max=50000 100000` | `cpu.max` throttling is independent of `cgroup_mode` |
| `latency` | load `400/2t` plus a 1 ms timer probe in `100/1t` | p50/p99/p99.9/max wakeup delay, context switches |

Expected shares under the idealized full-concurrency model (two CPUs):

`weighted_threads`:

| mode | w400-t1 | w200-t2 | w100-t4 |
|---|---:|---:|---:|
| `up` | 57.1% | 28.6% | 14.3% |
| `smp` | 57.1% | 28.6% | 14.3% |
| `concur` | 40.0% | 40.0% | 20.0% |
| `max` | 57.1% | 28.6% | 14.3% |
| `tasks` | 33.3% | 33.3% | 33.3% |

`qos_nested` (QoS parent shares):

| mode | Guaranteed | Burstable | burstable/guaranteed |
|---|---:|---:|---:|
| `up` | 50% | 50% | 1.0x |
| `smp` | 50% | 50% | 1.0x |
| `concur` | 33.3% | 66.7% | 2.0x |
| `max` | 50% | 50% | 1.0x |
| `tasks` | 20% | 80% | 4.0x |

The model assumes every group is runnable on every CPU. `up` and `smp` have
the same idealized share; the measured difference is the per-CPU placement
effect that `up` preserves. The analyzer reports the model delta and a
`relative_to_smp` column paired by repetition, so the mode effect is not
confused with placement.

## Running

Prerequisites:

- The workload cluster is up and `build/kubeconfig` is current.
- The host can SSH to the control plane; the control plane carries the worker
  key at `/tmp/id_ed25519`.
- The worker has `gcc`, `python3`, cgroup v2 with `cpu`/`cpuset`, and debugfs.
- `kubectl` and `python3` on the host.

```sh
# Discover the worker, check kernel/cgroup/debugfs/tools.
task sched-core-modes-setup

# Seeded plan only; does not change the worker mode.
task sched-core-modes-plan PROFILE=pilot

# Pilot: 1 repetition, 3 s measurement, 1 s warmup, all four scenarios.
task sched-core-modes-pilot

# Full: 3 repetitions, 8 s measurement, 2 s warmup.
task sched-core-modes-run

# Analyze the latest run.
task sched-core-modes-analyze
task sched-core-modes-report
```

Select the stock worker for a cross-kernel check:

```sh
task sched-core-modes-run WORKER=stock
```

Select a specific run or data root:

```sh
task sched-core-modes-run RUN_ID=20261024T120000Z
task sched-core-modes-analyze \
  SCHED_CORE_MODES_RUN_DIR=build/research/sched-core-modes/20261024T120000Z
```

Environment overrides: `REPS`, `DURATION`, `WARMUP`, `COOLDOWN`,
`SCENARIOS`, `MODES`, `PARENT_WEIGHT`, `WORKER`, `CP_HOST`, `CP_PORT`,
`CP_KEY`, `WORKER_KEY`, `REMOTE_DIR`, `DATA_ROOT`.

## Optional Kubernetes QoS sanity check

`workloads/k8s-qos.yaml` runs four short `stress-ng` Jobs on one worker:
Guaranteed (requests = limits = 500m), Burstable (250m request, 1 CPU limit),
BestEffort (no requests/limits) and a quota-shaped pod (250m request, 500m
limit). The kubelet derives `cpu.weight` from the requests, so this checks
that the real QoS shapes reach the same weight/limit plumbing. It is a sanity
check, not the controlled matrix: the kubelet, runtime and system pods add
their own weights and scheduling.

```sh
NODE=k8labs-md-custom-hbsml-qx6v4-mch2q \
  task sched-core-modes-k8s
task sched-core-modes-k8s-clean
```

The Jobs print `stress-ng` bogo-ops in their logs. Compare the four logs and,
if needed, read the pod cgroups on the worker:

```sh
grep usage_usec /sys/fs/cgroup/kubepods.slice/kubepods-*.slice/kubepods-*-pod*.slice/cpu.stat
```

## Data and outputs

Data lives under `build/research/sched-core-modes/<run-id>/` (outside the
tracked source tree). A run directory contains:

- `run.log` — the worker-side runner log
- `matrix.jsonl` — one row per measured cgroup per repetition
- `meta.json` — kernel, node, CPU count, seeded mode order, restored mode
- `run.json` — worker class, profile and host-side settings

The analyzer writes to `<run>/analysis/`:

- `shares.csv` — wide table: measured share, model share, delta,
  `relative_to_smp`, per-task share, throughput
- `metrics.csv` — long table: share, per-task share, throughput,
  context switches, QoS ratio, throttle ratios, wakeup percentiles
- `pods.csv` — pod-level shares for the nested QoS scenario
- `summary.json` — the same data in machine-readable form
- `summary.md` — human-readable tables and generated findings

`task sched-core-modes-report` copies the latest `summary.md` to
`RESULTS.md` in this directory.

## Results

The full 3-repetition, 8-second run on the custom-kernel worker is in
[RESULTS.md](RESULTS.md). Key numbers (two vCPUs, kernel `7.3.0-k8labs`):

- QoS amplification: `up` 3.01x, `smp` 1.07x, `concur` 2.00x, `max` 1.07x,
  `tasks` 3.98x for Burstable versus Guaranteed at the same parent weight.
- `up` also destabilized pod-level fairness: the two equal Burstable pods
  split 61.2/38.8% (sd 9.7) under `up`, while the other modes stayed within
  0.2 points of 50/50.
- `weighted_threads`: the 4-thread group gained 1.53x (`concur`) and 2.16x
  (`tasks`) over its `smp` share; the 2-thread group did not gain (0.77x and
  0.68x) because its one-thread-per-CPU placement does not produce the same
  group runnable average.
- `quota`: the limited group stayed at 26.1-26.4% in every mode, so `cpu.max`
  is independent of `cgroup_mode`; `up` throttled in 81% of CFS periods versus
  100% for the other modes, without changing the CPU share.
- `latency`: p99 wakeup delay was 697-1327 us; the best p99.9/max tail was
  `max` (2512/4263 us), the worst `up` (4381/7257 us).

A one-repetition pilot on the stock `7.3.0-65.fc46` worker reproduced the same
ordering: `up` 2.94x, `smp` 1.05x, `concur` 2.00x, `max` 1.03x, `tasks` 3.70x
for QoS amplification, and 1.55x/2.40x for the 4-thread group under
`concur`/`tasks`. That pilot also produced a 32/68 pod split under `tasks` in
its single repetition, so pod-level fairness is placement-sensitive in the
thread-scaling modes as well; the three-repetition custom-kernel run above is
the primary result.

## Measurement details

- Each group is a direct child of `/sys/fs/cgroup/sched-modes`; the parent has
  `cpu.weight=300` and `cpuset.cpus=0-(N-1)`.
- CPU time comes from the group's `cpu.stat` `usage_usec` delta. A nested
  parent's `usage_usec` includes its children; the analyzer therefore uses
  parent rows for QoS shares and child rows for pod shares, never the sum of
  both.
- Throughput is the `cpu_burn` outer-loop iteration count. The arithmetic and
  duration are identical across modes, so iterations track delivered CPU time.
- Wakeup delay is measured by an absolute `CLOCK_MONOTONIC` 1 ms timer; the
  probe reports p50/p99/p99.9/max in microseconds.
- Quota metrics are `nr_throttled / nr_periods`, `throttled_usec / usage_usec`
  and `throttled_usec / wall_time`.
- The mode order is shuffled per repetition with the seed recorded in
  `meta.json`; the worker returns to `concur` after every repetition and after
  the run.

## Safety

`run-matrix.sh` restores `concur` and removes `/sys/fs/cgroup/sched-modes` in
an `EXIT` trap, including on SSH failure or interruption. The worker-side
runner does the same in a `finally` block and converts `SIGINT`/`SIGTERM` to
`SystemExit` so the cleanup still runs. A failed run leaves the JSONL and log
for diagnosis but never leaves the worker in a test mode.

## Limitations

- Two vCPUs only. The `max` clip at nice -20 is not reached by the weights used
  here, so `max` and `smp` are expected to have the same idealized shares.
- Raw cgroups, not Kubernetes pods. The weights are set directly, not derived
  by the kubelet from requests; the nested scenario models the QoS shape, not
  the full kubelet accounting.
- The idealized model assumes full CPU occupancy. `up` and `smp` differ only
  when placement is uneven; the `relative_to_smp` column makes that visible.
- `cpu.max` is enforced by CFS bandwidth control and is expected to be
  mode-independent. The quota scenario is a control, not a mode comparison.
- The 1 ms timer probe measures wakeup delay under a CPU-bound load group; it
  does not represent network or block I/O latency.
