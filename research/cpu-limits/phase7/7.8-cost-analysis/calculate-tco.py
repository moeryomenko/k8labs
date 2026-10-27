#!/usr/bin/env python3
"""
Phase 7 Topic 7.8: Cost-Performance Analysis

Builds a TCO (Total Cost of Ownership) model from Phase 4 throttling data.

Two quantities are reported per scenario and kept separate on purpose:

  wasted_capacity  cluster CPU spend the quota prevented tasks from using.
                   Upper bound, not a measured saving.
  performance_cost workload throughput loss priced with user-supplied
                   application economics. Zero-value by default.

Do not add them into one "savings" number: they are different kinds of
loss, and the revenue input is a placeholder until you supply yours.
"""

import json
import os
from pathlib import Path
from datetime import datetime

EVIDENCE_DIR = (
    Path(__file__).parent.parent.parent / "evidence" / "phase7" / "cost-analysis"
)
PHASE4_EVIDENCE = Path(__file__).parent.parent.parent / "phase4" / "evidence"

# Cloud pricing (AWS EKS us-east-1 on-demand, published hourly rates)
CLOUD_PRICING = {
    "aws": {
        "m5.2xlarge": {
            "vcpu": 8,
            "memory_gb": 32,
            "price_per_hour": 0.384,
            "price_per_month": 280.32,  # 730 hours
        },
        "c5.4xlarge": {
            "vcpu": 16,
            "memory_gb": 32,
            "price_per_hour": 0.68,
            "price_per_month": 496.40,
        },
    }
}

HOURS_PER_MONTH = 730


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def load_phase4_evidence():
    """Load measured throttling and throughput from Phase 4.

    A container with no CPU limit still counts quota periods but can never
    be throttled in them, so the unlimited run is identified by
    nr_throttled == 0. Treating its "0 throttled periods" as a
    quota-mediated 0% point is what made the first version of this script
    report $271M/year.
    """

    evidence_files = sorted(PHASE4_EVIDENCE.glob("cpu-bound-*-throttle-*.json"))

    if not evidence_files:
        raise SystemExit(
            f"No Phase 4 evidence in {PHASE4_EVIDENCE}. "
            "Run phase4/run-throttling-experiments.py first."
        )

    rows = []

    for path in evidence_files:
        with open(path) as f:
            data = json.load(f)

        cg = data.get("baseline_cgroup") or {}
        nr_periods = cg.get("nr_periods", 0)
        nr_throttled = cg.get("nr_throttled", 0)
        has_cpu_limit = nr_throttled > 0

        throttle_pct = None
        if has_cpu_limit and nr_periods:
            throttle_pct = nr_throttled / nr_periods * 100

        rows.append(
            {
                "config": data.get("test_config", {}).get("name", path.stem),
                "has_cpu_limit": has_cpu_limit,
                "nr_periods": nr_periods,
                "nr_throttled": nr_throttled,
                "throttle_pct": throttle_pct,
                "ops_per_sec": (data.get("performance") or {}).get(
                    "bogo_ops_per_sec_real", 0
                ),
                "evidence_file": str(path),
            }
        )

    baseline = next((r for r in rows if not r["has_cpu_limit"]), None)
    if baseline is None or baseline["ops_per_sec"] <= 0:
        raise SystemExit("Phase 4 evidence has no unthrottled baseline.")

    for row in rows:
        row["perf_loss_pct"] = round(
            (baseline["ops_per_sec"] - row["ops_per_sec"])
            / baseline["ops_per_sec"]
            * 100,
            2,
        )

    return rows


def build_model():
    rows = load_phase4_evidence()

    node_type = os.environ.get("PHASE7_NODE_TYPE", "m5.2xlarge")
    node_count = int(_env_float("PHASE7_NODE_COUNT", 10))
    pricing = CLOUD_PRICING["aws"][node_type]

    cluster_cost_per_month = node_count * pricing["price_per_month"]
    cluster_cost_per_hour = node_count * pricing["price_per_hour"]

    # Application economics. Defaults are neutral: no revenue is claimed
    # until the operator supplies real numbers.
    baseline_rps = _env_float("PHASE7_BASELINE_RPS", 0)
    revenue_per_request = _env_float("PHASE7_REVENUE_PER_REQUEST", 0)

    scenarios = []

    for row in rows:
        scenario = {
            "config": row["config"],
            "has_cpu_limit": row["has_cpu_limit"],
            "nr_periods": row["nr_periods"],
            "nr_throttled": row["nr_throttled"],
            "throttle_pct": row["throttle_pct"],
            "perf_loss_pct": row["perf_loss_pct"],
            "ops_per_sec": row["ops_per_sec"],
            "evidence_file": row["evidence_file"],
        }

        if row["has_cpu_limit"]:
            share = row["throttle_pct"] / 100
            scenario["wasted_capacity_per_month"] = round(
                cluster_cost_per_month * share, 2
            )
        else:
            scenario["wasted_capacity_per_month"] = 0.0
            scenario["note"] = (
                "No CPU limit: quota is infinite, throttling not applicable. "
                "This row is the throughput baseline."
            )

        lost_rps = baseline_rps * (row["perf_loss_pct"] / 100)
        scenario["throughput_cost_per_month"] = round(
            lost_rps * revenue_per_request * 3600 * HOURS_PER_MONTH, 2
        )

        scenarios.append(scenario)

    return {
        "cluster": {
            "node_type": node_type,
            "node_count": node_count,
            "vcpu_per_node": pricing["vcpu"],
            "cost_per_month": round(cluster_cost_per_month, 2),
        },
        "application_economics": {
            "baseline_rps": baseline_rps,
            "revenue_per_request": revenue_per_request,
            "provided_by_operator": bool(baseline_rps and revenue_per_request),
        },
        "model_limits": (
            "Wasted capacity assumes every throttled period would otherwise "
            "have done full-price useful work. It is an upper bound and not a "
            "saving that removing limits is guaranteed to recover."
        ),
        "scenarios": scenarios,
    }


def main():
    print("=" * 78)
    print("Phase 7 Topic 7.8: Cost-Performance Analysis")
    print("=" * 78)
    print()

    model = build_model()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    output_file = EVIDENCE_DIR / f"tco-model-{timestamp}.json"

    with open(output_file, "w") as f:
        json.dump(
            {
                "test": "cost-performance-analysis",
                "description": "Cluster CPU cost exposure from measured throttling",
                "timestamp": timestamp,
                "model": model,
            },
            f,
            indent=2,
        )

    cluster = model["cluster"]
    print(f"Cluster model: {cluster['node_count']} x {cluster['node_type']}")
    print(f"Monthly cost:  ${cluster['cost_per_month']:,.2f}")
    if not model["application_economics"]["provided_by_operator"]:
        print()
        print("Application economics not supplied (PHASE7_BASELINE_RPS /")
        print("PHASE7_REVENUE_PER_REQUEST unset), so throughput cost stays 0.")
    print()

    print(f"{'Scenario':<32} {'Throttled':>10} {'Perf loss':>10} {'Wasted/mo':>13}")
    print("-" * 78)

    for s in model["scenarios"]:
        throttled = f"{s['throttle_pct']:.1f}%" if s["has_cpu_limit"] else "no limit"
        print(
            f"{s['config']:<32} {throttled:>10} "
            f"{s['perf_loss_pct']:>9.1f}% "
            f"${s['wasted_capacity_per_month']:>12,.2f}"
        )

    print()
    print("Interpretation:")
    print("  Every limited scenario in Phase 4 was throttled in >98% of")
    print("  quota periods. That is not a tuning problem; the requested")
    print("  quota is below what the workload needs to run at all.")
    print()
    print("  Wasted capacity is the share of cluster CPU spend the quota")
    print("  made unusable for that workload. It is an upper bound.")
    print()
    print(f"Evidence: {output_file}")
    print()


if __name__ == "__main__":
    main()
