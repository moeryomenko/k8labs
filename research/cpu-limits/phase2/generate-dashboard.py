#!/usr/bin/env python3
"""
Generate HTML visualization of CPU throttling experiments.

Creates an interactive dashboard showing:
- Throttling time series for each experiment
- QoS class comparison
- Workload type analysis
"""

import json
from pathlib import Path
from typing import Optional
import sys


def generate_throttling_chart(experiment_data: dict) -> str:
    """Generate Chart.js configuration for throttling time series."""
    samples = experiment_data.get("cgroup_samples", [])
    if not samples:
        return ""

    # Extract time series data
    timestamps = []
    throttle_counts = []
    throttle_times = []

    prev_throttled = 0
    prev_throttled_usec = 0

    for sample in samples:
        if "cpu_stat" not in sample or not sample["cpu_stat"]:
            continue

        # Parse cpu_stat
        cpu_stat = {}
        for line in sample["cpu_stat"].split("\\n"):
            parts = line.strip().split()
            if len(parts) == 2:
                cpu_stat[parts[0]] = int(parts[1])

        nr_throttled = cpu_stat.get("nr_throttled", 0)
        throttled_usec = cpu_stat.get("throttled_usec", 0)

        # Calculate delta from previous sample
        delta_throttled = nr_throttled - prev_throttled
        delta_usec = throttled_usec - prev_throttled_usec

        timestamps.append(sample["timestamp"].split("T")[1][:8])
        throttle_counts.append(delta_throttled)
        throttle_times.append(delta_usec / 1000)  # Convert to ms

        prev_throttled = nr_throttled
        prev_throttled_usec = throttled_usec

    # Generate Chart.js config
    return f"""
    {{
        type: 'line',
        data: {{
            labels: {json.dumps(timestamps)},
            datasets: [
                {{
                    label: 'Throttle Events',
                    data: {json.dumps(throttle_counts)},
                    borderColor: '#F97316',
                    backgroundColor: 'rgba(249, 115, 22, 0.1)',
                    yAxisID: 'y'
                }},
                {{
                    label: 'Throttled Time (ms)',
                    data: {json.dumps(throttle_times)},
                    borderColor: '#22D3EE',
                    backgroundColor: 'rgba(34, 211, 238, 0.1)',
                    yAxisID: 'y1'
                }}
            ]
        }},
        options: {{
            responsive: true,
            interaction: {{
                mode: 'index',
                intersect: false
            }},
            plugins: {{
                title: {{
                    display: true,
                    text: '{experiment_data["workload_name"]} ({experiment_data["qos_class"]})'
                }}
            }},
            scales: {{
                y: {{
                    type: 'linear',
                    display: true,
                    position: 'left',
                    title: {{
                        display: true,
                        text: 'Throttle Events'
                    }}
                }},
                y1: {{
                    type: 'linear',
                    display: true,
                    position: 'right',
                    title: {{
                        display: true,
                        text: 'Throttled Time (ms)'
                    }},
                    grid: {{
                        drawOnChartArea: false
                    }}
                }}
            }}
        }}
    }}
    """


def generate_html(evidence_dir: Path) -> str:
    """Generate complete HTML dashboard."""
    experiments = []

    for evidence_file in sorted(evidence_dir.glob("*.json")):
        if evidence_file.name == "summary.json":
            continue

        with open(evidence_file) as f:
            data = json.load(f)

        if data.get("cgroup_samples"):
            experiments.append(data)

    # Generate HTML
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CPU Limits Research — Phase 2 Results</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0F172A;
            color: #F8FAFC;
            padding: 2rem;
            line-height: 1.6;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
        }

        h1 {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            background: linear-gradient(135deg, #38BDF8 0%, #22D3EE 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .subtitle {
            color: #94A3B8;
            margin-bottom: 2rem;
            font-size: 0.95rem;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(600px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        .card {
            background: #1E293B;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 1.5rem;
        }

        .card h2 {
            font-size: 1.25rem;
            margin-bottom: 1rem;
            color: #38BDF8;
        }

        .chart-container {
            position: relative;
            height: 300px;
        }

        .stats {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1rem;
            margin-top: 1rem;
        }

        .stat {
            text-align: center;
            padding: 1rem;
            background: #0F172A;
            border-radius: 8px;
        }

        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            color: #F97316;
        }

        .stat-label {
            font-size: 0.85rem;
            color: #94A3B8;
            margin-top: 0.25rem;
        }

        .badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .badge-besteffort { background: #334155; color: #94A3B8; }
        .badge-burstable { background: rgba(249, 115, 22, 0.2); color: #F97316; }
        .badge-guaranteed { background: rgba(34, 211, 238, 0.2); color: #22D3EE; }
    </style>
</head>
<body>
    <div class="container">
        <h1>CPU Limits Research — Phase 2</h1>
        <p class="subtitle">Empirical analysis of Kubernetes CPU throttling behavior across workload types and QoS classes</p>

        <div class="grid">
"""

    # Add chart for each experiment
    for i, experiment in enumerate(experiments):
        qos_class = experiment["qos_class"]
        badge_class = f"badge-{qos_class}"

        # Calculate summary stats
        samples = experiment.get("cgroup_samples", [])
        if len(samples) > 1:
            first = samples[0]
            last = samples[-1]

            # Parse first and last cpu_stat
            def parse_cpu_stat(sample):
                cpu_stat = {}
                for line in sample.get("cpu_stat", "").split("\\n"):
                    parts = line.strip().split()
                    if len(parts) == 2:
                        cpu_stat[parts[0]] = int(parts[1])
                return cpu_stat

            first_stat = parse_cpu_stat(first)
            last_stat = parse_cpu_stat(last)

            total_throttled = last_stat.get("nr_throttled", 0) - first_stat.get(
                "nr_throttled", 0
            )
            total_throttled_ms = (
                last_stat.get("throttled_usec", 0) - first_stat.get("throttled_usec", 0)
            ) / 1000
            throttle_ratio = (
                total_throttled / last_stat.get("nr_periods", 1) * 100
                if last_stat.get("nr_periods", 0) > 0
                else 0
            )
        else:
            total_throttled = 0
            total_throttled_ms = 0
            throttle_ratio = 0

        html += f"""
            <div class="card">
                <h2>
                    {experiment["workload_type"]}
                    <span class="badge {badge_class}">{qos_class}</span>
                </h2>
                <div class="chart-container">
                    <canvas id="chart{i}"></canvas>
                </div>
                <div class="stats">
                    <div class="stat">
                        <div class="stat-value">{total_throttled}</div>
                        <div class="stat-label">Throttle Events</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{total_throttled_ms / 1000:.1f}s</div>
                        <div class="stat-label">Throttled Time</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value">{throttle_ratio:.1f}%</div>
                        <div class="stat-label">Throttle Ratio</div>
                    </div>
                </div>
            </div>
"""

    html += """
        </div>
    </div>

    <script>
"""

    # Add Chart.js initialization for each experiment
    for i, experiment in enumerate(experiments):
        chart_config = generate_throttling_chart(experiment)
        html += f"""
        new Chart(document.getElementById('chart{i}'), {chart_config});
"""

    html += """
    </script>
</body>
</html>
"""

    return html


def main():
    """Main entry point."""
    evidence_dir = Path("research/cpu-limits/phase2/evidence")
    output_file = Path("research/cpu-limits/phase2/dashboard.html")

    if not evidence_dir.exists():
        print(f"Evidence directory not found: {evidence_dir}")
        sys.exit(1)

    print(f"Generating dashboard from {evidence_dir}...")
    html = generate_html(evidence_dir)

    with open(output_file, "w") as f:
        f.write(html)

    print(f"✓ Dashboard written to {output_file}")
    print(f"  Open in browser: file://{output_file.absolute()}")


if __name__ == "__main__":
    main()
