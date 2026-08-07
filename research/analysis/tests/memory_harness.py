#!/usr/bin/env python3
"""RSS measurement wrapper for the memory-bound tests (test_memory_bounds.py).

Usage:
    python3 memory_harness.py <stage-script> [stage argv...]

The stage script (dist-analyze.py / dist-plot.py / dist-gif.py / dist-steps.py)
is executed IN-PROCESS via ``runpy.run_path(..., run_name="__main__")`` so that
the measured peak RSS is the stage's own peak resident set size (plus a
constant ~15-25 MB interpreter overhead).

REVISION (authorized harness fix): the stage now runs in a GRANDCHILD
process so the gate measures the STAGE, not the fixture build.  The wrapper
(the pytest subprocess) forks a monitor whose ONLY job is to fork the stage
and wait; the monitor reports
``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` — the stage's own peak.

Why this is necessary (Linux ``ru_maxrss`` semantics): the counter lives in
``struct signal`` and is inherited in a surprising way across fork+exec:

  * ``copy_signal`` ZEROES a fresh fork child's ``signal->maxrss``
    (``kmem_cache_zalloc``), so a plain fork starts with a clean counter;
  * but ``dup_mm`` seeds the child's ``mm->hiwater_rss`` with the parent's
    CURRENT RSS, and ``exec_mmap`` folds that into the new program's
    ``signal->maxrss`` before the new mm starts.  So the old design — a
    subprocess that execs and runs the stage in-process — inherited the
    pytest parent's current RSS.  The session-scoped 10M-row x 3-replicate
    gif fixture leaves the pytest parent at ~3.07 GiB at fork time, so the
    gate measured fixture generation, not the stage.

By forking a monitor (freshly zeroed ``signal``) and measuring the stage as
the monitor's CHILD via ``RUSAGE_CHILDREN``, no inherited counter is ever
read: the monitor's ``cmaxrss`` accumulates only the stage's own exit-time
maxrss (``wait_task_zombie`` folds the child's own ``mm->hiwater_rss``).
The fixture-build peak in the pytest parent becomes irrelevant to every gate.

The wrapper:

  * propagates the stage's exit code (``SystemExit`` from
    ``sys.exit(main())``; signal deaths map to 128+signum),
  * prints ``PEAK_RSS_KB=<value>`` as the LAST stdout line so the pytest
    harness can parse the child's peak RSS unambiguously (the monitor
    flushes explicitly — ``os._exit`` skips stdio flushes),
  * normalizes the platform unit (Linux ru_maxrss is KiB; macOS reports
    bytes).
"""

from __future__ import annotations

import argparse
import os
import resource
import runpy
import sys


def _exit_code(status: int) -> int:
    """Decode a waitpid(2) status to a conventional process exit code."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)  # signal death, bash convention


def _monitor(args: argparse.Namespace) -> int:
    """Fork the stage, wait for it, and report its own peak RSS.

    Runs in the monitor process (child of the wrapper, parent of the stage).
    The monitor's ``signal`` struct was freshly zeroed at its own fork, so
    ``RUSAGE_CHILDREN.ru_maxrss`` reflects only the stage's own peak (see the
    module docstring REVISION note).
    """
    stage_pid = os.fork()
    if stage_pid == 0:
        # -- stage (grandchild of the wrapper) --
        sys.argv = [args.script, *args.argv]
        # A stage calling sys.exit(main()) raises SystemExit; letting it
        # propagate gives the stage its own normal interpreter shutdown
        # (atexit handlers run) and exits with the stage's code.
        runpy.run_path(args.script, run_name="__main__")
        os._exit(0)  # stage returned without sys.exit

    _, status = os.waitpid(stage_pid, 0)
    peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if sys.platform == "darwin":
        peak_kb //= 1024  # macOS reports bytes
    print(f"PEAK_RSS_KB={peak_kb}", flush=True)
    return _exit_code(status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an analysis stage in a grandchild process and report "
        "the stage's own peak RSS."
    )
    parser.add_argument("script", help="path to the stage script to run")
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="stage arguments")
    args = parser.parse_args(argv)

    monitor_pid = os.fork()
    if monitor_pid == 0:
        # -- monitor (child of the wrapper, parent of the stage) --
        os._exit(_monitor(args))

    # -- wrapper (the pytest subprocess) --
    _, status = os.waitpid(monitor_pid, 0)
    return _exit_code(status)


if __name__ == "__main__":
    sys.exit(main())
