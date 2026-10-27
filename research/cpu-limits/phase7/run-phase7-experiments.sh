#!/bin/bash
set -euo pipefail

#
# Phase 7 Quick Start - Run All Implemented Experiments
#
# This script executes all ready-to-run Phase 7 experiments:
# - Topic 7.1: Real application testing (nginx)
# - Topic 7.4: Live BPF tracing
# - Topic 7.5: Cgroup v2 analysis
#
# Estimated runtime: 10-12 minutes
# Requirements: kubectl, python3, bpftrace (optional), sudo (for BPF)
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
RESEARCH_ROOT="$(dirname "$SCRIPT_DIR")"
EVIDENCE_DIR="$RESEARCH_ROOT/evidence/phase7"

echo "================================================================================"
echo "Phase 7 Quick Start: Running All Implemented Experiments"
echo "================================================================================"
echo ""
echo "Estimated runtime: 10-12 minutes"
echo ""

# Check prerequisites
echo "Checking prerequisites..."

# Check kubectl
if ! command -v kubectl &> /dev/null; then
    echo "✗ kubectl not found"
    exit 1
fi
echo "  ✓ kubectl available"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "✗ python3 not found"
    exit 1
fi
echo "  ✓ python3 available"

# Check cluster access
if ! kubectl get nodes &> /dev/null; then
    echo "✗ Cannot access Kubernetes cluster"
    exit 1
fi
echo "  ✓ Kubernetes cluster accessible"

# Check bpftrace (optional)
BPF_AVAILABLE=false
if command -v bpftrace &> /dev/null; then
    echo "  ✓ bpftrace available (will collect live traces)"
    BPF_AVAILABLE=true
else
    echo "  ⚠ bpftrace not found (skipping live BPF tracing)"
fi

echo ""
echo "================================================================================"
echo "Experiment 1: Topic 7.5 - Cgroup V2 Analysis"
echo "================================================================================"
echo ""
echo "Analyzing cgroup v2 CPU parameters..."
echo ""

cd "$SCRIPT_DIR/7.5-cgroup-deep-dive"
chmod +x analyze-cgroup-v2.py

if python3 analyze-cgroup-v2.py; then
    echo ""
    echo "✓ Cgroup v2 analysis complete"
    echo "  Evidence: $EVIDENCE_DIR/cgroup-v2/"
else
    echo "✗ Cgroup v2 analysis failed (may require elevated permissions)"
fi

echo ""
echo "Waiting 5 seconds before next experiment..."
sleep 5

echo ""
echo "================================================================================"
echo "Experiment 2: Topic 7.1 - Real Application Testing (nginx)"
echo "================================================================================"
echo ""
echo "This experiment will:"
echo "  1. Deploy nginx pods with 4 different CPU limit configurations"
echo "  2. Run wrk HTTP load tests (60s each)"
echo "  3. Collect latency and throughput metrics"
echo "  4. Measure CPU throttling from cgroups"
echo ""
echo "Estimated runtime: 6-8 minutes"
echo ""

# Optional: Start BPF trace in background
BPF_PID=""
if $BPF_AVAILABLE; then
    echo "Starting BPF trace in background..."
    
    TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
    BPF_OUTPUT="$EVIDENCE_DIR/live-bpf/trace-nginx-$TIMESTAMP.json"
    mkdir -p "$EVIDENCE_DIR/live-bpf"
    
    cd "$SCRIPT_DIR/7.4-live-bpf"
    chmod +x scheduler-trace.bt
    
    # Run bpftrace in background (requires sudo)
    if sudo -n true 2>/dev/null; then
        echo "  Starting: sudo bpftrace scheduler-trace.bt > $BPF_OUTPUT"
        sudo bpftrace scheduler-trace.bt > "$BPF_OUTPUT" 2>&1 &
        BPF_PID=$!
        echo "  ✓ BPF trace started (PID: $BPF_PID)"
        echo "  Will collect for duration of nginx tests"
    else
        echo "  ⚠ sudo required for BPF tracing (skipping)"
    fi
    
    echo ""
fi

# Run nginx tests
cd "$SCRIPT_DIR/7.1-real-apps"
chmod +x test-nginx-throttling.py

echo "Running nginx throttling tests..."
if python3 test-nginx-throttling.py; then
    echo ""
    echo "✓ nginx testing complete"
    echo "  Evidence: $EVIDENCE_DIR/real-apps/"
else
    echo "✗ nginx testing failed"
fi

# Stop BPF trace if running
if [ -n "$BPF_PID" ]; then
    echo ""
    echo "Stopping BPF trace..."
    sudo kill -SIGINT "$BPF_PID" 2>/dev/null || true
    wait "$BPF_PID" 2>/dev/null || true
    echo "✓ BPF trace stopped"
    echo "  Evidence: $BPF_OUTPUT"
fi

echo ""
echo "================================================================================"
echo "Phase 7 Experiments Complete!"
echo "================================================================================"
echo ""
echo "Evidence collected:"
echo "  • Cgroup v2 analysis: $EVIDENCE_DIR/cgroup-v2/"
echo "  • nginx throttling:   $EVIDENCE_DIR/real-apps/"

if $BPF_AVAILABLE && [ -n "$BPF_PID" ]; then
    echo "  • BPF scheduler trace: $EVIDENCE_DIR/live-bpf/"
fi

echo ""
echo "Next steps:"
echo "  1. Review evidence files (JSON format)"
echo "  2. Compare results with Phase 4 synthetic workloads"
echo "  3. Optionally: Implement Topics 7.2-7.3 (QoS, interference)"
echo ""
echo "Documentation:"
echo "  • Phase 7 Status:  $SCRIPT_DIR/STATUS.md"
echo "  • Phase 7 Summary: $RESEARCH_ROOT/PHASE7_SUMMARY.md"
echo "  • Developer Guide: $RESEARCH_ROOT/DEVELOPER_GUIDE.md"
echo ""
echo "================================================================================"
