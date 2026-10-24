# CPU limits metrics

This directory contains optional Prometheus and Grafana configuration for the
CPU limits collector. The campaign runner stores each one-second scrape with
its cell data. A live Prometheus server is not required for offline analysis.

## Collector target

Apply `../observers/collector-daemonset.yaml` only after the live gate allows
cluster access. The collector listens on port 9100 and exposes `/metrics`.
`prometheus.yml` discovers collector Pods in the `cpu-limits` namespace and
keeps only the collector container on port 9100.

The collector must expose these metric families:

- CPU use and CFS period counters
- CFS throttled-period and throttled-time counters
- CPU pressure waiting time
- cgroup CPU weight and quota values

The Grafana dashboard shows CPU use against quota, throttled-period ratio, CPU
PSI, and p99 latency when a workload exports a latency histogram. The latency
panel is empty when no histogram is available. It does not treat missing latency
as a zero value.

## Import

1. Add the Prometheus scrape job from `prometheus.yml`.
2. Import `grafana-dashboard.json` into Grafana.
3. Select the Prometheus data source when Grafana asks for `DS_PROMETHEUS`.
4. Check that the collector target is healthy before starting a measurement.

## Metric labels

Target cgroup metrics use bounded labels from the frozen design: `node`,
`pod_uid`, `container`, `level`, and `cell_id`. Do not add request IDs, full
cgroup paths, or free text as metric labels. Those values make unbounded time
series.

## Offline validation

Run this command from the repository root:

```bash
bash research/cpu-limits/observers/apply.sh test
```

The command parses the manifest. It does not contact a Kubernetes cluster.
