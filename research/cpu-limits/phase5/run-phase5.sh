#!/usr/bin/env bash
#
# Phase 5 Quick Start
# Run key Phase 5 experiments to complete the research
#

set -e

PHASE5_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PHASE5_DIR"

echo "============================================================================="
echo " Phase 5: Scheduler Deep Dive & Production Readiness"
echo "============================================================================="
echo ""
echo "This script runs the Phase 5 experiments that complete the research:"
echo "  1. BPF trace collection during throttling (Task 5.1)"
echo "  2. Latency-sensitive workload testing (Task 5.2)"
echo "  3. Kernel scheduler parameter testing (Task 5.3) - requires node SSH"
echo "  4. Custom scheduler comparison (Task 5.4) - requires kernel 6.12+"
echo ""

# Check prerequisites
echo "Checking prerequisites..."

# Check kubectl
if ! command -v kubectl &> /dev/null; then
    echo "Error: kubectl not found"
    exit 1
fi
echo "✓ kubectl found"

# Check cluster access
if ! kubectl get nodes &> /dev/null; then
    echo "Error: Cannot access Kubernetes cluster"
    exit 1
fi
echo "✓ Cluster access OK"

# Check Phase 4 workloads
if [ ! -f "../phase4/workloads/cpu-bound-heavy-throttle.yaml" ]; then
    echo "Error: Phase 4 workloads not found"
    echo "Please complete Phase 4 first"
    exit 1
fi
echo "✓ Phase 4 workloads found"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found"
    exit 1
fi
echo "✓ python3 found"

echo ""
echo "Prerequisites: OK"
echo ""

# Ask which tasks to run
echo "Which experiments do you want to run?"
echo ""
echo "  1) Task 5.1: BPF Trace Collection (requires node SSH)"
echo "  2) Task 5.2: Latency Testing (requires wrk image)"
echo "  3) Both Task 5.1 and 5.2 (Recommended)"
echo "  4) Check system for Task 5.3 and 5.4 prerequisites"
echo "  5) Exit"
echo ""

read -p "Select option [1-5]: " choice

case $choice in
    1)
        echo ""
        echo "============================================================================="
        echo " Task 5.1: BPF Trace Collection During Throttling"
        echo "============================================================================="
        echo ""
        echo "This task collects low-level scheduler events during CPU throttling."
        echo ""
        echo "Prerequisites:"
        echo "  - SSH access to cluster nodes"
        echo "  - bpftrace installed on nodes"
        echo "  - Phase 3 BPF infrastructure deployed (optional)"
        echo ""
        read -p "Do you have SSH access to cluster nodes? [y/N]: " has_ssh
        
        if [[ "$has_ssh" =~ ^[Yy]$ ]]; then
            echo ""
            echo "Starting BPF trace collection..."
            cd bpf-correlation
            python3 collect-during-throttle.py
            cd ..
        else
            echo ""
            echo "Note: Task 5.1 requires SSH access to nodes where pods run."
            echo "      See phase3/README.md for BPF infrastructure setup."
            echo ""
            echo "Alternative: Run Phase 3 BPF tracer as DaemonSet (does not require SSH)"
            echo "             See phase3/daemonset/bpf-tracer-daemonset.yaml"
        fi
        ;;
    
    2)
        echo ""
        echo "============================================================================="
        echo " Task 5.2: Latency-Sensitive Workload Testing"
        echo "============================================================================="
        echo ""
        echo "This task measures P50/P95/P99 HTTP latency under different throttling levels."
        echo ""
        echo "Duration: ~15-20 minutes (3 configs × 2 min load test each)"
        echo ""
        read -p "Continue? [Y/n]: " continue_latency
        
        if [[ ! "$continue_latency" =~ ^[Nn]$ ]]; then
            echo ""
            echo "Starting latency testing..."
            cd experiments/latency-sensitive
            python3 run-latency-test.py
            cd ../..
        fi
        ;;
    
    3)
        echo ""
        echo "============================================================================="
        echo " Running Both Experiments"
        echo "============================================================================="
        echo ""
        echo "This will run Task 5.1 and Task 5.2 sequentially."
        echo "Total duration: ~20-30 minutes"
        echo ""
        read -p "Continue? [Y/n]: " continue_both
        
        if [[ ! "$continue_both" =~ ^[Nn]$ ]]; then
            # Task 5.1
            echo ""
            echo "--- Task 5.1: BPF Traces ---"
            read -p "Do you have SSH access to cluster nodes? [y/N]: " has_ssh
            
            if [[ "$has_ssh" =~ ^[Yy]$ ]]; then
                cd bpf-correlation
                python3 collect-during-throttle.py || echo "Task 5.1 failed, continuing..."
                cd ..
            else
                echo "Skipping Task 5.1 (requires SSH)"
            fi
            
            echo ""
            echo "Waiting 10s before Task 5.2..."
            sleep 10
            
            # Task 5.2
            echo ""
            echo "--- Task 5.2: Latency Testing ---"
            cd experiments/latency-sensitive
            python3 run-latency-test.py
            cd ../..
        fi
        ;;
    
    4)
        echo ""
        echo "============================================================================="
        echo " Checking Prerequisites for Advanced Tasks"
        echo "============================================================================="
        echo ""
        
        # Check kernel version
        echo "Checking kernel version..."
        kernel_version=$(uname -r | cut -d. -f1,2)
        echo "  Kernel: $(uname -r)"
        
        if (( $(echo "$kernel_version >= 6.12" | bc -l) )); then
            echo "  ✓ Kernel 6.12+ (scx support possible)"
            
            # Check for sched-ext
            if grep -q "CONFIG_SCHED_CLASS_EXT=y" /boot/config-$(uname -r) 2>/dev/null; then
                echo "  ✓ CONFIG_SCHED_CLASS_EXT=y (scx enabled)"
                
                # Check for scx tools
                if command -v scx_simple &> /dev/null; then
                    echo "  ✓ scx tools installed"
                    echo ""
                    echo "Task 5.4 (Custom Scheduler Comparison) is READY"
                else
                    echo "  ✗ scx tools not installed"
                    echo ""
                    echo "Install scx tools:"
                    echo "  git clone https://github.com/sched-ext/scx.git"
                    echo "  cd scx && make && sudo make install"
                fi
            else
                echo "  ✗ CONFIG_SCHED_CLASS_EXT not enabled"
                echo ""
                echo "Task 5.4 requires kernel with sched-ext support"
            fi
        else
            echo "  ✗ Kernel < 6.12 (scx not available)"
            echo ""
            echo "Task 5.4 (Custom Scheduler Comparison) requires kernel 6.12+"
        fi
        
        echo ""
        
        # Check node SSH access
        echo "Checking node SSH access (for Task 5.3)..."
        nodes=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
        
        if [ -n "$nodes" ]; then
            first_node=$(echo "$nodes" | head -1)
            echo "  First node IP: $first_node"
            
            if ssh -o ConnectTimeout=5 -o BatchMode=yes root@$first_node "echo 'SSH OK'" 2>/dev/null; then
                echo "  ✓ SSH access to nodes"
                echo ""
                echo "Task 5.3 (Scheduler Parameter Testing) is READY"
            else
                echo "  ✗ No SSH access to nodes"
                echo ""
                echo "Task 5.3 requires SSH access to tune kernel parameters"
            fi
        else
            echo "  ✗ Cannot determine node IPs"
        fi
        
        echo ""
        echo "--- Prerequisites Summary ---"
        echo "Task 5.1 (BPF Traces): SSH access required"
        echo "Task 5.2 (Latency Testing): Ready to run"
        echo "Task 5.3 (Scheduler Params): SSH access required"
        echo "Task 5.4 (Custom Schedulers): Kernel 6.12+ required"
        ;;
    
    5)
        echo "Exiting"
        exit 0
        ;;
    
    *)
        echo "Invalid option"
        exit 1
        ;;
esac

echo ""
echo "============================================================================="
echo " Phase 5 Experiment Complete"
echo "============================================================================="
echo ""
echo "Results saved to: evidence/"
echo ""
echo "Next steps:"
echo "  1. Review results: ls -lh evidence/"
echo "  2. Read admin guides:"
echo "     - admin-guide/KERNEL_CONFIG.md"
echo "     - admin-guide/CUSTOM_SCHEDULERS.md"
echo "  3. See README.md for full Phase 5 documentation"
echo ""
