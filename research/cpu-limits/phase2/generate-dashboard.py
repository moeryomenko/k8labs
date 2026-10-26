#!/usr/bin/env python3
"""
Generate a visual HTML dashboard summarizing the CPU limits research.
"""

import json
from pathlib import Path
from datetime import datetime

EVIDENCE_DIR = Path("research/cpu-limits/phase2/evidence")
OUTPUT_FILE = Path("research/cpu-limits/dashboard.html")


def load_evidence():
    """Load all evidence files."""
    evidence = []
    for f in EVIDENCE_DIR.glob("*.json"):
        if f.name == "summary.json":
            continue
        with open(f) as fp:
            evidence.append(json.load(fp))
    return sorted(evidence, key=lambda x: (x["workload_type"], x["qos_class"]))


def parse_stress_ng(logs):
    """Extract performance from stress-ng logs."""
    if not logs:
        return None
    import re

    match = re.search(
        r"cpu\s+(\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)",
        logs,
    )
    if match:
        return {
            "bogo_ops": int(match.group(1)),
            "real_time": float(match.group(2)),
            "bogo_ops_per_sec": float(match.group(5)),
        }
    return None


def generate_html(evidence):
    """Generate HTML dashboard."""

    # Analyze experiments
    results = {}
    for exp in evidence:
        wtype = exp["workload_type"]
        qos = exp["qos_class"]

        if wtype not in results:
            results[wtype] = {}

        perf = parse_stress_ng(exp.get("workload_logs"))

        if qos not in results[wtype]:
            results[wtype][qos] = []

        results[wtype][qos].append(
            {
                "duration": exp["duration_seconds"],
                "perf": perf["bogo_ops_per_sec"] if perf else 0,
                "exit_code": exp.get("workload_exit_code", 0),
            }
        )

    # Average performance by workload type and QoS
    avg_results = {}
    for wtype, qos_data in results.items():
        avg_results[wtype] = {}
        for qos, runs in qos_data.items():
            valid_perfs = [r["perf"] for r in runs if r["perf"] > 0]
            avg_results[wtype][qos] = (
                sum(valid_perfs) / len(valid_perfs) if valid_perfs else 0
            )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kubernetes CPU Limits Research - Dashboard</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: #0F172A;
            color: #F8FAFC;
            line-height: 1.6;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 3rem 2rem;
        }}
        
        header {{
            border-bottom: 1px solid #1E293B;
            padding-bottom: 2rem;
            margin-bottom: 3rem;
        }}
        
        h1 {{
            font-size: 2.5rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            color: #F8FAFC;
        }}
        
        .subtitle {{
            color: #94A3B8;
            font-size: 1.1rem;
        }}
        
        .meta {{
            display: flex;
            gap: 2rem;
            margin-top: 1rem;
            font-size: 0.9rem;
            color: #64748B;
        }}
        
        .meta-item {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .status-badge {{
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 0.25rem;
            font-size: 0.875rem;
            font-weight: 600;
            background: #22D3EE;
            color: #0F172A;
        }}
        
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }}
        
        .card {{
            background: #1E293B;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            padding: 1.5rem;
            transition: all 0.2s;
        }}
        
        .card:hover {{
            border-color: #38BDF8;
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.1);
        }}
        
        .card-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1rem;
        }}
        
        .card-title {{
            font-size: 1.25rem;
            font-weight: 600;
            color: #F8FAFC;
        }}
        
        .qos-badge {{
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        
        .qos-besteffort {{
            background: #F97316;
            color: #FFF;
        }}
        
        .qos-burstable {{
            background: #22D3EE;
            color: #0F172A;
        }}
        
        .qos-guaranteed {{
            background: #4ADE80;
            color: #0F172A;
        }}
        
        .metric {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 0;
            border-bottom: 1px solid #334155;
        }}
        
        .metric:last-child {{
            border-bottom: none;
        }}
        
        .metric-label {{
            color: #94A3B8;
            font-size: 0.875rem;
        }}
        
        .metric-value {{
            font-size: 1.5rem;
            font-weight: 700;
            color: #F8FAFC;
        }}
        
        .metric-unit {{
            font-size: 0.875rem;
            color: #64748B;
            margin-left: 0.25rem;
        }}
        
        .comparison-table {{
            background: #1E293B;
            border: 1px solid #334155;
            border-radius: 0.75rem;
            overflow: hidden;
            margin-bottom: 3rem;
        }}
        
        .table-header {{
            padding: 1.5rem;
            border-bottom: 1px solid #334155;
        }}
        
        .table-title {{
            font-size: 1.5rem;
            font-weight: 600;
            color: #F8FAFC;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        
        thead {{
            background: #334155;
        }}
        
        th {{
            padding: 1rem 1.5rem;
            text-align: left;
            font-size: 0.875rem;
            font-weight: 600;
            color: #F8FAFC;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        
        tbody tr {{
            border-bottom: 1px solid #334155;
        }}
        
        tbody tr:last-child {{
            border-bottom: none;
        }}
        
        tbody tr:hover {{
            background: #2D3748;
        }}
        
        td {{
            padding: 1rem 1.5rem;
            color: #E2E8F0;
        }}
        
        .perf-bar {{
            width: 100%;
            height: 1.5rem;
            background: #334155;
            border-radius: 0.25rem;
            overflow: hidden;
            position: relative;
        }}
        
        .perf-fill {{
            height: 100%;
            background: linear-gradient(90deg, #22D3EE, #38BDF8);
            transition: width 0.3s ease;
        }}
        
        .perf-text {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 0.75rem;
            font-weight: 600;
            color: #F8FAFC;
            text-shadow: 0 1px 2px rgba(0,0,0,0.5);
        }}
        
        .key-findings {{
            background: #1E293B;
            border: 1px solid #334155;
            border-left: 4px solid #22D3EE;
            border-radius: 0.5rem;
            padding: 1.5rem;
            margin-bottom: 3rem;
        }}
        
        .findings-title {{
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: #22D3EE;
        }}
        
        .findings-list {{
            list-style: none;
            padding: 0;
        }}
        
        .findings-list li {{
            padding: 0.5rem 0;
            padding-left: 1.5rem;
            position: relative;
        }}
        
        .findings-list li::before {{
            content: "→";
            position: absolute;
            left: 0;
            color: #22D3EE;
        }}
        
        footer {{
            margin-top: 4rem;
            padding-top: 2rem;
            border-top: 1px solid #1E293B;
            text-align: center;
            color: #64748B;
            font-size: 0.875rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Kubernetes CPU Limits Research</h1>
            <p class="subtitle">Comprehensive analysis of CPU requests, limits, and QoS classes</p>
            <div class="meta">
                <div class="meta-item">
                    <span class="status-badge">COMPLETE</span>
                </div>
                <div class="meta-item">
                    <strong>Experiments:</strong> {len(evidence)}
                </div>
                <div class="meta-item">
                    <strong>Generated:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M")}
                </div>
            </div>
        </header>
        
        <div class="key-findings">
            <div class="findings-title">🔍 Key Findings</div>
            <ul class="findings-list">
                <li><strong>BestEffort QoS</strong> achieved highest performance (98.8 ops/sec) by utilizing all available CPU</li>
                <li><strong>Burstable and Guaranteed</strong> showed similar performance (~51 ops/sec) when limited to 2 cores</li>
                <li><strong>No CPU throttling</strong> observed across all experiments - limits were appropriately sized</li>
                <li><strong>Setting limits</strong> reduces performance even when cluster has spare capacity</li>
            </ul>
        </div>
"""

    # Add performance comparison table
    html += f"""
        <div class="comparison-table">
            <div class="table-header">
                <div class="table-title">Performance by Workload Type and QoS Class</div>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Workload Type</th>
                        <th>QoS Class</th>
                        <th>Avg Performance</th>
                        <th>Visual Comparison</th>
                    </tr>
                </thead>
                <tbody>
"""

    max_perf = max(
        [max(qos_data.values()) for qos_data in avg_results.values() if qos_data]
    )

    for wtype in sorted(avg_results.keys()):
        qos_data = avg_results[wtype]
        for qos in ["besteffort", "burstable", "guaranteed"]:
            if qos in qos_data:
                perf = qos_data[qos]
                pct = (perf / max_perf) * 100 if max_perf > 0 else 0
                qos_class = f"qos-{qos}"

                html += f"""
                    <tr>
                        <td><strong>{wtype}</strong></td>
                        <td><span class="qos-badge {qos_class}">{qos}</span></td>
                        <td><strong>{perf:.1f}</strong> ops/sec</td>
                        <td>
                            <div class="perf-bar">
                                <div class="perf-fill" style="width: {pct}%"></div>
                                <div class="perf-text">{pct:.0f}%</div>
                            </div>
                        </td>
                    </tr>
"""

    html += """
                </tbody>
            </table>
        </div>
"""

    # Add individual experiment cards
    html += '<div class="grid">'

    for exp in evidence:
        wtype = exp["workload_type"]
        qos = exp["qos_class"]
        perf = parse_stress_ng(exp.get("workload_logs"))

        qos_class = f"qos-{qos}"

        html += f"""
            <div class="card">
                <div class="card-header">
                    <div class="card-title">{wtype.upper()}</div>
                    <span class="qos-badge {qos_class}">{qos}</span>
                </div>
"""

        if perf:
            html += f"""
                <div class="metric">
                    <span class="metric-label">Performance</span>
                    <span class="metric-value">{perf["bogo_ops_per_sec"]:.1f}<span class="metric-unit">ops/sec</span></span>
                </div>
                <div class="metric">
                    <span class="metric-label">Bogo Operations</span>
                    <span class="metric-value">{perf["bogo_ops"]}<span class="metric-unit">ops</span></span>
                </div>
"""

        html += f"""
                <div class="metric">
                    <span class="metric-label">Duration</span>
                    <span class="metric-value">{exp["duration_seconds"]:.1f}<span class="metric-unit">sec</span></span>
                </div>
                <div class="metric">
                    <span class="metric-label">Exit Code</span>
                    <span class="metric-value">{exp.get("workload_exit_code", 0)}</span>
                </div>
            </div>
"""

    html += "</div>"

    # Footer
    html += f"""
        <footer>
            <p>Full analysis available in <code>research/cpu-limits/GUIDE.md</code></p>
            <p>Evidence files: <code>research/cpu-limits/phase2/evidence/</code></p>
        </footer>
    </div>
</body>
</html>
"""

    return html


def main():
    print("Loading evidence...")
    evidence = load_evidence()
    print(f"Found {len(evidence)} experiments")

    print("Generating dashboard...")
    html = generate_html(evidence)

    with open(OUTPUT_FILE, "w") as f:
        f.write(html)

    print(f"\n✓ Dashboard written to: {OUTPUT_FILE}")
    print(f"  Open in browser: file://{OUTPUT_FILE.absolute()}")


if __name__ == "__main__":
    main()
