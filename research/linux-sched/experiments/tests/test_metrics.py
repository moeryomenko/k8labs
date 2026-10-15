"""Unit tests for the linux-sched primary-outcome parsers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import (  # noqa: E402
    extract,
    parse_ffmpeg_frames_per_sec,
    parse_ffmpeg_reported_fps,
    parse_pgbench_tps,
    parse_stress_ng_bogo_ops,
    parse_wrk2_p99_ms,
)

WRK2 = """
Running 3m test @ http://nginx.default.svc/
  2 threads and 64 connections
  Latency Distribution (HdrHistogram - Recorded Latency)
 50.000%  512.00us
 75.000%    1.40ms
 90.000%    2.10ms
 99.000%    4.25ms
100.000%   12.00ms
"""

PGBENCH = """
transaction type: /fixtures/transactions.sql
scaling factor: 1
query mode: simple
number of clients: 8
duration: 180 s
latency average = 3.21 ms
tps = 2490.123456 (without initial connection time)
"""

FFMPEG = """
frame=  120 fps= 30 q=28.0 size=N/A time=00:00:04.00 bitrate=N/A speed=1.0x
frame= 5400 fps= 30 q=28.0 size=N/A time=00:03:00.00 bitrate=N/A speed=1.0x
"""

STRESS = """
stress-ng: info:  [123] matrixprod: 32768 bogo ops in 180.00 sec
stress-ng: info:  [123] matrixprod:  182.04 bogo ops/s (real time)
"""


def test_wrk2_p99_ms() -> None:
    assert parse_wrk2_p99_ms(WRK2) == pytest.approx(4.25)


def test_wrk2_missing_raises() -> None:
    with pytest.raises(ValueError):
        parse_wrk2_p99_ms("no distribution here")


def test_pgbench_tps() -> None:
    assert parse_pgbench_tps(PGBENCH) == pytest.approx(2490.123456)


def test_ffmpeg_frames_per_sec() -> None:
    assert parse_ffmpeg_frames_per_sec(FFMPEG, 180.0) == pytest.approx(30.0)


def test_ffmpeg_reported_fps() -> None:
    assert parse_ffmpeg_reported_fps(FFMPEG) == pytest.approx(30.0)


def test_ffmpeg_zero_duration_rejected() -> None:
    with pytest.raises(ValueError):
        parse_ffmpeg_frames_per_sec(FFMPEG, 0.0)


def test_stress_ng_bogo_ops() -> None:
    assert parse_stress_ng_bogo_ops(STRESS) == pytest.approx(182.04)


def test_extract_dispatch(tmp_path: Path) -> None:
    raw = tmp_path / "raw.txt"
    raw.write_text(PGBENCH, encoding="utf-8")
    assert extract("pgbench-tps", raw, 180.0) == pytest.approx(2490.123456)


def test_extract_unknown_command(tmp_path: Path) -> None:
    raw = tmp_path / "raw.txt"
    raw.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown metric command"):
        extract("nope", raw, 180.0)
