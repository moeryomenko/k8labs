# Pilot summary

## Status

The three-block pilot completed. This record is descriptive only. It does not
make a confirmatory result or a policy recommendation.

## Run settings

- Data root: `build/research/cpu-limits/pilot/`
- Selected node: `k8labs-md-custom-4vcpu-qq498-zlz6p-6n655`
- Matrix: frozen 36-cell matrix
- Timing: 5 s warmup and 15 s measurement per cell
- Trace cells: `solo-cpu-bound-fit`, `solo-cpu-bound-burstable-low`,
  `solo-io-bound-fit`, and `solo-io-bound-burstable-low`
- Trace and perf artifacts exist for every selected trace cell in every block.

## Cell counts

| item | count |
|---|---:|
| planned blocks | 3 |
| complete blocks | 3 |
| planned cells | 108 |
| complete cells | 108 |
| failed cells | 0 |
| retried cells | 0 |

Each block has 36 unique complete cells. No cell needed a retry.

## Primary outcome ranges

These ranges cover all completed pilot cells. They are not adjusted estimates.

| workload | outcome | observations | range |
|---|---|---:|---:|
| cpu-bound | `bogo_ops_per_second` | 27 | 112.84 to 470.02 ops/s |
| memory-bound | `bogo_ops_per_second` | 27 | 2.99 to 6.61 ops/s |
| io-bound | `latency_p99_ms` | 27 | 1.879 to 2.647 ms |
| io-bound | `requests_per_second` | 27 | 400.00 to 400.12 requests/s |
| hybrid | `latency_p99_ms` | 27 | 2.087 to 3.729 ms |
| hybrid | `requests_per_second` | 27 | 400.02 to 400.12 requests/s |
| hybrid | `frames_per_second` | 27 | 1907 to 9359 frames/s |

## Throttled-period ratios

The ratio is `cpu.stat.nr_throttled / cpu.stat.nr_periods` from the
container-leaf measurement delta. A ratio is undefined when the cgroup has
zero CFS periods. This occurs for no-quota policies and is not recorded as a
zero ratio.

| workload | ratios with positive CFS periods | undefined: zero CFS periods | observed range |
|---|---:|---:|---:|
| cpu-bound | 15 | 12 | 0.00% to 100.00% |
| io-bound | 15 | 12 | 0.00% to 0.00% |
| memory-bound | 15 | 12 | 0.00% to 100.00% |
| hybrid | 15 | 12 | 0.00% to 100.00% |
| all workloads | 60 | 48 | 0.00% to 100.00% |

## Data and limits

The raw campaign data is ignored by Git at
`build/research/cpu-limits/pilot/`. The stable campaign files are
`blocks.csv`, `cgroups.csv`, and `metrics.csv`. The data has three block IDs,
108 complete rows, and no duplicate block/cell row.

The pilot estimates variance for the later sample-size step. It is not a
confirmatory analysis. Missing cells would remain incomplete and would not be
filled; no cells are missing in this run.
