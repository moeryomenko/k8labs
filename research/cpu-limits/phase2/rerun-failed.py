#!/usr/bin/env python3
"""
Re-run specific experiments that failed or had too few samples.

Usage:
  python3 rerun-failed.py [workload-name...]

Examples:
  python3 rerun-failed.py cpu-bound-guaranteed
  python3 rerun-failed.py cpu-bound-guaranteed io-bound-guaranteed
"""

import sys
import subprocess
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 rerun-failed.py [workload-name...]")
        print("\nAvailable workloads:")
        workload_dir = Path(__file__).parent / "workloads"
        for f in sorted(workload_dir.glob("*.yaml")):
            print(f"  - {f.stem}")
        sys.exit(1)

    workloads = sys.argv[1:]

    for workload in workloads:
        workload_file = Path(__file__).parent / "workloads" / f"{workload}.yaml"
        if not workload_file.exists():
            print(f"❌ Workload not found: {workload}")
            continue

        print(f"\n🔄 Re-running {workload}...")
        cmd = [
            "python3",
            str(Path(__file__).parent / "run-experiments.py"),
            str(workload_file),
        ]
        subprocess.run(cmd, check=True)

    print("\n✅ Re-runs complete!")


if __name__ == "__main__":
    main()
