#!/bin/bash
# Run nginx latency experiments with CPU throttling
# Measures P50/P95/P99 latency under different throttling levels

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="$SCRIPT_DIR/evidence"
MANIFESTS_DIR="$SCRIPT_DIR/manifests"
WRK_SCRIPTS_DIR="$SCRIPT_DIR/wrk-scripts"

# Experiment configuration
WRK_THREADS=4
WRK_CONNECTIONS=100
WRK_DURATION=60s
NAMESPACE="default"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

mkdir -p "$EVIDENCE_DIR"

echo "================================================================================"
echo "Topic 6.1: Real-World Latency Testing (nginx + wrk)"
echo "================================================================================"
echo ""
echo "Configuration:"
echo "  wrk threads: $WRK_THREADS"
echo "  wrk connections: $WRK_CONNECTIONS"
echo "  wrk duration: $WRK_DURATION"
echo "  namespace: $NAMESPACE"
echo ""

# Test configurations
declare -A configs
configs[baseline]="0%"
configs[light]="~5%"
configs[moderate]="~50%"
configs[heavy]="~70%"

# Function to wait for pod
wait_for_pod() {
    local pod_name=$1
    local timeout=60
    echo "  Waiting for pod $pod_name to be ready..."
    if kubectl wait --for=condition=ready pod/"$pod_name" -n "$NAMESPACE" --timeout="${timeout}s" &> /dev/null; then
        echo -e "  ${GREEN}✓${NC} Pod $pod_name ready"
        return 0
    else
        echo -e "  ${RED}✗${NC} Pod $pod_name did not become ready within ${timeout}s"
        return 1
    fi
}

# Function to run wrk and collect metrics
run_wrk_test() {
    local config_name=$1
    local nginx_pod=$2
    local service_name="nginx-service"
    local timestamp=$(date -u +"%Y%m%dT%H%M%SZ")
    local evidence_file="$EVIDENCE_DIR/latency-${config_name}-${timestamp}.json"
    
    echo "  Running wrk load test..."
    
    # Get service IP
    local service_ip=$(kubectl get svc "$service_name" -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
    if [ -z "$service_ip" ]; then
        echo -e "  ${RED}✗${NC} Could not get service IP"
        return 1
    fi
    echo "  Service IP: $service_ip"
    
    # Run wrk from a job
    local wrk_output=$(mktemp)
    kubectl run wrk-test-"$config_name" \
        --image=williamyeh/wrk \
        --restart=Never \
        --rm \
        -i \
        -n "$NAMESPACE" \
        --command -- \
        wrk -t "$WRK_THREADS" -c "$WRK_CONNECTIONS" -d "$WRK_DURATION" \
        --latency "http://${service_ip}/" 2>&1 | tee "$wrk_output" || true
    
    # Parse wrk output
    local latency_p50=$(grep "50.000%" "$wrk_output" | awk '{print $2}' || echo "0")
    local latency_p75=$(grep "75.000%" "$wrk_output" | awk '{print $2}' || echo "0")
    local latency_p90=$(grep "90.000%" "$wrk_output" | awk '{print $2}' || echo "0")
    local latency_p99=$(grep "99.000%" "$wrk_output" | awk '{print $2}' || echo "0")
    local latency_p999=$(grep "99.900%" "$wrk_output" | awk '{print $2}' || echo "0")
    local requests_per_sec=$(grep "Requests/sec:" "$wrk_output" | awk '{print $2}' || echo "0")
    
    # Get CPU throttling stats
    local cpu_stat=$(kubectl exec "$nginx_pod" -n "$NAMESPACE" -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null || echo "")
    local nr_periods=$(echo "$cpu_stat" | grep "nr_periods" | awk '{print $2}' || echo "0")
    local nr_throttled=$(echo "$cpu_stat" | grep "nr_throttled" | awk '{print $2}' || echo "0")
    local throttled_time=$(echo "$cpu_stat" | grep "throttled_usec" | awk '{print $2}' || echo "0")
    
    local throttle_percentage=0
    if [ "$nr_periods" -gt 0 ]; then
        throttle_percentage=$(awk "BEGIN {printf \"%.1f\", ($nr_throttled / $nr_periods) * 100}")
    fi
    
    # Create evidence JSON
    cat > "$evidence_file" << EOF
{
  "experiment": "latency-testing",
  "config": "$config_name",
  "timestamp": "$timestamp",
  "duration_sec": ${WRK_DURATION%s},
  "wrk": {
    "threads": $WRK_THREADS,
    "connections": $WRK_CONNECTIONS,
    "requests_per_sec": $requests_per_sec
  },
  "latency": {
    "p50": "$latency_p50",
    "p75": "$latency_p75",
    "p90": "$latency_p90",
    "p99": "$latency_p99",
    "p99.9": "$latency_p999"
  },
  "throttling": {
    "percentage": $throttle_percentage,
    "nr_periods": $nr_periods,
    "nr_throttled": $nr_throttled,
    "throttled_usec": $throttled_time
  },
  "pod": "$nginx_pod"
}
EOF
    
    echo -e "  ${GREEN}✓${NC} Evidence saved: $evidence_file"
    echo ""
    echo "  Results:"
    echo "    Requests/sec: $requests_per_sec"
    echo "    Latency P50: $latency_p50"
    echo "    Latency P95: (see P90 + P99)"
    echo "    Latency P99: $latency_p99"
    echo "    Latency P99.9: $latency_p999"
    echo "    CPU Throttling: ${throttle_percentage}%"
    echo ""
    
    rm -f "$wrk_output"
}

# Main experiment loop
EXPERIMENT_COUNT=0

for config in baseline light moderate heavy; do
    EXPERIMENT_COUNT=$((EXPERIMENT_COUNT + 1))
    expected_throttle="${configs[$config]}"
    
    echo ""
    echo "================================================================================"
    echo "Experiment $EXPERIMENT_COUNT/4: $config (Expected Throttling: $expected_throttle)"
    echo "================================================================================"
    
    # Deploy nginx with config
    manifest="$MANIFESTS_DIR/nginx-${config}.yaml"
    if [ ! -f "$manifest" ]; then
        echo -e "${YELLOW}⚠${NC} Manifest not found: $manifest"
        echo "  Skipping $config"
        continue
    fi
    
    echo "  Applying manifest: $manifest"
    kubectl apply -f "$manifest" -n "$NAMESPACE" > /dev/null
    
    # Wait for pod
    pod_name="nginx-$config"
    if ! wait_for_pod "$pod_name"; then
        echo "  Skipping $config due to pod failure"
        kubectl delete -f "$manifest" -n "$NAMESPACE" --ignore-not-found > /dev/null
        continue
    fi
    
    # Ensure service exists
    if ! kubectl get svc nginx-service -n "$NAMESPACE" &> /dev/null; then
        echo "  Creating nginx service..."
        kubectl apply -f "$MANIFESTS_DIR/nginx-service.yaml" -n "$NAMESPACE" > /dev/null
        sleep 5
    fi
    
    # Run wrk test
    run_wrk_test "$config" "$pod_name"
    
    # Cleanup
    echo "  Cleaning up..."
    kubectl delete -f "$manifest" -n "$NAMESPACE" --ignore-not-found > /dev/null
    
    # Wait between experiments
    if [ "$config" != "heavy" ]; then
        echo "  Waiting 10s before next experiment..."
        sleep 10
    fi
done

# Cleanup service
echo ""
echo "  Cleaning up service..."
kubectl delete svc nginx-service -n "$NAMESPACE" --ignore-not-found > /dev/null

echo ""
echo "================================================================================"
echo "All experiments complete!"
echo "================================================================================"
echo ""
echo "Evidence directory: $EVIDENCE_DIR"
echo ""
echo "Next steps:"
echo "  1. Analyze results: python3 analyze-latency.py"
echo "  2. View report: cat LATENCY_REPORT.md"
echo ""
