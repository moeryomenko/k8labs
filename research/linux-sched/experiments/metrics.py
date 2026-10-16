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
# One progress line: the frame count and the encoded position, in that order,
# as ffmpeg prints them to stderr.
_FFMPEG_PROGRESS = re.compile(r"frame=\s*([0-9]+).*?time=(\d+):(\d+):(\d+(?:\.\d+)?)")
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


def parse_ffmpeg_fps_window(
    text: str, duration_s: float, warmup_s: float = 0.0
) -> float:
    """Frames/s over the measured interval of a longer FFmpeg run.

    A co-located batch job outlives the measurement window by the warmup, so the
    whole-run figure would average in the warmup. This takes the last frame count
    at or before the warmup boundary and divides the frames produced after it by
    the measured duration, which is what the mixed scenario reports as its
    secondary outcome.
    """

    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    progress = [
        (int(frames), int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        for frames, hours, minutes, seconds in _FFMPEG_PROGRESS.findall(text)
    ]
    if not progress:
        raise ValueError("no ffmpeg progress lines with frame= and time= found")
    at_warmup = [frames for frames, position in progress if position <= warmup_s]
    start_frames = at_warmup[-1] if at_warmup else 0
    end_frames = max(frames for frames, _ in progress)
    produced = end_frames - start_frames
    if produced < 0:
        raise ValueError("ffmpeg frame count went backwards")
    return produced / duration_s


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
    "wrk2-p99-ms": lambda text, duration, warmup: parse_wrk2_p99_ms(text),
    "pgbench-tps": lambda text, duration, warmup: parse_pgbench_tps(text),
    "ffmpeg-fps": lambda text, duration, warmup: parse_ffmpeg_frames_per_sec(
        text, duration
    ),
    "ffmpeg-fps-window": parse_ffmpeg_fps_window,
    "stress-ng-bogo": lambda text, duration, warmup: parse_stress_ng_bogo_ops(text),
}


def extract(
    metric_command: str, raw_path: Path, duration_s: float, warmup_s: float = 0.0
) -> float:
    """Dispatch to the parser named by a scenario's METRIC_COMMAND."""

    if metric_command not in PARSERS:
        raise ValueError(f"unknown metric command: {metric_command}")
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    return PARSERS[metric_command](text, duration_s, warmup_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric-command", required=True)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--duration-s", required=True, type=float)
    parser.add_argument("--warmup-s", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    value = extract(args.metric_command, args.raw, args.duration_s, args.warmup_s)
    if args.json:
        print(json.dumps({"value": value}))
    else:
        print(value)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
