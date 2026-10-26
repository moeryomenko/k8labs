#!/bin/bash
# Deploy Prometheus monitoring stack for CPU limits research

set -euo pipefail

MONITORING_NS="monitoring"
PHASE4_DIR="research/cpu-limits/phase4"

echo "Deploying Prometheus monitoring stack..."

# Create namespace
kubectl create namespace ${MONITORING_NS} --dry-run=client -o yaml | kubectl apply -f -

# Deploy kube-prometheus-stack (includes Prometheus, Grafana, kube-state-metrics)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace ${MONITORING_NS} \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set grafana.adminPassword=admin \
  --set prometheus.prometheusSpec.retention=7d \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=10Gi \
  --wait

echo "✓ Prometheus stack deployed"

# Wait for Prometheus to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=prometheus -n ${MONITORING_NS} --timeout=300s

# Wait for Grafana to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=grafana -n ${MONITORING_NS} --timeout=300s

echo ""
echo "================================================================"
echo " Prometheus Monitoring Stack Deployed"
echo "================================================================"
echo ""
echo "Access Prometheus:"
echo "  kubectl port-forward -n ${MONITORING_NS} svc/kube-prometheus-stack-prometheus 9090:9090"
echo "  URL: http://localhost:9090"
echo ""
echo "Access Grafana:"
echo "  kubectl port-forward -n ${MONITORING_NS} svc/kube-prometheus-stack-grafana 3000:80"
echo "  URL: http://localhost:3000"
echo "  Username: admin"
echo "  Password: admin"
echo ""
echo "Key PromQL queries for CPU limits research:"
echo ""
echo "# CPU usage rate"
echo "rate(container_cpu_usage_seconds_total[5m])"
echo ""
echo "# CPU throttling rate"
echo "rate(container_cpu_cfs_throttled_seconds_total[5m])"
echo ""
echo "# CPU request utilization"
echo "rate(container_cpu_usage_seconds_total[5m]) /"
echo "  on(namespace,pod,container)"
echo "  kube_pod_container_resource_requests{resource=\"cpu\"}"
echo ""
echo "# CPU limit utilization"
echo "rate(container_cpu_usage_seconds_total[5m]) /"
echo "  on(namespace,pod,container)"
echo "  kube_pod_container_resource_limits{resource=\"cpu\"}"
echo ""
echo "# Throttling percentage"
echo "rate(container_cpu_cfs_throttled_seconds_total[5m]) /"
echo "  rate(container_cpu_cfs_periods_total[5m]) * 100"
echo ""
echo "================================================================"
