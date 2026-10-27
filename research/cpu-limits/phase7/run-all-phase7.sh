#!/bin/bash
set -euo pipefail

#
# Phase 7 runner - execute every implemented experiment in sequence
#
# Topics:
#   7.1  nginx under CPU quota (real application)
#   7.2  QoS class comparison
#   7.3  noisy neighbour interference
#   7.4  live BPF scheduling trace
#   7.5  cgroup v2 CPU parameter analysis
#   7.6  kernel scheduler comparison (CFS modes, sched_ext, preempt)
#   7.7  production incident simulation
#   7.8  cost / TCO model
#
# Every topic except 7.8 needs the cluster and a node the reader pod can
# attach to. Set PHASE7_NODE to pin the pods to one node; without it the
# scheduler chooses and the scripts follow it.
#
# Estimated runtime: 20-30 minutes.
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RESEARCH_ROOT="$(dirname "$SCRIPT_DIR")"
EVIDENCE_DIR="$RESEARCH_ROOT/evidence/phase7"

export PHASE7_NAMESPACE="${PHASE7_NAMESPACE:-cpu-limits-research}"
export PHASE7_NODE="${PHASE7_NODE:-}"

echo "================================================================================"
echo "Phase 7 runner: all implemented experiments"
echo "================================================================================"
echo ""
echo "  namespace: $PHASE7_NAMESPACE"
echo "  node:      ${PHASE7_NODE:-(scheduler chooses; scripts follow)}"
echo ""
echo "Topics:"
echo "  1. 7.5 cgroup v2 analysis        (<1 min)"
echo "  2. 7.8 cost / TCO model          (<1 min)"
echo "  3. 7.1 nginx throttling          (~8 min)"
echo "  4. 7.2 QoS comparison            (~6 min)"
echo "  5. 7.3 noisy neighbour           (~5 min)"
echo "  6. 7.4 live BPF scheduling trace (~4 min)"
echo "  7. 7.6 kernel scheduler comparison    (~25 min)"
echo "  8. 7.7 production incident simulation (~10 min)"
echo ""

# Prerequisites
echo "Checking prerequisites..."

if ! command -v kubectl &> /dev/null; then
    echo "✗ kubectl not found"
    exit 1
fi
echo "  ✓ kubectl available"

if ! command -v python3 &> /dev/null; then
    echo "✗ python3 not found"
    exit 1
fi
echo "  ✓ python3 available"

if ! kubectl get nodes &> /dev/null; then
    echo "✗ cannot access the Kubernetes cluster"
    exit 1
fi
echo "  ✓ Kubernetes cluster accessible"

echo ""

declare -a COMPLETED=()
declare -a FAILED=()

run_experiment() {
    local TOPIC=$1
    local NAME=$2
    local SCRIPT=$3
    local DURATION=$4

    echo ""
    echo "================================================================================"
    echo "Experiment: Topic $TOPIC - $NAME"
    echo "================================================================================"
    echo ""
    echo "Estimated runtime: $DURATION"
    echo ""

    if eval "$SCRIPT"; then
        echo ""
        echo "✓ Topic $TOPIC complete"
        COMPLETED+=("$TOPIC: $NAME")
    else
        echo ""
        echo "✗ Topic $TOPIC failed"
        FAILED+=("$TOPIC: $NAME")
    fi

    echo ""
    echo "Waiting 10 seconds before the next experiment..."
    sleep 10
}

run_experiment \
    "7.5" \
    "cgroup v2 CPU parameters" \
    "cd '$SCRIPT_DIR/7.5-cgroup-deep-dive' && python3 analyze-cgroup-v2.py" \
    "<1 minute"

run_experiment \
    "7.8" \
    "cost / TCO model" \
    "cd '$SCRIPT_DIR/7.8-cost-analysis' && python3 calculate-tco.py" \
    "<1 minute"

run_experiment \
    "7.1" \
    "nginx under CPU quota" \
    "cd '$SCRIPT_DIR/7.1-real-apps' && python3 test-nginx-throttling.py" \
    "~8 minutes"

run_experiment \
    "7.2" \
    "QoS class comparison" \
    "cd '$SCRIPT_DIR/7.2-qos-comparison' && python3 test-qos-classes.py" \
    "~6 minutes"

run_experiment \
    "7.3" \
    "noisy neighbour interference" \
    "cd '$SCRIPT_DIR/7.3-interference' && python3 test-noisy-neighbor.py" \
    "~5 minutes"

run_experiment \
    "7.4" \
    "live BPF scheduling trace" \
    "cd '$SCRIPT_DIR/7.4-live-bpf' && python3 trace-scheduler.py" \
    "~4 minutes"

# 7.6 needs the custom kernel node: the CFS group modes and sched_ext only
# exist on 7.3.0-k8labs. It also reboots nothing, but it does take the node
# over for the duration of the matrix.
run_experiment \
    "7.6" \
    "kernel scheduler comparison" \
    "cd '$SCRIPT_DIR/7.6-kernel-comparison' && python3 test-kernel-comparison.py --reps 2 --duration 8" \
    "~25 minutes"

run_experiment \
    "7.7" \
    "production incident simulation" \
    "cd '$SCRIPT_DIR/7.7-incidents' && python3 test-incidents.py" \
    "~10 minutes"

echo ""
echo "================================================================================"
echo "Phase 7 finished"
echo "================================================================================"
echo ""

if [ ${#COMPLETED[@]} -gt 0 ]; then
    echo "✓ Completed (${#COMPLETED[@]}):"
    for exp in "${COMPLETED[@]}"; do
        echo "    $exp"
    done
    echo ""
fi

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "✗ Failed (${#FAILED[@]}):"
    for exp in "${FAILED[@]}"; do
        echo "    $exp"
    done
    echo ""
fi

echo "Evidence:"
echo "  • cgroup v2:    $EVIDENCE_DIR/cgroup-v2/"
echo "  • cost model:   $EVIDENCE_DIR/cost-analysis/"
echo "  • nginx:        $EVIDENCE_DIR/real-apps/"
echo "  • QoS:          $EVIDENCE_DIR/qos-comparison/"
echo "  • interference: $EVIDENCE_DIR/interference/"
echo "  • live BPF:     $EVIDENCE_DIR/live-bpf/"
echo "  • kernel:       $EVIDENCE_DIR/kernel-comparison/"
echo "  • incidents:    $EVIDENCE_DIR/incidents/"
echo ""

if [ ${#FAILED[@]} -eq 0 ]; then
    exit 0
else
    exit 1
fi
