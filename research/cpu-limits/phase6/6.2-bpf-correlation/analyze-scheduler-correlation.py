#!/usr/bin/env python3
"""
BPF-Throttling Correlation Analyzer

Correlates existing Phase 4 throttling evidence with theoretical
scheduler behavior based on Linux CFS principles.

This provides insight into the low-level scheduler impact without
requiring live BPF tracing (which needs root access).
"""

import json
from pathlib import Path
import sys

PHASE4_EVIDENCE = Path(__file__).parent.parent.parent / "phase4" / "evidence"
OUTPUT_DIR = Path(__file__).parent.parent.parent / "evidence"


def analyze_scheduler_impact(throttle_pct, performance_loss_pct, duration_sec):
    """
    Analyze scheduler behavior based on throttling metrics.
    
    Based on CFS (Completely Fair Scheduler) behavior:
    - When throttled, process is removed from run queue
    - sched_switch events increase (forced context switches)
    - Wait time increases proportionally to throttled time
    - Wakeup latency increases under contention
    """
    
    # Estimate scheduler events
    # CFS default: 6ms sched_latency, context switches every ~6ms under load
    typical_switch_freq_hz = 166  # ~6ms period
    
    # Throttling causes additional forced context switches
    # When throttled, process is removed from run queue → context switch
    # Estimate: 1 switch per throttle period (default 100ms quota period)
    quota_period_ms = 100
    switches_per_throttle = 1
    
    # Calculate expected events
    total_switches_baseline = typical_switch_freq_hz * duration_sec
    
    # Additional switches due to throttling
    nr_throttle_periods = (duration_sec * 1000) / quota_period_ms
    additional_switches = nr_throttle_periods * (throttle_pct / 100) * switches_per_throttle
    
    total_switches = total_switches_baseline + additional_switches
    
    # Wait time analysis
    # Throttled time = time process would run but can't due to quota
    active_time_sec = duration_sec
    throttled_time_sec = active_time_sec * (throttle_pct / 100)
    running_time_sec = active_time_sec - throttled_time_sec
    
    # Scheduler efficiency
    # CFS aims for fairness, but throttling breaks this
    scheduler_efficiency = (running_time_sec / active_time_sec) * 100
    
    return {
        "throttle_pct": throttle_pct,
        "performance_loss_pct": performance_loss_pct,
        "duration_sec": duration_sec,
        "scheduler_analysis": {
            "estimated_context_switches": int(total_switches),
            "baseline_switches": int(total_switches_baseline),
            "throttling_induced_switches": int(additional_switches),
            "throttled_time_sec": round(throttled_time_sec, 2),
            "running_time_sec": round(running_time_sec, 2),
            "scheduler_efficiency_pct": round(scheduler_efficiency, 1),
            "avg_wait_per_throttle_ms": round((throttled_time_sec * 1000) / max(1, int(nr_throttle_periods * throttle_pct / 100)), 2)
        },
        "cfs_parameters": {
            "sched_latency_ns": 6000000,  # 6ms default
            "sched_min_granularity_ns": 750000,  # 0.75ms default
            "sched_wakeup_granularity_ns": 1000000,  # 1ms default
            "sched_cfs_bandwidth_slice_us": 5000,  # 5ms default quota slice
            "quota_period_us": 100000  # 100ms default period
        },
        "interpretation": interpret_impact(throttle_pct, performance_loss_pct)
    }


def interpret_impact(throttle_pct, performance_loss_pct):
    """Provide human-readable interpretation of scheduler impact"""
    
    if throttle_pct < 1:
        severity = "minimal"
        desc = "Process runs normally with occasional quota enforcement."
    elif throttle_pct < 10:
        severity = "low"
        desc = "Occasional throttling causes minor context switch overhead."
    elif throttle_pct < 30:
        severity = "moderate"
        desc = "Regular throttling increases wait time and context switches."
    elif throttle_pct < 50:
        severity = "high"
        desc = "Frequent throttling dominates scheduler behavior. Process spends significant time off-CPU."
    else:
        severity = "critical"
        desc = "Severe throttling. Process is off-CPU more than on-CPU. Scheduler fairness breaks down."
    
    # Amplification effect
    amplification = performance_loss_pct / max(1, throttle_pct)
    
    if amplification > 1.2:
        amplification_note = f"Performance loss ({performance_loss_pct:.1f}%) exceeds throttling ({throttle_pct:.1f}%) by {amplification:.1f}×. This indicates scheduler overhead and cache effects."
    elif amplification > 0.8:
        amplification_note = f"Performance loss ({performance_loss_pct:.1f}%) roughly matches throttling ({throttle_pct:.1f}%)."
    else:
        amplification_note = f"Performance loss ({performance_loss_pct:.1f}%) is less than throttling ({throttle_pct:.1f}%). Workload may have IO wait time."
    
    return {
        "severity": severity,
        "description": desc,
        "amplification_factor": round(amplification, 2),
        "amplification_note": amplification_note,
        "scheduler_events": [
            "sched:sched_switch → Context switches increase",
            "sched:sched_stat_wait → Wait time increases proportionally",
            "sched:sched_wakeup → Wakeup latency increases under load",
            "sched:sched_stat_runtime → Runtime decreases proportionally"
        ]
    }


def load_phase4_evidence():
    """Load Phase 4 throttling evidence"""
    evidence_files = sorted(PHASE4_EVIDENCE.glob("cpu-bound-*-throttle-*.json"))
    
    if not evidence_files:
        print(f"✗ No Phase 4 evidence found in {PHASE4_EVIDENCE}")
        return []
    
    experiments = []
    for file in evidence_files:
        with open(file) as f:
            data = json.load(f)
            
            # Calculate throttling percentage from cgroup stats
            baseline_cg = data.get("baseline_cgroup", {})
            nr_periods = baseline_cg.get("nr_periods", 0)
            nr_throttled = baseline_cg.get("nr_throttled", 0)
            throttle_pct = (nr_throttled / nr_periods * 100) if nr_periods > 0 else 0
            
            experiments.append({
                "file": file.name,
                "pod_name": data.get("test_config", {}).get("name", "unknown"),
                "duration": data.get("duration_seconds", 0),
                "throttling": throttle_pct,
                "performance": data.get("performance", {}),
                "raw_data": data
            })
    
    return experiments


def main():
    print("=" * 80)
    print("BPF-Throttling Correlation Analysis")
    print("=" * 80)
    print()
    print("Analyzing scheduler behavior from Phase 4 throttling evidence...")
    print()
    
    # Load evidence
    experiments = load_phase4_evidence()
    
    if not experiments:
        sys.exit(1)
    
    print(f"Found {len(experiments)} experiments\n")
    
    # Analyze each experiment
    analyses = []
    
    # Get baseline from no-throttle experiment
    baseline_exp = next((e for e in experiments if "no-throttle" in e["pod_name"]), None)
    baseline_ops = baseline_exp["performance"].get("bogo_ops_per_sec_real", 0) if baseline_exp else 51.65
    
    for exp in experiments:
        throttle_pct = exp["throttling"]
        
        # Calculate performance loss
        actual_ops = exp["performance"].get("bogo_ops_per_sec_real", 0)
        performance_loss_pct = ((baseline_ops - actual_ops) / baseline_ops) * 100 if baseline_ops > 0 else 0
        
        print(f"{'='*80}")
        print(f"Experiment: {exp['pod_name']}")
        print(f"{'='*80}")
        print(f"  Throttling: {throttle_pct:.1f}%")
        print(f"  Performance: {actual_ops:.2f} ops/sec (baseline: {baseline_ops:.2f})")
        print(f"  Performance Loss: {performance_loss_pct:.1f}%")
        print()
        
        analysis = analyze_scheduler_impact(
            throttle_pct,
            performance_loss_pct,
            exp["duration"]
        )
        
        analysis["experiment"] = exp["pod_name"]
        analysis["actual_ops_per_sec"] = actual_ops
        analysis["baseline_ops_per_sec"] = baseline_ops
        
        # Print key insights
        sched = analysis["scheduler_analysis"]
        interp = analysis["interpretation"]
        
        print(f"  Scheduler Impact ({interp['severity'].upper()}):")
        print(f"    Context Switches: {sched['estimated_context_switches']:,} " +
              f"(+{sched['throttling_induced_switches']:,} from throttling)")
        print(f"    Running Time: {sched['running_time_sec']:.1f}s / {analysis['duration_sec']}s " +
              f"({sched['scheduler_efficiency_pct']:.1f}% efficient)")
        print(f"    Throttled Time: {sched['throttled_time_sec']:.1f}s")
        print(f"    Avg Wait/Throttle: {sched['avg_wait_per_throttle_ms']:.2f}ms")
        print()
        print(f"  Interpretation:")
        print(f"    {interp['description']}")
        print(f"    {interp['amplification_note']}")
        print()
        
        analyses.append(analysis)
    
    # Write summary
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "bpf-correlation-analysis.json"
    
    with open(output_file, 'w') as f:
        json.dump({
            "analysis": "bpf-throttling-correlation",
            "description": "Scheduler behavior analysis based on CPU throttling metrics",
            "methodology": "Theoretical CFS scheduler modeling based on measured throttling",
            "experiments": analyses
        }, f, indent=2)
    
    print("=" * 80)
    print(f"Analysis complete: {output_file}")
    print("=" * 80)
    print()
    print("Key Findings:")
    print()
    
    # Summary table
    print(f"{'Config':<30} {'Throttle':<10} {'Perf Loss':<12} {'Severity':<10}")
    print("-" * 80)
    
    for analysis in analyses:
        config = analysis["experiment"]
        throttle = f"{analysis['throttle_pct']:.1f}%"
        perf_loss = f"{analysis['performance_loss_pct']:.1f}%"
        severity = analysis["interpretation"]["severity"].upper()
        
        print(f"{config:<30} {throttle:<10} {perf_loss:<12} {severity:<10}")
    
    print()
    print("Scheduler Events Traced (theoretical):")
    for event in analyses[0]["interpretation"]["scheduler_events"]:
        print(f"  • {event}")
    
    print()
    print("CFS Parameters (system defaults):")
    cfs = analyses[0]["cfs_parameters"]
    print(f"  • sched_latency: {cfs['sched_latency_ns'] / 1000000:.1f}ms")
    print(f"  • sched_min_granularity: {cfs['sched_min_granularity_ns'] / 1000000:.2f}ms")
    print(f"  • quota_period: {cfs['quota_period_us'] / 1000:.0f}ms")
    print(f"  • quota_slice: {cfs['sched_cfs_bandwidth_slice_us'] / 1000:.0f}ms")
    
    print()


if __name__ == "__main__":
    main()
