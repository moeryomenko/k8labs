#!/usr/bin/env python3
"""Bound the linux-sched data root without losing the primary records.

The plan asks for persistent *bounded* storage. A sample carries hundreds of
megabytes of raw trace and `perf.data`, so a confirmatory run over several
scenarios reaches tens of gigabytes; nothing pruned any of it.

The policy is deliberately conservative, and it never touches the records a
result is built from:

* ``blocks.csv`` and ``analysis/`` are never removed — they are the result.
* Every block keeps its ``metadata.json``, per-arm load output, pod log and
  extracted metric, plus the run-queue histograms, which are small and are what
  the scheduler-behaviour plots are drawn from.
* Only the large raw evidence is dropped, and only from blocks older than the
  newest ``--keep-blocks`` per scenario: ``perf.data*`` and ``sched-trace*``.

Reporting is the default; ``--apply`` is required to delete anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Large, reproducible-from-the-probe evidence. The run-queue histograms are not
# here on purpose: they are kilobytes and the plots read them.
BULK_PATTERNS = ("perf.data*", "sched-trace.txt*")
NEVER_REMOVE = ("blocks.csv", "analysis")


def block_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("block-")
    try:
        return (int(suffix), path.name)
    except ValueError:
        return (10**9, path.name)


def bulk_files(block_dir: Path) -> list[Path]:
    """Large evidence anywhere under the block, including arm subdirectories."""

    found: list[Path] = []
    for pattern in BULK_PATTERNS:
        found.extend(sorted(p for p in block_dir.rglob(pattern) if p.is_file()))
    return found


def plan_pruning(data_root: Path, keep_blocks: int) -> tuple[list[Path], int, int]:
    """Return (files to remove, bytes removable, bytes retained-in-blocks)."""

    removable: list[Path] = []
    removable_bytes = 0
    kept_bytes = 0
    if not data_root.is_dir():
        raise FileNotFoundError(f"no data root at {data_root}")

    for scenario_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        if scenario_dir.name in NEVER_REMOVE:
            continue
        blocks = sorted(
            (p for p in scenario_dir.glob("block-*") if p.is_dir()),
            key=block_sort_key,
        )
        keep = set(blocks[-keep_blocks:]) if keep_blocks > 0 else set()
        for block_dir in blocks:
            candidates = bulk_files(block_dir) if block_dir not in keep else []
            remove_here = {p for p in candidates}
            for path in block_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path in remove_here:
                    removable.append(path)
                    removable_bytes += path.stat().st_size
                else:
                    kept_bytes += path.stat().st_size
    return removable, removable_bytes, kept_bytes


def human(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--keep-blocks",
        type=int,
        default=5,
        help="newest complete blocks per scenario to keep in full (default 5)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually delete; default is a report"
    )
    args = parser.parse_args(argv)

    removable, removable_bytes, kept_bytes = plan_pruning(
        args.data_root, args.keep_blocks
    )
    total = removable_bytes + kept_bytes
    print(
        f"data root: {args.data_root}\n"
        f"  total            {human(total)}\n"
        f"  retained in blocks {human(kept_bytes)}\n"
        f"  prunable (bulk)  {human(removable_bytes)} in {len(removable)} file(s)"
    )
    if not args.apply:
        print("report only; pass --apply to remove the prunable files")
        return 0

    removed = 0
    for path in removable:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    print(f"removed {removed} file(s), freeing about {human(removable_bytes)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
