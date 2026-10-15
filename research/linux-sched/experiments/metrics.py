"""Primary-outcome parsers for the linux-sched workload scenarios.

Each parser turns a captured raw workload artifact (stdout of the load
generator, or the pod log of a batch job) into the single primary-outcome
number recorded in `blocks.csv`. Parsers are pure functions with no I/O and no
timestamps, so they are trivially unit-testable and deterministic.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_WRK2_P99 = re.compile(r"^\s*99\.000%\s+([0-9.]+)(us|ms|s)\b", re.MULTILINE)
_PGBENCH_TPS = re.compile(r"^tps\s*=\s*([0-9.]+)", re.MULTILINE)
_FFMPEG_FRAME = re.compile(r"frame=\s*([0-9]+)")
_FFMPEG_FPS = re.compile(r"fps=\s*([0-9.]+)")
_STRESS_BOGO = re.compile(
    r"bogo ops/s\s+\(real time\)\s*[:=]?\s*([0-9.]+)", re.IGNORECASE
)
_STRESS_BOGO_ALT = re.compile(r"^.*?([0-9.]+)\s+bogo ops/s.*$", re.MULTILINE)


def _to_ms(value: str, unit: str) -> float:
    scale = {"us": 0.001, "ms": 1.0, "s": 1000.0}[unit]
    return float(value) * scale


def parse_wrk2_p99_ms(text: str) -> float:
    """Extract the p99 latency (ms) from a wrk2 `--latency` report."""

    match = _WRK2_P99.search(text)
    if not match:
        raise ValueError("no wrk2 99.000% latency line found")
    return _to_ms(match.group(1), match.group(2))


def parse_pgbench_tps(text: str) -> float:
    """Extract transactions/s from a pgbench summary block."""

    match = _PGBENCH_TPS.search(text)
    if not match:
        raise ValueError("no 'tps =' line found in pgbench output")
    return float(match.group(1))


def parse_ffmpeg_frames_per_sec(text: str, duration_s: float) -> float:
    """Frames/s from an FFmpeg progress log over a fixed duration window."""

    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    frames = _FFMPEG_FRAME.findall(text)
    if not frames:
        raise ValueError("no ffmpeg 'frame=' progress found")
    return int(frames[-1]) / duration_s


def parse_ffmpeg_reported_fps(text: str) -> float:
    """FFmpeg's own final average fps figure (sanity cross-check)."""

    fps = _FFMPEG_FPS.findall(text)
    if not fps:
        raise ValueError("no ffmpeg 'fps=' progress found")
    return float(fps[-1])


def parse_stress_ng_bogo_ops(text: str) -> float:
    """Bogo-ops/s (real time) from a stress-ng `--metrics-brief` run."""

    match = _STRESS_BOGO.search(text)
    if match:
        return float(match.group(1))
    match = _STRESS_BOGO_ALT.search(text)
    if match:
        return float(match.group(1))
    raise ValueError("no 'bogo ops/s (real time)' figure found")


PARSERS = {
    "wrk2-p99-ms": lambda text, duration: parse_wrk2_p99_ms(text),
    "pgbench-tps": lambda text, duration: parse_pgbench_tps(text),
    "ffmpeg-fps": lambda text, duration: parse_ffmpeg_frames_per_sec(text, duration),
    "stress-ng-bogo": lambda text, duration: parse_stress_ng_bogo_ops(text),
}


def extract(metric_command: str, raw_path: Path, duration_s: float) -> float:
    """Dispatch to the parser named by a scenario's METRIC_COMMAND."""

    if metric_command not in PARSERS:
        raise ValueError(f"unknown metric command: {metric_command}")
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    return PARSERS[metric_command](text, duration_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric-command", required=True)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--duration-s", required=True, type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    value = extract(args.metric_command, args.raw, args.duration_s)
    if args.json:
        print(json.dumps({"value": value}))
    else:
        print(value)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
