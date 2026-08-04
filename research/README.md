# k8labs — Container Process & CPU Throttling Research

Reproducible research on the k8labs Kubernetes cluster investigating:

1. **Container process lifecycle** — tracing the full chain from `kubectl apply` through scheduler, kubelet, CRI-O, crun, to cgroups v2
2. **CPU throttling characterization** — measuring cgroup v2 `cpu.stat` under varying CPU limit configurations
3. **CPU parameter selection** — developing evidence-based guidance for pod CPU request/limit choices

## Prerequisites

- **k8labs cluster running and healthy** (`kubectl --kubeconfig kubeconfig get nodes` returns 3 Ready nodes)
- **kubectl** with a working kubeconfig
- **tofu** (or terraform) with state from `tofu apply`
- **virsh** with access to the KVM VMs
- **SSH key** injected into VMs (done via cloud-init during `make deploy`)
- **yq** (optional, for advanced YAML parsing)

## Quick Start

### 1. Deploy the cluster

```bash
cd k8labs
make cluster
```

### 2. Verify prerequisites

```bash
cd research
make setup
```

### 3. Run individual experiments

```bash
make experiment-baseline        # ~7 min — 1 config x 3 replicates
make experiment-limits          # ~50 min — 7 configs x 3 replicates
make experiment-request-limit   # ~58 min — 8 configs x 3 replicates
make experiment-colocated       # ~12 min — 1 config x 5 replicates
```

### 4. Run all experiments sequentially

```bash
make all-experiments   # ~2 hours total
```

### 5. Generate analysis report

```bash
make report   # (after TASK-R006 is implemented)
```

### 6. Run EEVDF experiments

```bash
make experiment-eevdf-metrics            # ~15 min — cpu-burner, 3 limit configs x 3 replicates
make experiment-eevdf-stress             # ~30 min — stress-ng, 4 limit configs x 3 replicates
make experiment-eevdf-stress-perfetto    # ~30 min — stress-ng with Perfetto tracing
make experiment-eevdf-api-baseline       # ~15 min — API server workload
make experiment-eevdf-db-baseline        # ~15 min — database workload
```

### 7. Analyze EEVDF results

```bash
make eevdf-analyze   # Extract EEVDF metrics from experiment data
make eevdf-plots     # Generate vruntime, slice, and latency plots
make eevdf-report    # View findings in research/EEVDF-DEEP-DIVE.md
```

## Experiment Descriptions

### Experiment A: Baseline (`throttling-baseline.yaml`)

| Param | Value |
|-------|-------|
| Workload | stress-ng, 2 cores, 100% load |
| CPU requests | (none) |
| CPU limits | (none) |
| Duration | 120s per run |
| Replicates | 3 |
| Cells | 1 |
| Expected | No throttling, full CPU utilization |

### Experiment B: Limit-Only Sweep (`throttling-limits.yaml`)

| Param | Value |
|-------|-------|
| Limits tested | 100m, 250m, 500m, 750m, 1000m, 1500m, 2000m |
| Requests | (none) |
| Workload | stress-ng, 2 cores, 100% load |
| Duration | 120s per run |
| Replicates | 3 per config |
| Cells | 7 |

### Experiment C: Request/Limit Interaction (`throttling-request-limit.yaml`)

| Param | Value |
|-------|-------|
| Request set | 100m, 500m, 1000m |
| Limit ratios | 2x, 3x, 5x (plus Guaranteed) |
| Workload | Go HTTP CPU burner (fibonacci endpoint) |
| Duration | 120s per run |
| Replicates | 3 per config |
| Cells | 8 |

### Experiment D: Co-located Workloads (`co-located.yaml`)

| Param | Value |
|-------|-------|
| Latency app | Go HTTP server, request=200m, limit=500m |
| Interference | stress-ng, request=1000m, limit=2000m |
| Co-location | Same worker node |
| Duration | 120s per run |
| Replicates | 5 |
| Cells | 1 |

### Experiment E: EEVDF Metrics (`eevdf-metrics.yaml`)

| Param | Value |
|-------|-------|
| Workload | cpu-burner (fibonacci endpoint) |
| CPU configs | no limits, 500m limit, 1000m limit |
| Duration | 120s per run |
| Replicates | 3 |
| Cells | 3 |
| Measurement | cgroup stats + EEVDF vruntime/deadline/slice metrics |
| Expected | Baseline latency distribution for EEVDF under different caps |

### Experiment F: EEVDF Stress-ng Metrics (`eevdf-metrics-stress.yaml`)

| Param | Value |
|-------|-------|
| Workload | stress-ng, 2 cores, 100% load |
| CPU configs | no limits, 250m, 750m, 1500m |
| Duration | 120s per run |
| Replicates | 3 |
| Cells | 4 |
| Measurement | cgroup stats + full Perfetto tracing |
| Expected | Throttling profiles match CFS behavior (same cpu.max mechanism) |

### Experiment G: EEVDF API Server Baseline (`eevdf-api-baseline.yaml`)

| Param | Value |
|-------|-------|
| Workload | cpu-burner (fibonacci endpoint) |
| CPU configs | no limits, 200m/500m, 500m/1000m |
| Duration | 120s per run |
| Replicates | 3 |
| Cells | 3 |
| Measurement | cgroup stats + request-rate correlation |
| Expected | Variable CPU intensity by endpoint; burst allowance absorbs traffic spikes |

### Experiment H: EEVDF Database Baseline (`eevdf-db-baseline.yaml`)

| Param | Value |
|-------|-------|
| Workload | cpu-burner (fibonacci endpoint) |
| CPU configs | no limits, 500m/1000m, 1000m/2000m |
| Duration | 120s per run |
| Replicates | 3 |
| Cells | 3 |
| Measurement | cgroup stats + checkpoint spike simulation |
| Expected | Guaranteed QoS absorbs periodic spikes without throttling |

## Output Structure

After experiments run, data is organized as:

```
research/experiments/data/
├── throttling-baseline/
│   └── 2026-07-27T112233Z/
│       ├── 001_request=_limit=_rep1/
│       │   ├── cgroup-watch.csv       # Polled cgroup stats
│       │   ├── kubectl-top.csv        # kubectl top pod samples
│       │   └── metadata.json          # Run metadata
│       ├── 002_request=_limit=_rep2/
│       └── 003_request=_limit=_rep3/
│   └── summary.csv                   # Aggregated results
├── throttling-limits/
├── throttling-request-limit/
└── co-located/
```

## Research Tooling

### Cgroup Observation Tools (`research/bin/`)

| Tool | Description |
|------|-------------|
| `cgroup-observe.sh` | Read cgroup v2 CPU stats for a pod/container → JSON |
| `cgroup-watch.sh` | Poll cgroup stats at interval → CSV |
| `cgroup-snapshot.sh` | One-shot full cgroup state dump → JSON |
| `trace-lifecycle.sh` | Trace pod creation to cgroup writes (OCI spec extraction) |

### EEVDF Analysis Toolkit (`research/analysis/`)

| Tool | Description |
|------|-------------|
| `eevdf-analyze.py` | Extract EEVDF scheduler metrics — vruntime trajectory, slice duration, wakeup latency, per-task lag |
| `eevdf-plot.py` | Generate EEVDF visualization plots — vruntime trajectory, slice histogram, deadline drift, lag timeseries, sched latency ECDF |
| `sched-latency-heatmap.py` | Generate scheduling latency heatmaps from perfetto trace data |

### Make Targets for EEVDF

| Target | Description |
|--------|-------------|
| `make experiment-eevdf-metrics` | EEVDF metrics with cpu-burner (9 runs) |
| `make experiment-eevdf-stress` | EEVDF metrics with stress-ng (12 runs, ~30min) |
| `make experiment-eevdf-stress-perfetto` | EEVDF stress-ng with Perfetto tracing |
| `make experiment-eevdf-api-baseline` | HTTP API server workload baseline |
| `make experiment-eevdf-db-baseline` | Database workload baseline |
| `make eevdf-analyze` | Run EEVDF analysis pipeline |
| `make eevdf-plots` | Generate EEVDF visualization plots |
| `make eevdf-report` | Show pointer to EEVDF deep-dive report |

### Workloads (`research/workloads/`)

| Workload | Type | Description |
|----------|------|-------------|
| stress-ng | Container (alpine) | Configurable CPU stress via env vars |
| cpu-burner | Go HTTP server | 6 endpoints, Prometheus metrics, `runtime.LockOSThread()` |
| co-located | Dual deployment | Latency-sensitive + batch on same node |
| eevdf-metrics | cpu-burner | EEVDF vruntime/deadline/slice analysis under limit sweep |
| eevdf-metrics-stress | stress-ng | EEVDF throttling analysis with full Perfetto tracing |
| eevdf-api-baseline | cpu-burner | HTTP API server CPU profile characterization |
| eevdf-db-baseline | cpu-burner | Database checkpoint spike simulation |

## Expected Results

Based on prior research on cgroup v2 CPU throttling:

1. **Baseline**: No throttling, ~200% CPU usage on 2-vCPU workers
2. **Limit-only**: Throttling starts above ~50% of limit, increases non-linearly as demand approaches limit (CFS quota effect)
3. **Request/Limit ratio**: Higher requests (cpu.weight) reduce throttling impact at the same limit
4. **Co-located**: P99 latency spikes during batch interference, mitigated by proper QoS class selection
5. **crun conversion**: The cpu.weight → CpuShares mapping is logarithmic, verified by comparing cgroup values to pod spec

## Perfetto System Tracing

Perfetto is integrated into the research framework for capturing CPU scheduling traces during experiments.

### Quick Start

```bash
# Build and deploy the Perfetto sysext
make perfetto-sysext

# Run an experiment with Perfetto tracing
make experiment-baseline PERFETTO=1

# Analyze and visualize traces
make perfetto-analyze
make perfetto-plots
```

### Trace Configs

| Config | Description |
|--------|-------------|
| `scheduling` | CPU scheduling events (sched_switch, sched_waking) |
| `full-system` | Scheduling + CPU frequency + process stats |
| `syscalls` | System call tracing with scheduling context |

### Make Targets

| Target | Description |
|--------|-------------|
| `make perfetto-capture` | Interactive capture from a node |
| `make perfetto-analyze` | Analyze all traces in experiment data |
| `make perfetto-plots` | Generate CPU execution time plots |
| `make perfetto-view` | Serve the newest trace and open it in the Perfetto UI |
| `make perfetto-clean` | Remove all Perfetto trace data |

### Viewing Traces

`make perfetto-view` serves the newest `*.perfetto-trace` under
`experiments/data` (recursive, newest mtime) as a raw file over HTTP via the
read-only CORS server `perfetto/bin/perfetto-serve.py`, then opens
`https://ui.perfetto.dev/#!/?url=http://127.0.0.1:<port>/<trace-basename>`.
The Perfetto UI treats the `?url=` parameter as a raw trace file: it fetches the
URL and the in-browser WASM trace processor parses the returned bytes. The
server binds `127.0.0.1` only and sends
`Access-Control-Allow-Origin: https://ui.perfetto.dev` on every response so the
UI's fetch succeeds. Override the port with `PERFETTO_VIEW_PORT` (default 9001).
Press Ctrl-C to stop the server; it is killed on EXIT/INT/TERM.

**Why the old recipe failed:** the previous target ran
`trace_processor --httpd --http-port <port>` and opened the bare URL
`https://ui.perfetto.dev/#!/?url=http://127.0.0.1:<port>`. The Perfetto UI still
treats a bare localhost URL as a raw trace file, but the httpd root returns a
plain-text RPC help page instead of trace bytes, so the WASM parser failed with
"Unknown trace type provided (ERR:fmt)".

**Alternative for very large traces:** if the trace is too big to fetch into the
browser efficiently, use the native trace processor acceleration instead:

```bash
trace_processor --httpd --http-port 9001 /path/to/trace.perfetto-trace
# then open https://ui.perfetto.dev and click YES on the
# "Trace Processor native acceleration" dialog (Enter the "Add Trace
# Processor" / httpd URL as http://127.0.0.1:9001)
```

The httpd server is an RPC server, not a file server — it must never be
referenced with a bare `?url=http://127.0.0.1:<port>` URL, because that path
makes the UI treat the RPC help page as a raw trace.

## File Structure

```
research/
├── Makefile                       # Top-level experiment targets
├── README.md                      # This file
├── bin/                           # Cgroup observation tools
│   ├── cgroup-common.sh
│   ├── cgroup-observe.sh
│   ├── cgroup-watch.sh
│   ├── cgroup-snapshot.sh
│   └── trace-lifecycle.sh
├── workloads/                     # Workload containers + manifests
│   ├── Makefile
│   ├── stress-ng/
│   │   ├── Containerfile
│   │   └── deploy.yaml
│   ├── cpu-burner/
│   │   ├── main.go
│   │   ├── go.mod
│   │   ├── Containerfile
│   │   ├── deploy.yaml
│   │   └── Makefile
│   └── co-located/
│       ├── latency-sensitive.yaml
│       └── batch-burner.yaml
├── experiments/                   # Experiment orchestration
│   ├── run-experiment.sh
│   ├── common.sh
│   ├── configs/
│   │   ├── throttling-baseline.yaml
│   │   ├── throttling-limits.yaml
│   │   ├── throttling-request-limit.yaml
│   │   └── co-located.yaml
│   └── data/                      # Collected data (gitignored)
├── analysis/                      # Analysis scripts
│   ├── eevdf-analyze.py           # EEVDF metrics extraction
│   ├── eevdf-plot.py              # EEVDF visualization plots
│   └── sched-latency-heatmap.py   # Scheduling latency heatmaps
├── EEVDF-DEEP-DIVE.md             # Detailed EEVDF experimental results
└── FRAMEWORK.md                   # CPU parameter selection framework
```
