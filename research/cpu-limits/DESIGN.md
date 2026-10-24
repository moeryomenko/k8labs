# CPU requests and limits design

## Objective

Measure how Kubernetes CPU requests and limits map to cgroup-v2 CPU settings,
and measure their effects on workload performance, delivered CPU, and
throttling. The control is the workload-specific `fit` policy. The primary
worker is the 4-vCPU custom-kernel worker. The 2-vCPU stock worker is a
replication check only.

## Scope boundary

This series measures Kubernetes workload CPU resource settings, cgroup-v2
parameters, and observed scheduler behaviour. It records the QoS slice, Pod
slice, container scope, container leaf, CPU counters, CPU PSI, and selected
scheduler parameters.

It does not measure kube-scheduler placement, compare flat schedulers, attribute
stock versus custom kernel changes, test raw cgroup modes without Kubernetes, or
test sched_ext parent/child delegation. It does not change Packer, cluster
configuration, `scx/`, or kernel source.

## Hypotheses

- **H1:** A CPU request changes `cpu.weight`; it does not create a quota.
- **H2:** A CPU limit creates `cpu.max`; it does not set `cpu.weight`.
- **H3:** CPU weight changes delivered share only under contention.
- **H4:** A CPU quota can throttle a workload while the node has idle CPU.
- **H5:** A bursty workload can have low average CPU use and a high
  throttled-period ratio at the same time.
- **H6:** The performance impact of a limit differs by workload shape.
- **H7:** Lower requests reduce delivered CPU under contention when limits are
  absent.
- **H8:** Equal limits with Guaranteed and Burstable QoS have different
  cgroup hierarchy and may have different observed behaviour.
- **H9:** Throttling changes run-queue delay, wakeup delay, on-CPU slice
  length, and application latency differently by workload shape.

## Workloads

| id | workload shape | primary outcome |
|---|---|---|
| `cpu-bound` | pinned `stress-ng`, two CPU workers | bogo operations per second |
| `io-bound` | pinned nginx with node-baked `wrk2` | p99 latency and requests per second |
| `memory-bound` | pinned `stress-ng --stream`, fallback `--vm` | bogo operations per second |
| `hybrid` | pinned nginx with pinned ffmpeg sidecar | p99 latency and frames per second |

The cgroup probe also records CPU delivered, throttled periods, throttled time,
CPU PSI, run-queue latency, and context switches for every cell.

## Policy matrix

CPU values are per container. `fit` and `low` values are frozen before pilot.

| workload | fit | low |
|---|---:|---:|
| `cpu-bound` | 2000m | 500m |
| `io-bound` | 1000m | 250m |
| `memory-bound` | 1000m | 500m |
| `hybrid` | 1000m | 500m |

| policy | request | limit | QoS | conditions |
|---|---:|---:|---|---|
| `fit` | fit | fit | Guaranteed | solo, contention |
| `best-effort` | none | none | BestEffort | solo |
| `request-low` | 100m | none | Burstable | solo, contention |
| `request-high` | fit | none | Burstable | contention |
| `guaranteed-low` | low | low | Guaranteed | contention |
| `burstable-low` | 100m | low | Burstable | solo, contention |

## Conditions and cells

Each cell has one target Pod. `solo` has no deliberate neighbour. `contention`
adds one fixed CPU-hog neighbour on the same worker.

- Solo: 4 workloads × 4 policies = 16 cells.
- Contention: 4 workloads × 5 policies = 20 cells.
- One complete block: 36 cells.

The design generates a deterministic seeded permutation for every block. The
seed and cell order are stored with the run data.

## Timing

Every cell uses a five-second warmup, a 15-second measurement, and a two-second
cooldown. A trace and perf slice is five seconds inside the measurement for six
selected cells. The pilot runs three complete blocks. Confirmatory block count
comes from pilot variance, is capped at six, and is labelled underpowered if the
required count is higher than six.

## Outcomes

Primary outcomes are CPU-bound throughput, I/O-bound p99 latency and throughput,
memory-bound throughput, and hybrid p99 latency and batch throughput. Every
performance comparison uses the workload-specific `fit` control.

Secondary outcomes are CPU delivered, throttled-period ratio, throttled time,
CPU PSI `some` and `full`, run-queue latency, context switches, cgroup settings,
and the enforcement level of the Pod and containers.

## Decision families and margins

| family | planned contrasts | margin |
|---|---:|---:|
| `limit_effect` | 4 | 5 percent |
| `request_weight` | 4 | 5 percent |
| `guaranteed_vs_burstable` | 4 | 5 percent |
| `throttle_gap` | 8 | 10 percentage points |

Holm step-down correction runs within each family against its planned size.
Missing contrasts do not reduce the correction. A performance regression or
improvement requires a corrected interval that excludes one and a point estimate
outside the five-percent margin. A result is equivalent only when the whole
interval is inside the margin. Other results are inconclusive. Throttle outcomes
are descriptive and use the ten-percentage-point margin.

## Sample-size plan

The paired unit is one complete 36-cell block. The estimator is a geometric mean
paired ratio with a 95-percent paired-t interval and deterministic block
bootstrap interval. The three-block pilot estimates log-scale variance. The
sample-size step uses 90-percent power and the frozen family size. Pilot rows do
not enter a confirmatory verdict. A partial block is incomplete and never
promoted or filled.

## Interfaces and file ownership

The frozen design files are read-only for later tasks. Parallel tasks change only
the files listed below. They do not run `git commit`; the integration task owns
commits after the parallel work is reviewed.

| task | owned files and directories |
|---|---|
| 02a | `workloads/`, `experiments/render_policy.py`, and policy-renderer tests |
| 02b | `experiments/workload_validate.py` and workload-validation tests |
| 03a | `experiments/cgroup_probe.py` and cgroup-probe tests |
| 03b | `experiments/runtime_gate.py` and runtime-gate tests |
| 04a | `observers/collector.py`, `observers/collector.yaml`, and collector tests |
| 04b | `metrics/`, dashboard and scrape configuration files, and metric tests |
| 05a | `capture/sched_trace.bt`, `capture/trace_capture.py`, and trace-capture tests |
| 05b | `capture/perf_capture.py`, `capture/trace_parse.py`, and trace-parser tests |
| 08 | `analysis/`, analysis tests, and the sample-size output contract |

Tasks 02a through 05b and task 08 must not edit `DESIGN.md`, `README.md`,
`GATES.md`, `Taskfile.yml`, or another task's owned files. They must not run
`git commit`.

### Manifest placeholder contract

The policy renderer accepts only these placeholders in workload manifests:
`__NAME__`, `__NODE__`, `__RUN_ID__`, `__WORKLOAD__`, and `__RESOURCES__`.
`__RESOURCES__` is a complete YAML `resources` mapping. The renderer replaces
each placeholder exactly once. It rejects an unknown, repeated, or unresolved
placeholder. It writes the rendered manifest to the run directory and does not
modify the source manifest.

### Cgroup probe JSON schema

Each probe record is one JSON object with this required shape:

```json
{
  "protocol": "cpu-limits-v1",
  "captured_at": "RFC3339 UTC timestamp",
  "run_id": "string",
  "block_id": "integer",
  "cell_id": "string",
  "node": "string",
  "pod_uid": "string",
  "scheduler": {"name": "string", "parameters": {"key": "value"}},
  "levels": [
    {
      "kind": "qos_slice|pod_slice|container_scope|container_leaf",
      "path": "absolute cgroup-v2 path",
      "cpu_weight": 1,
      "cpu_max_quota_us": 100000,
      "cpu_max_period_us": 100000,
      "cpu_stat": {"usage_usec": 0, "nr_periods": 0, "nr_throttled": 0, "throttled_usec": 0},
      "cpu_pressure": {"some_total_usec": 0, "full_total_usec": 0},
      "memory": {"current_bytes": 0, "events": {"key": 0}}
    }
  ]
}
```

A quota value of `null` means `max`. Counters are cumulative snapshots. The
runner computes window deltas only from the snapshots around the measured
window.

### Collector endpoints and metrics

The collector exposes `GET /healthz`, `GET /readyz`, and `GET /metrics` on port
9100. `/metrics` uses Prometheus text format. Every metric includes `node`,
`pod_uid`, `container`, `level`, and `cell_id` labels when the value applies to
a target cgroup.

Required metric names are:

- `container_cpu_usage_seconds_total`
- `container_cpu_cfs_periods_total`
- `container_cpu_cfs_throttled_periods_total`
- `container_cpu_cfs_throttled_seconds_total`
- `container_memory_working_set_bytes`
- `container_pressure_cpu_waiting_seconds_total`
- `k8labs_cgroup_cpu_weight`
- `k8labs_cgroup_cpu_max_quota`
- `k8labs_cgroup_cpu_max_period`
- `k8labs_cgroup_cpu_usage_seconds_total`

### Trace CSV contract

The trace parser writes UTF-8 CSV with this header:

```text
protocol,run_id,block_id,cell_id,source,event,ts_ns,cpu,pid,tgid,comm,target_pid,latency_ns,runtime_ns,prev_state,cgroup_path
```

`source` is `bpftrace` or `perf`. `event` is the source event name. Numeric
fields use base-10 units shown by their suffix. Fields that do not apply to an
event are empty. Raw trace files remain outside Git; this CSV is the stable
parsed interface.

### Campaign CSV contracts

`blocks.csv` has one row per scheduled cell:

```text
protocol,phase,run_id,block_id,seed,position,cell_id,workload,policy,condition,node,pod_uid,status,started_at,finished_at
```

`cgroups.csv` is long-form, one row per captured cgroup value:

```text
protocol,run_id,block_id,cell_id,captured_at,window,level,path,key,value,unit
```

`metrics.csv` is long-form, one row per stored metric sample:

```text
protocol,run_id,block_id,cell_id,timestamp,source,metric,value,unit,labels_json
```

`phase` is `smoke`, `pilot`, or `confirmatory`. `status` is `complete`,
`incomplete`, or `failed`. Only complete confirmatory rows can enter a verdict.
All files use a header, UTF-8 encoding, RFC 4180 CSV quoting, and the
`cpu-limits-v1` protocol value.

## Threats to validity

- The primary worker has four vCPUs. Results may not generalize to other CPU
  counts, host contention, or fleet sizes.
- I/O and hybrid latency can include virtual-network effects. The run records
  node noise, retransmissions, and PSI before interpretation.
- Kubelet, CRI-O, and crun can place controls at different cgroup levels. The
  probe records the full hierarchy rather than assuming container-only
  enforcement.
- The fixed noisy neighbour models one contention shape only.
- Fifteen-second windows can miss long periodic behaviour. Counters and raw
  traces remain available for audit.
- Measurements are conditional on image, kernel, fixture, and capability gates.

## Non-goals

- Tune or replace kube-scheduler.
- Claim a fleet-wide capacity result.
- Change production Pod requests or limits.
- Change the worker image, Packer inputs, cluster configuration, kernel source,
  or sched_ext source.
- Treat a failed gate or missing cell as a measured null result.
