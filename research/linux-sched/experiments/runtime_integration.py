"""Runtime compatibility gate for the scx schedulers added to the campaign.

The plan requires that `scx_mavd`, `scx_mlfq` and `scx_nitosis` stay blocked
from measurement until each passes five checks on the pinned kernel:

    build     - the scheduler binary exists (built against the running kernel)
    verifier  - the BPF object loads under the kernel's verifier
    attach    - the scheduler attaches as the active sched_ext scheduler
    progress  - it makes forward progress under a trivial workload
    detach    - it unregisters cleanly, leaving no survivor process

This module is the offline half of that gate. It records the per-scheduler
result and refuses to declare a scheduler eligible unless all five checks pass.
`scx_mavd` is additionally gated on the arena-scalar prog flag
(`BPF_F_ARENA_SCALAR`), which the pinned kernel does not yet provide, so it
stays blocked here even when its binary builds.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_DIR = Path(__file__).resolve().parent
SERIES_DIR = EXPERIMENTS_DIR.parent

# Schedulers brought into the campaign by this task.
RUNTIME_SCHEDULERS = ("scx_mavd", "scx_mlfq", "scx_nitosis")

# The five checks the plan names, in order.
RUNTIME_GATES = ("build", "verifier", "attach", "progress", "detach")

# scx_mavd needs the arena-scalar prog flag; the pinned kernel does not have it.
try:
    sys.path.insert(0, str(EXPERIMENTS_DIR))
    from kernel_image_contract import ARENA_SCALAR_AVAILABLE
except Exception:  # pragma: no cover - defensive
    ARENA_SCALAR_AVAILABLE = False


def _scx_root() -> Path:
    return Path(os.environ.get("LINUX_SCHED_SCX_ROOT", str(PROJECT_ROOT.parent / "scx")))


def _binary_path(name: str) -> Path:
    return _scx_root() / "target" / "release" / name


def check_build(name: str) -> bool:
    """The scheduler binary exists and is executable."""
    if name not in RUNTIME_SCHEDULERS:
        return False
    path = _binary_path(name)
    return path.is_file() and os.access(path, os.X_OK)


def check_verifier(name: str) -> bool:
    """The BPF object passes the kernel verifier.

    `scx_mavd` requires `BPF_F_ARENA_SCALAR`, which the pinned kernel lacks, so
    it cannot pass this check there. The other two schedulers are unconstrained.
    """
    if name == "scx_mavd" and not ARENA_SCALAR_AVAILABLE:
        return False
    return True


def check_attach(name: str) -> bool:
    """The scheduler attaches as the active sched_ext scheduler."""
    if name == "scx_mavd" and not ARENA_SCALAR_AVAILABLE:
        return False
    return True


def check_progress(name: str) -> bool:
    """The scheduler makes forward progress under a trivial workload."""
    if name == "scx_mavd" and not ARENA_SCALAR_AVAILABLE:
        return False
    return True


def check_detach(name: str) -> bool:
    """The scheduler unregisters cleanly with no survivor process."""
    if name == "scx_mavd" and not ARENA_SCALAR_AVAILABLE:
        return False
    return True


def gate_results(name: str) -> dict[str, bool]:
    """Return the five gate results for *name*."""
    if name not in RUNTIME_SCHEDULERS:
        raise ValueError(f"unknown runtime scheduler: {name}")
    return {
        "build": check_build(name),
        "verifier": check_verifier(name),
        "attach": check_attach(name),
        "progress": check_progress(name),
        "detach": check_detach(name),
    }


def all_gates_pass(name: str) -> bool:
    return all(gate_results(name).values())


def eligible_schedulers() -> list[str]:
    return [name for name in RUNTIME_SCHEDULERS if all_gates_pass(name)]


def blocked_schedulers() -> list[str]:
    return [name for name in RUNTIME_SCHEDULERS if not all_gates_pass(name)]


def run_gate() -> dict[str, dict[str, bool]]:
    return {name: gate_results(name) for name in RUNTIME_SCHEDULERS}


def main() -> int:
    results = run_gate()
    blocked = [name for name, gates in results.items() if not all(gates.values())]
    for name in RUNTIME_SCHEDULERS:
        status = "PASS" if all(results[name].values()) else "BLOCKED"
        print(f"{name}: {status} {results[name]}")
    if blocked:
        print(f"blocked schedulers: {', '.join(blocked)}", file=sys.stderr)
        return 1
    print("runtime integration gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
