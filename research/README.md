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

### Workloads (`research/workloads/`)

| Workload | Type | Description |
|----------|------|-------------|
| stress-ng | Container (alpine) | Configurable CPU stress via env vars |
| cpu-burner | Go HTTP server | 6 endpoints, Prometheus metrics, `runtime.LockOSThread()` |
| co-located | Dual deployment | Latency-sensitive + batch on same node |

## Expected Results

Based on prior research on cgroup v2 CPU throttling:

1. **Baseline**: No throttling, ~200% CPU usage on 2-vCPU workers
2. **Limit-only**: Throttling starts above ~50% of limit, increases non-linearly as demand approaches limit (CFS quota effect)
3. **Request/Limit ratio**: Higher requests (cpu.weight) reduce throttling impact at the same limit
4. **Co-located**: P99 latency spikes during batch interference, mitigated by proper QoS class selection
5. **crun conversion**: The cpu.weight → CpuShares mapping is logarithmic, verified by comparing cgroup values to pod spec

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
├── analysis/                      # (TASK-R006) Analysis scripts
└── FRAMEWORK.md                   # (TASK-R006) CPU parameter selection framework
```
