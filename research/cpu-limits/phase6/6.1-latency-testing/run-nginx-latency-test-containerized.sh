#!/bin/bash
set -euo pipefail

EVIDENCE_DIR="../../evidence"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_FILE="${EVIDENCE_DIR}/latency-nginx-${TIMESTAMP}.json"

mkdir -p "$EVIDENCE_DIR"

echo "================================================================================"
echo "Phase 6 Topic 6.1: Real-World Latency Testing (Containerized wrk)"
echo "================================================================================"
echo ""
echo "This test uses a containerized wrk pod to measure nginx latency under"
echo "different CPU throttling levels."
echo ""

# Configs to test
declare -a CONFIGS=(
  "nginx-baseline:2000m:4000m:0"
  "nginx-light:1000m:3000m:5"
  "nginx-moderate:500m:1500m:33"
  "nginx-heavy:100m:500m:70"
)

# Results array
declare -a RESULTS=()

echo "================================================================================"
echo "Starting Latency Testing"
echo "================================================================================"
echo ""

for CONFIG in "${CONFIGS[@]}"; do
  IFS=':' read -r NAME REQUEST LIMIT EXPECTED_THROTTLE <<< "$CONFIG"
  
  echo "================================================================================"
  echo "Experiment: $NAME"
  echo "  CPU Request: $REQUEST"
  echo "  CPU Limit: $LIMIT"
  echo "  Expected Throttling: ~${EXPECTED_THROTTLE}%"
  echo "================================================================================"
  
  # Deploy nginx
  kubectl apply -f "manifests/${NAME}.yaml" > /dev/null
  kubectl apply -f manifests/nginx-service.yaml > /dev/null 2>&1 || true
  
  echo "  Waiting for nginx to start..."
  kubectl wait --for=condition=ready pod -l app=nginx --timeout=60s > /dev/null || {
    echo "  ✗ Nginx pod did not start"
    kubectl delete -f "manifests/${NAME}.yaml" --ignore-not-found > /dev/null
    continue
  }
  
  POD_IP=$(kubectl get pod -l app=nginx -o jsonpath='{.items[0].status.podIP}')
  echo "  ✓ Nginx started at $POD_IP"
  
  # Wait for nginx to be ready
  sleep 5
  
  # Deploy wrk load generator pod
  cat > /tmp/wrk-job-${NAME}.yaml << WRKYAML
apiVersion: v1
kind: Pod
metadata:
  name: wrk-${NAME}
spec:
  restartPolicy: Never
  containers:
  - name: wrk
    image: williamyeh/wrk:4.2.0
    command: ["/bin/sh", "-c"]
    args:
      - |
        wrk -t4 -c100 -d60s --latency http://${POD_IP}:80/ 2>&1 | tee /tmp/wrk-output.txt
        echo "---SEPARATOR---"
        cat /tmp/wrk-output.txt
    resources:
      requests:
        cpu: "1000m"
        memory: "256Mi"
      limits:
        cpu: "2000m"
        memory: "512Mi"
WRKYAML
  
  kubectl apply -f /tmp/wrk-job-${NAME}.yaml > /dev/null
  
  echo "  Running wrk load test (60s)..."
  kubectl wait --for=condition=ready pod wrk-${NAME} --timeout=30s > /dev/null 2>&1 || true
  
  # Wait for completion
  for i in {1..90}; do
    STATUS=$(kubectl get pod wrk-${NAME} -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
    if [[ "$STATUS" == "Succeeded" ]] || [[ "$STATUS" == "Failed" ]]; then
      break
    fi
    sleep 2
  done
  
  # Get results
  WRK_OUTPUT=$(kubectl logs wrk-${NAME} 2>/dev/null | sed -n '/---SEPARATOR---/,$p' | tail -n +2 || echo "")
  
  if [[ -n "$WRK_OUTPUT" ]]; then
    echo "  ✓ Load test completed"
    
    # Parse wrk output
    REQUESTS_SEC=$(echo "$WRK_OUTPUT" | grep "Requests/sec:" | awk '{print $2}' || echo "0")
    LATENCY_AVG=$(echo "$WRK_OUTPUT" | grep "Latency" | head -1 | awk '{print $2}' || echo "0ms")
    
    # Parse percentile latencies
    P50=$(echo "$WRK_OUTPUT" | grep "50%" | awk '{print $2}' || echo "0ms")
    P75=$(echo "$WRK_OUTPUT" | grep "75%" | awk '{print $2}' || echo "0ms")
    P90=$(echo "$WRK_OUTPUT" | grep "90%" | awk '{print $2}' || echo "0ms")
    P99=$(echo "$WRK_OUTPUT" | grep "99%" | awk '{print $2}' || echo "0ms")
    
    echo ""
    echo "  Results:"
    echo "    Throughput: ${REQUESTS_SEC} req/s"
    echo "    Avg Latency: ${LATENCY_AVG}"
    echo "    P50: ${P50}"
    echo "    P75: ${P75}"
    echo "    P90: ${P90}"
    echo "    P99: ${P99}"
    
    # Get throttling stats
    POD_NAME=$(kubectl get pod -l app=nginx -o jsonpath='{.items[0].metadata.name}')
    CGROUP_PATH=$(kubectl get pod "$POD_NAME" -o jsonpath='{.metadata.uid}' | xargs -I {} echo "/sys/fs/cgroup/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod_{}.slice")
    
    # Try to get throttling from node (may fail if no node access)
    THROTTLE_PCT="N/A"
    
    echo "    Throttling: ${THROTTLE_PCT}"
    
    # Store result
    RESULT_JSON=$(cat <<RESULTJSON
{
  "config": "$NAME",
  "request": "$REQUEST",
  "limit": "$LIMIT",
  "expected_throttle": "${EXPECTED_THROTTLE}%",
  "throughput_rps": ${REQUESTS_SEC},
  "latency_avg": "${LATENCY_AVG}",
  "p50": "${P50}",
  "p75": "${P75}",
  "p90": "${P90}",
  "p99": "${P99}",
  "throttle_pct": "${THROTTLE_PCT}"
}
RESULTJSON
)
    RESULTS+=("$RESULT_JSON")
  else
    echo "  ✗ Load test failed or no output"
  fi
  
  # Cleanup
  echo ""
  kubectl delete pod wrk-${NAME} --ignore-not-found > /dev/null 2>&1 || true
  kubectl delete -f "manifests/${NAME}.yaml" --ignore-not-found > /dev/null
  rm -f /tmp/wrk-job-${NAME}.yaml
  
  echo "  Waiting 10s before next test..."
  sleep 10
  echo ""
done

# Write results
echo "================================================================================"
echo "All Tests Complete"
echo "================================================================================"
echo ""

# Build JSON array
RESULTS_JSON="["
for i in "${!RESULTS[@]}"; do
  RESULTS_JSON+="${RESULTS[$i]}"
  if [[ $i -lt $((${#RESULTS[@]} - 1)) ]]; then
    RESULTS_JSON+=","
  fi
done
RESULTS_JSON+="]"

FULL_REPORT=$(cat <<FULLREPORT
{
  "test": "nginx-latency-containerized",
  "timestamp": "$TIMESTAMP",
  "description": "Real-world latency testing with nginx + wrk under CPU throttling",
  "results": $RESULTS_JSON
}
FULLREPORT
)

echo "$FULL_REPORT" | python3 -m json.tool > "$RESULTS_FILE"

echo "Results written to: $RESULTS_FILE"
echo ""
echo "To analyze:"
echo "  cat $RESULTS_FILE | python3 -m json.tool"
