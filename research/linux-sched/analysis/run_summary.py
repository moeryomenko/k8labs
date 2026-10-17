#!/usr/bin/env python3
"""Record one scenario run's shape, including whether the wall-clock cap cut it short.

A truncated run and a complete one otherwise leave the same ``blocks.csv`` behind,
and the difference decides how an ``inconclusive`` verdict should be read: "the
effect is smaller than the study can see" or "the study ran out of time". The
stopping rule is time rather than data, so this record is what keeps that visible.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build(
    protocol_version: str,
    phase: str,
    scenario: str,
    first_block: int,
    last_block: int,
    completed: int,
    cap_s: int,
    elapsed_s: int,
    cap_reached: bool,
    duration_s: int,
    warmup_s: int,
    block_seconds: list[int],
) -> dict:
    return {
        "protocol_version": protocol_version,
        "phase": phase,
        "scenario": scenario,
        "blocks_first": first_block,
        "blocks_last": last_block,
        "blocks_requested": last_block - first_block + 1,
        "blocks_completed": completed,
        "wall_clock_cap_s": cap_s,
        "wall_clock_elapsed_s": elapsed_s,
        "cap_reached": cap_reached,
        "block_seconds": block_seconds,
        "duration_s": duration_s,
        "warmup_s": warmup_s,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--first", required=True, type=int)
    parser.add_argument("--last", required=True, type=int)
    parser.add_argument("--completed", required=True, type=int)
    parser.add_argument("--cap-s", required=True, type=int)
    parser.add_argument("--elapsed-s", required=True, type=int)
    parser.add_argument("--cap-reached", action="store_true")
    parser.add_argument("--duration-s", required=True, type=int)
    parser.add_argument("--warmup-s", type=int, default=0)
    parser.add_argument("--block-seconds", default="")
    args = parser.parse_args(argv)

    seconds = [int(s) for s in args.block_seconds.split(",") if s]
    payload = build(
        protocol_version=args.protocol,
        phase=args.phase,
        scenario=args.scenario,
        first_block=args.first,
        last_block=args.last,
        completed=args.completed,
        cap_s=args.cap_s,
        elapsed_s=args.elapsed_s,
        cap_reached=args.cap_reached,
        duration_s=args.duration_s,
        warmup_s=args.warmup_s,
        block_seconds=seconds,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
