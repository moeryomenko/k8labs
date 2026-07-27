#!/usr/bin/env python3
"""plot-throttling.py — Generate visualizations from experiment data.

Usage: python3 plot-throttling.py <aggregates.csv> [--output-dir path]

Generates PNG plots:
  - throttling-vs-limit.png
  - throttled-time-vs-ratio.png
  - latency-interference.png (placeholder)
  - usage-vs-limit.png
"""

import sys
import os
import csv
import json
from collections import defaultdict

# Check imports gracefully
MISSING_IMPORTS = []
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
except ImportError:
    MISSING_IMPORTS.append('matplotlib')

try:
    import numpy as np
except ImportError:
    MISSING_IMPORTS.append('numpy')

try:
    import pandas as pd
except ImportError:
    MISSING_IMPORTS.append('pandas')

if MISSING_IMPORTS:
    print("Missing required Python packages: " + ", ".join(MISSING_IMPORTS))
    print("Install with: pip install " + " ".join(MISSING_IMPORTS))
    sys.exit(1)


def parse_aggregates(filepath):
    """Parse aggregates CSV into a pandas DataFrame."""
    df = pd.read_csv(filepath)

    # Parse config_cell into request and limit columns
    def parse_cell(cell):
        parts = cell.replace(' ', '').split(';')
        req = ''
        lim = ''
        for p in parts:
            if p.startswith('request='):
                req = p.replace('request=', '')
            elif p.startswith('limit='):
                lim = p.replace('limit=', '')
            elif p.startswith('ls_request='):
                req = p.replace('ls_request=', '')
            elif p.startswith('ls_limit='):
                lim = p.replace('ls_limit=', '')
        # Convert to numeric (millicores), handle empty
        req_m = int(req.replace('m', '')) if req else 0
        lim_m = int(lim.replace('m', '')) if lim else 0
        return req_m, lim_m

    parsed = df['config_cell'].apply(parse_cell)
    df['request_m'] = parsed.apply(lambda x: x[0])
    df['limit_m'] = parsed.apply(lambda x: x[1])

    # Compute ratio (avoid division by zero)
    df['request_limit_ratio'] = df.apply(
        lambda row: row['request_m'] / row['limit_m'] if row['limit_m'] > 0 else 0,
        axis=1
    )

    return df


def plot_throttling_vs_limit(df, output_dir):
    """Plot 1: CPU limit vs throttling ratio."""
    if df['limit_m'].max() == 0:
        print("  SKIP: throttling-vs-limit — no limit values in data")
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    for req_val in sorted(df['request_m'].unique()):
        subset = df[df['request_m'] == req_val]
        if subset.empty:
            continue
        label = f"request={req_val}m" if req_val > 0 else "no request"
        ax.scatter(subset['limit_m'], subset['mean_throttling_ratio'],
                   label=label, s=80, alpha=0.7)

    ax.set_xlabel('CPU Limit (millicores)')
    ax.set_ylabel('Throttling Ratio (nr_throttled / nr_periods)')
    ax.set_title('CPU Throttling Ratio vs CPU Limit')
    ax.legend()
    ax.grid(True, alpha=0.3)

    path = os.path.join(output_dir, 'throttling-vs-limit.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Created: {path}")


def plot_throttled_time_vs_ratio(df, output_dir):
    """Plot 2: Request/Limit ratio vs throttled time ratio."""
    if df['limit_m'].max() == 0:
        print("  SKIP: throttled-time-vs-ratio — no limit values in data")
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    subset = df[df['request_limit_ratio'] > 0]
    if subset.empty:
        print("  SKIP: throttled-time-vs-ratio — no request/limit ratio data")
        plt.close()
        return

    ax.scatter(subset['request_limit_ratio'], subset['mean_throttled_time_ratio'],
               c='steelblue', s=80, alpha=0.7)

    ax.set_xlabel('Request/Limit Ratio')
    ax.set_ylabel('Throttled Time Ratio')
    ax.set_title('Throttled Time vs Request/Limit Ratio')
    ax.grid(True, alpha=0.3)

    path = os.path.join(output_dir, 'throttled-time-vs-ratio.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Created: {path}")


def plot_latency_interference(df, output_dir):
    """Plot 3: Co-located interference — placeholder for now."""
    # This requires time-series data from the co-located experiment
    # which includes HTTP latency samples over time.
    # For now, create a placeholder if co-located data exists.

    colocated = df[df['experiment'] == 'co-located']
    if colocated.empty:
        print("  SKIP: latency-interference — no co-located experiment data")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.text(0.5, 0.5,
            'Co-located interference plot\n(requires time-series latency data)\n\n'
            'Run co-located experiment with HTTP latency\n'
            'monitoring enabled to populate this plot.',
            horizontalalignment='center', verticalalignment='center',
            transform=ax.transAxes, fontsize=12)
    ax.set_title('HTTP P99 Latency Under Batch Interference')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('P99 Latency (ms)')

    path = os.path.join(output_dir, 'latency-interference.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Created: {path}")


def plot_usage_vs_limit(df, output_dir):
    """Plot 4: CPU limit vs actual usage with throttling overlay."""
    if df['limit_m'].max() == 0:
        print("  SKIP: usage-vs-limit — no limit values in data")
        return

    fig, ax1 = plt.subplots(figsize=(10, 6))

    # Bar chart: usage by limit
    for req_val in sorted(df['request_m'].unique()):
        subset = df[df['request_m'] == req_val]
        if subset.empty:
            continue
        label = f"request={req_val}m" if req_val > 0 else "no request"
        ax1.bar(subset['limit_m'] + (req_val / 100), subset['mean_usage_usec'] / 1000,
                width=80, label=label, alpha=0.6)

    ax1.set_xlabel('CPU Limit (millicores)')
    ax1.set_ylabel('CPU Usage (normalized)')
    ax1.set_title('CPU Usage vs Limit')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)

    path = os.path.join(output_dir, 'usage-vs-limit.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Created: {path}")


def main():
    if len(sys.argv) < 2:
        print("No data file provided. Usage: python3 plot-throttling.py <aggregates.csv> [--output-dir path]")
        sys.exit(1)

    csv_file = sys.argv[1]
    if not os.path.isfile(csv_file):
        print(f"File not found: {csv_file}")
        sys.exit(1)

    output_dir = './analysis-output'
    if '--output-dir' in sys.argv:
        idx = sys.argv.index('--output-dir')
        if idx + 1 < len(sys.argv):
            output_dir = sys.argv[idx + 1]

    os.makedirs(output_dir, exist_ok=True)

    print("Loading data...")
    df = parse_aggregates(csv_file)
    print(f"  Loaded {len(df)} config cells from {csv_file}")

    print("Generating plots...")
    plot_throttling_vs_limit(df, output_dir)
    plot_throttled_time_vs_ratio(df, output_dir)
    plot_latency_interference(df, output_dir)
    plot_usage_vs_limit(df, output_dir)

    print(f"\nAll plots generated in: {output_dir}/")
    print("Files:")
    for f in sorted(os.listdir(output_dir)):
        if f.endswith('.png'):
            print(f"  {f}")


if __name__ == '__main__':
    main()
