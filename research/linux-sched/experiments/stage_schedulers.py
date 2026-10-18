"""Staging gate for the sched_ext binaries the campaign runs.

Staging copies the built schedulers into the observer Pod on every research
worker. This gate checks, offline, that the staging inputs (the three new
schedulers and the staging script) are present and consistent, so a missing
binary is caught before an experiment starts.
"""

from __future__ import annotations

import os
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).resolve().parent
SERIES_DIR = EXPERIMENTS_DIR.parent
PROJECT_ROOT = SERIES_DIR.parents[1]

STAGED_SCHEDULERS = ("scx_mavd", "scx_mlfq", "scx_nitosis")
STAGING_SCRIPT = EXPERIMENTS_DIR / "stage-scx.sh"


def staging_script_present() -> bool:
    return STAGING_SCRIPT.is_file()


def binary_present(name: str) -> bool:
    scx_root = Path(os.environ.get("LINUX_SCHED_SCX_ROOT", str(PROJECT_ROOT.parent / "scx")))
    path = scx_root / "target" / "release" / name
    return path.is_file()


def all_staged() -> bool:
    return staging_script_present() and all(binary_present(n) for n in STAGED_SCHEDULERS)


def run_gate() -> dict:
    return {
        "staging_script": staging_script_present(),
        "binaries": {name: binary_present(name) for name in STAGED_SCHEDULERS},
    }


def main() -> int:
    import sys

    result = run_gate()
    print(result)
    if not all_staged():
        print("staging gate: missing inputs", file=sys.stderr)
        return 1
    print("staging gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
