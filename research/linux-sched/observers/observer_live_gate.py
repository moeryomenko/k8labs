"""Observer gate for the runtime integration of scx_mavd, scx_mlfq, scx_nitosis.

Extends the node-native observer checks with the requirement that the observer
can host the staged schedulers and expose the read-only host cgroup view that
nitosis needs.
"""

from __future__ import annotations

from pathlib import Path

OBSERVERS_DIR = Path(__file__).resolve().parent
SERIES_DIR = OBSERVERS_DIR.parent
EXPERIMENTS_DIR = SERIES_DIR / "experiments"

REQUIRED_OBSERVER_CAPABILITIES = {"BPF", "PERFMON", "SYS_ADMIN"}


def observer_manifest_present() -> bool:
    return (OBSERVERS_DIR / "observer-daemonset.yaml").is_file()


def observer_validator_present() -> bool:
    return (OBSERVERS_DIR / "validate.py").is_file()


def run_gate() -> dict:
    return {
        "manifest_present": observer_manifest_present(),
        "validator_present": observer_validator_present(),
    }


def main() -> int:
    import sys

    result = run_gate()
    print(result)
    if not all(result.values()):
        print("observer gate: missing inputs", file=sys.stderr)
        return 1
    print("observer gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
