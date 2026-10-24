# CPU-limits pilot descriptive results

This is pilot descriptive, not confirmatory.

## Campaign coverage

- Complete cells: 108
- Complete blocks: 3

## Environment

- blocks: 3
- cells_per_block: 36
- cgroup_version: v2
- complete_cells: 108
- kernel: 7.3.0-k8labs
- worker: k8labs-md-custom-4vcpu-qq498-zlz6p-6n655

## Summary

Data: `build/research/cpu-limits/pilot/{blocks,cgroups,metrics}.csv`; 3 blocks and 108 complete cells.

# CPU-limits pilot descriptive summary

Complete cells: 108
Incomplete cells: 0

## Pilot descriptive contrast availability

| family | workload | metric | pilot label | paired blocks |
|---|---|---|---|---:|
| limit_effect | cpu-bound | throughput | pilot descriptive | 3 |
| limit_effect | hybrid | latency_p99_ms | pilot descriptive | 3 |
| limit_effect | io-bound | latency_p99_ms | pilot descriptive | 3 |
| limit_effect | memory-bound | throughput | pilot descriptive | 3 |
| request_weight | cpu-bound | throughput | pilot descriptive | 3 |
| request_weight | hybrid | latency_p99_ms | pilot descriptive | 3 |
| request_weight | io-bound | latency_p99_ms | pilot descriptive | 3 |
| request_weight | memory-bound | throughput | pilot descriptive | 3 |
| guaranteed_vs_burstable | cpu-bound | throughput | pilot descriptive | 3 |
| guaranteed_vs_burstable | hybrid | latency_p99_ms | pilot descriptive | 3 |
| guaranteed_vs_burstable | io-bound | latency_p99_ms | pilot descriptive | 3 |
| guaranteed_vs_burstable | memory-bound | throughput | pilot descriptive | 3 |
| throttle_gap | cpu-bound | throttled_period_ratio | incomplete | 0 |
| throttle_gap | hybrid | throttled_period_ratio | incomplete | 1 |
| throttle_gap | io-bound | throttled_period_ratio | incomplete | 0 |
| throttle_gap | memory-bound | throttled_period_ratio | incomplete | 0 |

## Paired pilot descriptive estimates and 95% intervals

Data: `build/research/cpu-limits/pilot/{blocks,cgroups,metrics}.csv`; 3 blocks and 108 complete cells.

These intervals describe the three pilot blocks. They do not make a confirmatory result.

| family | workload | metric | pilot label | estimate | 95% interval | paired blocks |
|---|---|---|---|---:|---|---:|
| guaranteed_vs_burstable | cpu-bound | throughput | pilot descriptive | 1.014951 | 0.963814 to 1.068801 | 3 |
| guaranteed_vs_burstable | hybrid | latency_p99_ms | pilot descriptive | 0.913008 | 0.574776 to 1.450274 | 3 |
| guaranteed_vs_burstable | io-bound | latency_p99_ms | pilot descriptive | 1.082746 | 0.747127 to 1.569129 | 3 |
| guaranteed_vs_burstable | memory-bound | throughput | pilot descriptive | 0.995609 | 0.956067 to 1.036786 | 3 |
| limit_effect | cpu-bound | throughput | pilot descriptive | 0.269480 | 0.257453 to 0.282069 | 3 |
| limit_effect | hybrid | latency_p99_ms | pilot descriptive | 1.036768 | 1.023807 to 1.049893 | 3 |
| limit_effect | io-bound | latency_p99_ms | pilot descriptive | 1.089449 | 0.856397 to 1.385921 | 3 |
| limit_effect | memory-bound | throughput | pilot descriptive | 0.506762 | 0.502751 to 0.510805 | 3 |
| request_weight | cpu-bound | throughput | pilot descriptive | 0.991223 | 0.924106 to 1.063214 | 3 |
| request_weight | hybrid | latency_p99_ms | pilot descriptive | 1.145336 | 0.708746 to 1.850866 | 3 |
| request_weight | io-bound | latency_p99_ms | pilot descriptive | 0.956800 | 0.649117 to 1.410327 | 3 |
| request_weight | memory-bound | throughput | pilot descriptive | 1.012153 | 0.968860 to 1.057381 | 3 |
| throttle_gap | cpu-bound | throttled_period_ratio | incomplete | not available | not available to not available | 0 |
| throttle_gap | hybrid | throttled_period_ratio | incomplete | not available | not available to not available | 1 |
| throttle_gap | io-bound | throttled_period_ratio | incomplete | not available | not available to not available | 0 |
| throttle_gap | memory-bound | throttled_period_ratio | incomplete | not available | not available to not available | 0 |

## CPU use and throttling by workload and policy

Data: `build/research/cpu-limits/pilot/{blocks,cgroups,metrics}.csv`; 3 blocks and 108 complete cells.

| workload | policy | condition | mean CPU seconds | defined throttled ratios | no-quota zero-period cases | mean throttled ratio | mean throttled seconds |
|---|---|---|---:|---:|---:|---:|---:|
| cpu-bound | best-effort | solo | 38.045645 | 0/3 | 3 | not available | 0.000000 |
| cpu-bound | burstable-low | contention | 10.064504 | 3/3 | 0 | 0.993367 | 4.636133 |
| cpu-bound | burstable-low | solo | 10.088176 | 3/3 | 0 | 1.000000 | 17.154221 |
| cpu-bound | fit | contention | 38.172935 | 3/3 | 0 | 0.000000 | 0.000000 |
| cpu-bound | fit | solo | 38.099230 | 3/3 | 0 | 0.000000 | 0.000000 |
| cpu-bound | guaranteed-low | contention | 10.350576 | 3/3 | 0 | 1.000000 | 16.274368 |
| cpu-bound | request-high | contention | 38.088362 | 0/3 | 3 | not available | 0.000000 |
| cpu-bound | request-low | contention | 37.912079 | 0/3 | 3 | not available | 0.000000 |
| cpu-bound | request-low | solo | 38.687017 | 0/3 | 3 | not available | 0.000000 |
| hybrid | best-effort | solo | 0.346452 | 0/3 | 3 | not available | 0.000000 |
| hybrid | burstable-low | contention | 6.986209 | 3/3 | 0 | 0.666667 | 10.445798 |
| hybrid | burstable-low | solo | 6.714185 | 3/3 | 0 | 0.661741 | 5.916755 |
| hybrid | fit | contention | 13.655000 | 3/3 | 0 | 0.666667 | 6.426204 |
| hybrid | fit | solo | 13.567935 | 3/3 | 0 | 0.645528 | 5.624885 |
| hybrid | guaranteed-low | contention | 3.515360 | 3/3 | 0 | 0.333333 | 4.207652 |
| hybrid | request-high | contention | 13.180021 | 0/3 | 3 | not available | 0.000000 |
| hybrid | request-low | contention | 0.345855 | 0/3 | 3 | not available | 0.000000 |
| hybrid | request-low | solo | 0.344515 | 0/3 | 3 | not available | 0.000000 |
| io-bound | best-effort | solo | 0.292682 | 0/3 | 3 | not available | 0.000000 |
| io-bound | burstable-low | contention | 0.304083 | 3/3 | 0 | 0.000000 | 0.000000 |
| io-bound | burstable-low | solo | 0.296234 | 3/3 | 0 | 0.000000 | 0.000000 |
| io-bound | fit | contention | 0.292559 | 3/3 | 0 | 0.000000 | 0.000000 |
| io-bound | fit | solo | 0.304712 | 3/3 | 0 | 0.000000 | 0.000000 |
| io-bound | guaranteed-low | contention | 0.295349 | 3/3 | 0 | 0.000000 | 0.000000 |
| io-bound | request-high | contention | 0.301392 | 0/3 | 3 | not available | 0.000000 |
| io-bound | request-low | contention | 0.302362 | 0/3 | 3 | not available | 0.000000 |
| io-bound | request-low | solo | 0.297355 | 0/3 | 3 | not available | 0.000000 |
| memory-bound | best-effort | solo | 19.075875 | 0/3 | 3 | not available | 0.000000 |
| memory-bound | burstable-low | contention | 10.064842 | 3/3 | 0 | 0.729397 | 2.433412 |
| memory-bound | burstable-low | solo | 9.992582 | 3/3 | 0 | 1.000000 | 7.691704 |
| memory-bound | fit | contention | 18.917089 | 3/3 | 0 | 0.000000 | 0.000000 |
| memory-bound | fit | solo | 18.765134 | 3/3 | 0 | 0.000000 | 0.000000 |
| memory-bound | guaranteed-low | contention | 10.378603 | 3/3 | 0 | 0.839727 | 4.031822 |
| memory-bound | request-high | contention | 18.931865 | 0/3 | 3 | not available | 0.000000 |
| memory-bound | request-low | contention | 18.782340 | 0/3 | 3 | not available | 0.000000 |
| memory-bound | request-low | solo | 18.813752 | 0/3 | 3 | not available | 0.000000 |

## Application outcomes by workload and policy

Data: `build/research/cpu-limits/pilot/{blocks,cgroups,metrics}.csv`; 3 blocks and 108 complete cells.

| workload | policy | condition | CPU bogo ops/s | memory bogo ops/s | I/O p99 ms | I/O req/s | hybrid p99 ms | hybrid fps |
|---|---|---|---:|---:|---:|---:|---:|---:|
| cpu-bound | best-effort | solo | 463.010000 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | burstable-low | contention | 114.366667 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | burstable-low | solo | 121.790000 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | fit | contention | 447.410000 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | fit | solo | 451.926667 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | guaranteed-low | contention | 116.073333 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | request-high | contention | 447.070000 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | request-low | contention | 443.230000 | not applicable | not applicable | not applicable | not applicable | not applicable |
| cpu-bound | request-low | solo | 465.493333 | not applicable | not applicable | not applicable | not applicable | not applicable |
| hybrid | best-effort | solo | not applicable | not applicable | not applicable | not applicable | 2.415667 | 9235.666667 |
| hybrid | burstable-low | contention | not applicable | not applicable | not applicable | not applicable | 2.487667 | 1940.000000 |
| hybrid | burstable-low | solo | not applicable | not applicable | not applicable | not applicable | 2.399000 | 1951.666667 |
| hybrid | fit | contention | not applicable | not applicable | not applicable | not applicable | 3.168333 | 3850.333333 |
| hybrid | fit | solo | not applicable | not applicable | not applicable | not applicable | 2.487000 | 3931.000000 |
| hybrid | guaranteed-low | contention | not applicable | not applicable | not applicable | not applicable | 2.745000 | 1952.333333 |
| hybrid | request-high | contention | not applicable | not applicable | not applicable | not applicable | 3.415667 | 7497.000000 |
| hybrid | request-low | contention | not applicable | not applicable | not applicable | not applicable | 2.973667 | 7475.333333 |
| hybrid | request-low | solo | not applicable | not applicable | not applicable | not applicable | 2.399667 | 9189.333333 |
| io-bound | best-effort | solo | not applicable | not applicable | 2.001667 | 400.093333 | not applicable | not applicable |
| io-bound | burstable-low | contention | not applicable | not applicable | 2.342333 | 400.100000 | not applicable | not applicable |
| io-bound | burstable-low | solo | not applicable | not applicable | 2.077000 | 400.103333 | not applicable | not applicable |
| io-bound | fit | contention | not applicable | not applicable | 2.234333 | 400.110000 | not applicable | not applicable |
| io-bound | fit | solo | not applicable | not applicable | 2.264333 | 400.063333 | not applicable | not applicable |
| io-bound | guaranteed-low | contention | not applicable | not applicable | 2.176333 | 400.096667 | not applicable | not applicable |
| io-bound | request-high | contention | not applicable | not applicable | 2.122667 | 400.103333 | not applicable | not applicable |
| io-bound | request-low | contention | not applicable | not applicable | 2.209333 | 400.103333 | not applicable | not applicable |
| io-bound | request-low | solo | not applicable | not applicable | 2.020667 | 400.103333 | not applicable | not applicable |
| memory-bound | best-effort | solo | not applicable | 6.486667 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | burstable-low | contention | not applicable | 3.026667 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | burstable-low | solo | not applicable | 3.246667 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | fit | contention | not applicable | 6.086667 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | fit | solo | not applicable | 6.406667 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | guaranteed-low | contention | not applicable | 3.013333 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | request-high | contention | not applicable | 6.040000 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | request-low | contention | not applicable | 6.113333 | not applicable | not applicable | not applicable | not applicable |
| memory-bound | request-low | solo | not applicable | 6.423333 | not applicable | not applicable | not applicable | not applicable |

## Cgroup mapping

Data: `build/research/cpu-limits/pilot/{blocks,cgroups,metrics}.csv`; 3 blocks and 108 complete cells.

Configured requests map to measured `cpu.weight`; configured limits map to measured `cpu.max` quota and period.

| workload | policy | request (m) | limit (m) | pod `cpu.weight` | pod `cpu.max` | container `cpu.weight` | container `cpu.max` |
|---|---|---:|---:|---:|---|---:|---|
| cpu-bound | best-effort | none | none | 1 | max 100000 | 1 | max 100000 |
| cpu-bound | burstable-low | 100 | 500 | 4 | 50000 100000 | 17 | 50000 100000 |
| cpu-bound | fit | 2000 | 2000 | 79 | 200000 100000 | 174 | 200000 100000 |
| cpu-bound | guaranteed-low | 500 | 500 | 20 | 50000 100000 | 59 | 50000 100000 |
| cpu-bound | request-high | 2000 | none | 79 | max 100000 | 174 | max 100000 |
| cpu-bound | request-low | 100 | none | 4 | max 100000 | 17 | max 100000 |
| hybrid | best-effort | none | none | 1 | max 100000 | 1 | max 100000 |
| hybrid | burstable-low | 100 | 500 | 8 | 100000 100000 | 17 | 50000 100000 |
| hybrid | fit | 1000 | 1000 | 79 | 200000 100000 | 100 | 100000 100000 |
| hybrid | guaranteed-low | 500 | 500 | 39 | 100000 100000 | 59 | 50000 100000 |
| hybrid | request-high | 1000 | none | 79 | max 100000 | 100 | max 100000 |
| hybrid | request-low | 100 | none | 8 | max 100000 | 17 | max 100000 |
| io-bound | best-effort | none | none | 1 | max 100000 | 1 | max 100000 |
| io-bound | burstable-low | 100 | 250 | 4 | 25000 100000 | 17 | 25000 100000 |
| io-bound | fit | 1000 | 1000 | 39 | 100000 100000 | 100 | 100000 100000 |
| io-bound | guaranteed-low | 250 | 250 | 10 | 25000 100000 | 35 | 25000 100000 |
| io-bound | request-high | 1000 | none | 39 | max 100000 | 100 | max 100000 |
| io-bound | request-low | 100 | none | 4 | max 100000 | 17 | max 100000 |
| memory-bound | best-effort | none | none | 1 | max 100000 | 1 | max 100000 |
| memory-bound | burstable-low | 100 | 500 | 4 | 50000 100000 | 17 | 50000 100000 |
| memory-bound | fit | 1000 | 1000 | 39 | 100000 100000 | 100 | 100000 100000 |
| memory-bound | guaranteed-low | 500 | 500 | 20 | 50000 100000 | 59 | 50000 100000 |
| memory-bound | request-high | 1000 | none | 39 | max 100000 | 100 | max 100000 |
| memory-bound | request-low | 100 | none | 4 | max 100000 | 17 | max 100000 |

## Figures

- `cgroup-parameter-map.svg`
- `cpu-quota-scatter.svg`
- `latency-cdf.svg`
- `performance-heatmap.svg`
- `runqueue-histogram.svg`
- `throttling-bars.svg`
- `timeline.svg`

## Limitations

- Results apply to the recorded worker, workload images, measurement windows, and complete pilot rows only.
- Pilot estimates do not make a confirmatory result.
- Incomplete cells and missing contrasts remain incomplete; this report does not fill them.
- Interpret focused cgroup-mode, base-slice, and sched_ext results only when their figures and gate evidence are present.

## Calibration outcome and campaign status

Data: `research/cpu-limits/FREEZE.md` and
`build/research/cpu-limits/policies.json`; 3 blocks and 108 complete cells.

The status is **pilot descriptive, not confirmatory**. The policy calibration
failed because no low policy met the 10% to 40% throttled-period target for
`cpu-bound`, `io-bound`, and `hybrid`. The pre-pilot defaults remain recorded
only to identify the failed calibration. They are not frozen policies. The
confirmatory campaign did not run.

## Trace evidence

Data: `build/research/cpu-limits/pilot/cells/*/*/trace/raw/{sched-trace,throttle}.csv`;
3 blocks and 108 complete cells, with traces in 4 selected cells per block.

This is pilot-descriptive trace evidence, not a confirmatory finding. The
trace parser matched target workload PIDs and produced 34,898 trace rows across
the four trace cells: `solo-cpu-bound-fit`,
`solo-cpu-bound-burstable-low`, `solo-io-bound-fit`, and
`solo-io-bound-burstable-low`. The target-task run-queue delay was
p50 = 0.003 ms and p99 = 0.087 ms in these pilot slices. The run-queue
histogram and on-CPU/off-CPU timeline figures are generated from those rows.
The trace slice is five seconds inside the measured window. It is not a
whole-window sample and applies only to the recorded cells and worker.

## Figure references

- [Performance heatmap](figures/performance-heatmap.svg)
- [CPU usage versus quota scatter](figures/cpu-quota-scatter.svg)
- [Throttling bars](figures/throttling-bars.svg)
- [Latency CDF](figures/latency-cdf.svg)
- [Cgroup parameter map](figures/cgroup-parameter-map.svg)
- [Run-queue histogram](figures/runqueue-histogram.svg)
- [On-CPU and off-CPU timeline](figures/timeline.svg)

## Limitations

- Calibration failed, so the confirmatory campaign did not run.
- The report has three blocks, 108 cells, and one worker only.
- The numerical trace result is pilot descriptive. It uses the recorded
  `trace-rows.json` artifact, not the campaign CSV inputs.
- The trace slice covers selected cells only. It does not describe a full
  measurement window or every workload policy.
- No sched_ext measurement was run.
