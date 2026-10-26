#!/bin/bash
# Check if all prerequisites for Topic 6.1 are met

set -euo pipefail

echo "Checking prerequisites for Topic 6.1: Latency Testing..."
echo ""

PREREQS_MET=true

# Check kubectl
if command -v kubectl &> /dev/null; then
    echo "✓ kubectl found: $(kubectl version --client --short 2>/dev/null || kubectl version --client 2>&1 | head -1)"
else
    echo "✗ kubectl not found"
    echo "  Install: https://kubernetes.io/docs/tasks/tools/"
    PREREQS_MET=false
fi

# Check wrk
if command -v wrk &> /dev/null; then
    echo "✓ wrk found: $(wrk --version 2>&1 | head -1)"
else
    echo "✗ wrk not found"
    echo "  Install:"
    echo "    Ubuntu/Debian: sudo apt install wrk"
    echo "    macOS: brew install wrk"
    echo "    Source: https://github.com/wg/wrk"
    PREREQS_MET=false
fi

# Check cluster access
if kubectl cluster-info &> /dev/null; then
    CLUSTER=$(kubectl config current-context)
    echo "✓ Kubernetes cluster accessible: $CLUSTER"
else
    echo "✗ Cannot access Kubernetes cluster"
    echo "  Run: kubectl cluster-info"
    PREREQS_MET=false
fi

# Check Python 3
if command -v python3 &> /dev/null; then
    PYTHON_VER=$(python3 --version)
    echo "✓ Python 3 found: $PYTHON_VER"
else
    echo "✗ Python 3 not found"
    echo "  Install: https://www.python.org/downloads/"
    PREREQS_MET=false
fi

echo ""
if [ "$PREREQS_MET" = true ]; then
    echo "✓ All prerequisites met"
    echo ""
    echo "Ready to run experiments:"
    echo "  ./run-nginx-latency-test.sh"
    exit 0
else
    echo "✗ Some prerequisites missing"
    echo "  Install missing tools and try again"
    exit 1
fi
