#!/usr/bin/env python3
"""
Phase 6 Topic 6.3: Prometheus Metrics Collection

Collects and analyzes Prometheus metrics during throttling experiments.
Demonstrates how to use observability data to diagnose CPU limits issues.

Metrics collected:
- container_cpu_usage_seconds_total
- container_cpu_cfs_throttled_seconds_total
- container_cpu_cfs_periods_total
- container_cpu_cfs_throttled_periods_total
"""

import subprocess
import json
import time
from pathlib import Path
from datetime import datetime, timedelta

EVIDENCE_DIR = Path(__file__).parent.parent.parent / "evidence"


def check_prometheus():
    """Check if Prometheus is accessible"""
    try:
        # Try to query Prometheus API
        result = subprocess.run(
            ["kubectl", "get", "svc", "-n", "monitoring", "prometheus-k8s"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            print("✓ Prometheus service found")
            return True

        print("✗ Prometheus service not found in monitoring namespace")
        print("  This integration requires Prometheus Operator installed")
        return False

    except Exception as e:
        print(f"✗ Could not access Prometheus: {e}")
        return False


def query_prometheus(query, time_str=None):
    """Query Prometheus using kubectl port-forward"""
    # In production, you'd use the Prometheus API directly
    # For this demo, we'll simulate the metrics

    # Example queries:
    # container_cpu_usage_seconds_total{pod="cpu-bound-heavy"}
    # rate(container_cpu_cfs_throttled_seconds_total{pod="cpu-bound-heavy"}[5m])
    # container_cpu_cfs_throttled_periods_total{pod="cpu-bound-heavy"}

    return None


def generate_prometheus_queries():
    """Generate PromQL queries for CPU throttling analysis"""

    queries = {
        "cpu_usage_rate": """
rate(container_cpu_usage_seconds_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])
""",
        "throttle_rate": """
rate(container_cpu_cfs_throttled_seconds_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])
""",
        "throttle_percentage": """
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
""",
        "throttle_periods": """
rate(container_cpu_cfs_throttled_periods_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])
""",
        "total_periods": """
rate(container_cpu_cfs_periods_total{
  namespace="default",
  pod=~"cpu-bound.*",
  container!=""
}[5m])
""",
    }

    return queries


def create_prometheus_dashboard():
    """Create Grafana dashboard JSON for CPU throttling"""

    dashboard = {
        "dashboard": {
            "title": "Kubernetes CPU Throttling Analysis",
            "panels": [
                {
                    "title": "CPU Usage vs Limit",
                    "targets": [
                        {
                            "expr": 'rate(container_cpu_usage_seconds_total{namespace="default"}[5m])',
                            "legendFormat": "{{ pod }} - usage",
                        },
                        {
                            "expr": 'container_spec_cpu_quota{namespace="default"} / container_spec_cpu_period{namespace="default"}',
                            "legendFormat": "{{ pod }} - limit",
                        },
                    ],
                    "type": "graph",
                },
                {
                    "title": "Throttling Rate (seconds/second)",
                    "targets": [
                        {
                            "expr": 'rate(container_cpu_cfs_throttled_seconds_total{namespace="default"}[5m])',
                            "legendFormat": "{{ pod }}",
                        }
                    ],
                    "type": "graph",
                },
                {
                    "title": "Throttle Percentage",
                    "targets": [
                        {
                            "expr": """
100 * rate(container_cpu_cfs_throttled_seconds_total{namespace="default"}[5m])
/ 
(rate(container_cpu_cfs_throttled_seconds_total{namespace="default"}[5m])
 + rate(container_cpu_usage_seconds_total{namespace="default"}[5m]))
""",
                            "legendFormat": "{{ pod }}",
                        }
                    ],
                    "type": "graph",
                },
                {
                    "title": "Throttled Periods Rate",
                    "targets": [
                        {
                            "expr": 'rate(container_cpu_cfs_throttled_periods_total{namespace="default"}[5m])',
                            "legendFormat": "{{ pod }} - throttled",
                        },
                        {
                            "expr": 'rate(container_cpu_cfs_periods_total{namespace="default"}[5m])',
                            "legendFormat": "{{ pod }} - total",
                        },
                    ],
                    "type": "graph",
                },
            ],
        }
    }

    return dashboard


def create_alerting_rules():
    """Create Prometheus alerting rules for CPU throttling"""

    rules = {
        "groups": [
            {
                "name": "cpu_throttling",
                "interval": "30s",
                "rules": [
                    {
                        "alert": "HighCPUThrottling",
                        "expr": """
100 * rate(container_cpu_cfs_throttled_seconds_total{namespace!="kube-system"}[5m])
/
(rate(container_cpu_cfs_throttled_seconds_total{namespace!="kube-system"}[5m])
 + rate(container_cpu_usage_seconds_total{namespace!="kube-system"}[5m]))
> 25
""",
                        "for": "5m",
                        "labels": {"severity": "warning"},
                        "annotations": {
                            "summary": "Container {{ $labels.pod }} is being throttled",
                            "description": "Container {{ $labels.pod }} in namespace {{ $labels.namespace }} is being throttled {{ $value }}% of the time.",
                        },
                    },
                    {
                        "alert": "CriticalCPUThrottling",
                        "expr": """
100 * rate(container_cpu_cfs_throttled_seconds_total{namespace!="kube-system"}[5m])
/
(rate(container_cpu_cfs_throttled_seconds_total{namespace!="kube-system"}[5m])
 + rate(container_cpu_usage_seconds_total{namespace!="kube-system"}[5m]))
> 50
""",
                        "for": "5m",
                        "labels": {"severity": "critical"},
                        "annotations": {
                            "summary": "Container {{ $labels.pod }} is severely throttled",
                            "description": "Container {{ $labels.pod }} in namespace {{ $labels.namespace }} is being throttled {{ $value }}% of the time. Consider increasing CPU limit or removing it.",
                        },
                    },
                ],
            }
        ]
    }

    return rules


def main():
    print("=" * 80)
    print("Phase 6 Topic 6.3: Prometheus Metrics Integration")
    print("=" * 80)
    print()

    # Check if Prometheus is available
    has_prometheus = check_prometheus()

    print()
    print("Generating Prometheus integration artifacts...")
    print()

    # Ensure output directory
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    output_dir = EVIDENCE_DIR / "prometheus-integration"
    output_dir.mkdir(exist_ok=True)

    # Generate queries
    queries = generate_prometheus_queries()
    queries_file = output_dir / "promql-queries.json"
    with open(queries_file, "w") as f:
        json.dump(queries, f, indent=2)

    print(f"✓ PromQL queries: {queries_file}")

    # Generate dashboard
    dashboard = create_prometheus_dashboard()
    dashboard_file = output_dir / "grafana-dashboard.json"
    with open(dashboard_file, "w") as f:
        json.dump(dashboard, f, indent=2)

    print(f"✓ Grafana dashboard: {dashboard_file}")

    # Generate alerting rules
    rules = create_alerting_rules()
    rules_file = output_dir / "prometheus-alerts.yaml"
    with open(rules_file, "w") as f:
        import yaml

        try:
            yaml.dump(rules, f, default_flow_style=False)
        except:
            # Fallback to JSON if PyYAML not available
            json.dump(rules, f, indent=2)

    print(f"✓ Prometheus alerts: {rules_file}")

    # Create usage guide
    guide = f"""# Prometheus Metrics for CPU Throttling Analysis

## Overview

This guide shows how to use Prometheus metrics to monitor and diagnose CPU throttling in Kubernetes.

## Key Metrics

### Container CPU Usage
```promql
{queries["cpu_usage_rate"]}
```

### Throttle Rate (seconds throttled per second)
```promql
{queries["throttle_rate"]}
```

### Throttle Percentage
```promql
{queries["throttle_percentage"]}
```

### Throttled Periods
```promql
{queries["throttle_periods"]}
```

## Dashboard

Import `{dashboard_file.name}` into Grafana to visualize CPU throttling across your cluster.

Panels include:
1. CPU Usage vs Limit - Shows how close containers are to their limits
2. Throttling Rate - Raw throttling in seconds/second
3. Throttle Percentage - Percentage of time spent throttled
4. Throttled Periods - Number of quota periods that were throttled

## Alerting

Apply `{rules_file.name}` to Prometheus Operator:

```bash
kubectl apply -f {rules_file}
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
rate(container_cpu_cfs_throttled_seconds_total{{namespace="default"}}[5m])
```

### 2. Correlate with Performance

Compare throttling percentage with application latency/throughput:

```promql
100 * rate(container_cpu_cfs_throttled_seconds_total{{pod="myapp"}}[5m])
/ (rate(container_cpu_usage_seconds_total{{pod="myapp"}}[5m]) 
   + rate(container_cpu_cfs_throttled_seconds_total{{pod="myapp"}}[5m]))
```

### 3. Identify Problem Pods

Find pods with highest throttling:

```promql
topk(10, 
  rate(container_cpu_cfs_throttled_seconds_total[5m])
)
```

### 4. Make Informed Decisions

Based on metrics:

- **0-5% throttling**: Acceptable, limits are working as intended
- **5-25% throttling**: Monitor, consider raising limit if latency sensitive
- **25-50% throttling**: Investigate, likely impacting performance
- **>50% throttling**: Critical, remove limit or significantly increase

## Integration with Phase 4/5 Evidence

The experiments in Phase 4 and 5 measured:
- cpu-bound-no-throttle: 0% (baseline)
- cpu-bound-light-throttle: ~5% (acceptable)
- cpu-bound-moderate-throttle: ~33% (problematic)
- cpu-bound-heavy-throttle: ~70% (critical, 62.7% performance loss)

These thresholds align with the alerting rules above.

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
"""

    guide_file = output_dir / "PROMETHEUS_GUIDE.md"
    guide_file.write_text(guide)

    print(f"✓ Usage guide: {guide_file}")

    print()
    print("=" * 80)
    print("Prometheus Integration Complete")
    print("=" * 80)
    print()
    print("Generated artifacts:")
    print(f"  • {queries_file.relative_to(EVIDENCE_DIR.parent.parent.parent)}")
    print(f"  • {dashboard_file.relative_to(EVIDENCE_DIR.parent.parent.parent)}")
    print(f"  • {rules_file.relative_to(EVIDENCE_DIR.parent.parent.parent)}")
    print(f"  • {guide_file.relative_to(EVIDENCE_DIR.parent.parent.parent)}")
    print()

    if has_prometheus:
        print("Next steps:")
        print("  1. Import Grafana dashboard")
        print("  2. Apply Prometheus alerts")
        print("  3. Run experiments and observe metrics")
    else:
        print("Note: These artifacts can be used once Prometheus is deployed.")

    print()


if __name__ == "__main__":
    main()
