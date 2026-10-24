# cpu-limits gates

This file records live capability gates. Every gate is **PENDING** until a
future task records command output and a result. A failed gate stops the affected
campaign path. It does not create synthetic data.

| gate | status | required evidence |
|---|---|---|
| Workload cluster is reachable and selected worker is Ready | PENDING | Command and node status |
| Selected worker uses the required custom kernel | PENDING | Kernel release |
| cgroup v2 CPU controller is available | PENDING | Controller listing |
| cgroup hierarchy is readable from the collector | PENDING | Read-only path check |
| `sched/cgroup_mode` and `base_slice_ns` are readable | PENDING | Debugfs output |
| Collector is Ready and exposes required metrics | PENDING | Pod and `/metrics` output |
| `bpftrace`, `perf`, `wrk2`, and input fixture are available | PENDING | Tool and fixture checks |
| Pinned workload images pull successfully | PENDING | Image pull evidence |
| Noise floor passes the frozen threshold | PENDING | Ten-second sample |
| Target Pods become Ready before the timeout | PENDING | Pod status and events |
| Every recorded block has 36 cells | PENDING | Block manifest |
| `policies.json` and `sample-size.json` exist before confirmatory runs | PENDING | File checks |
