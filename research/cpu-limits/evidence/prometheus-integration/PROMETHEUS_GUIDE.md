# Prometheus Metrics for CPU Throttling Analysis

## Overview

This guide shows how to use Prometheus metrics to monitor and diagnose CPU throttling in Kubernetes.

## Key Metrics

### Container CPU Usage
```promql

rate(container_cpu_usage_seconds_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])

```

### Throttle Rate (seconds throttled per second)
```promql

rate(container_cpu_cfs_throttled_seconds_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])

```

### Throttle Percentage
```promql

100 * rate(container_cpu_cfs_throttled_seconds_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])
/ 
(
  rate(container_cpu_cfs_throttled_seconds_total{
    namespace="default",
    pod=~"cpu-bound.*",
    container!=""
  }[5m])
  + 
  rate(container_cpu_usage_seconds_total{
    namespace="default",
    pod=~"cpu-bound.*",
    container!=""
  }[5m])
)

```

### Throttled Periods
```promql

rate(container_cpu_cfs_throttled_periods_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])

```

## Dashboard

Import `grafana-dashboard.json` into Grafana to visualize CPU throttling across your cluster.

Panels include:
1. CPU Usage vs Limit - Shows how close containers are to their limits
2. Throttling Rate - Raw throttling in seconds/second
3. Throttle Percentage - Percentage of time spent throttled
4. Throttled Periods - Number of quota periods that were throttled

## Alerting

Apply `prometheus-alerts.yaml` to Prometheus Operator:

```bash
kubectl apply -f /home/eryoma/workspace/blog/k8labs/research/cpu-limits/evidence/prometheus-integration/prometheus-alerts.yaml
```

Alerts:
- **HighCPUThrottling**: Fires when throttling exceeds 25% for 5 minutes
- **CriticalCPUThrottling**: Fires when throttling exceeds 50% for 5 minutes

## Analysis Workflow

### 1. Check Current Throttling

```bash
kubectl port-forward -n monitoring svc/prometheus-k8s 9090:9090
```

Then open http://localhost:9090 and run:
```promql
rate(container_cpu_cfs_throttled_seconds_total{namespace="default"}[5m])
```

### 2. Correlate with Performance

Compare throttling percentage with application latency/throughput:

```promql
100 * rate(container_cpu_cfs_throttled_seconds_total{pod="myapp"}[5m])
/ (rate(container_cpu_usage_seconds_total{pod="myapp"}[5m]) 
   + rate(container_cpu_cfs_throttled_seconds_total{pod="myapp"}[5m]))
```

### 3. Identify Problem Pods

Find pods with highest throttling:

```promql
topk(10, 
  rate(container_cpu_cfs_throttled_seconds_total[5m])
)
```

### 4. Make Informed Decisions

Throttle percentage tells you a quota binds. It does not tell you whether it
hurts, and it does not rank configurations. Phase 7.1 measured a pod throttled
in 75.1% of periods with p99 98.87 ms next to one throttled in 95.6% of periods
with p99 67.76 ms.

Working guidance:

- **Any sustained throttling on a service with a latency SLO**: investigate.
  Remove the limit first and re-measure p99; Phase 7.1 measured p99 falling from
  69.43 ms to 13.98 ms when the limit was removed.
- **High throttling on batch throughput work**: check whether the limit equals
  the request. If it does not, the gap is pure throughput loss (Phase 4: 86.2%
  loss going from a 2000m/4000m quota to 100m/500m).
- **Zero throttling everywhere**: your limits are not binding. Check requests
  against actual P95 usage instead.

## Integration with Phase 4/5 Evidence

Phase 4 measured throughput, not throttle percentages. The configurations and
their results (`phase4/evidence/throttling-summary.json`) are:

- cpu-bound-no-throttle: 139.05 ops/sec (baseline)
- cpu-bound-light-throttle: 108.78 ops/sec (-21.8%)
- cpu-bound-moderate-throttle: 53.57 ops/sec (-61.5%)
- cpu-bound-heavy-throttle: 19.22 ops/sec (-86.2%)

The thresholds in the alerting rules are operational defaults, not boundaries
derived from these measurements.

## Next Steps

1. Deploy Prometheus + Grafana if not present
2. Import the dashboard
3. Apply the alerting rules
4. Monitor your workloads for a week
5. Adjust limits based on actual throttling vs SLOs

## References

- [Prometheus cAdvisor Metrics](https://github.com/google/cadvisor/blob/master/docs/storage/prometheus.md)
- [Kubernetes Monitoring with Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config)
- [Grafana Kubernetes App](https://grafana.com/grafana/plugins/grafana-kubernetes-app/)
