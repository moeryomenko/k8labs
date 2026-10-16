"""Unit tests for the linux-sched primary-outcome parsers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import (  # noqa: E402
    extract,
    parse_ffmpeg_fps_window,
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
stress-ng: info:  [1] dispatching hogs: 2 cpu
stress-ng: info:  [1] successful run completed in 180.00s (3 mins, 0.00 secs)
stress-ng: info:  [1] stressor       bogo ops real time  usr time  sys time   bogo ops/s   bogo ops/s
stress-ng: info:  [1]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)
stress-ng: info:  [1] cpu               79119    180.00    330.43      0.03       439.55       239.42
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
    # The real-time column of the summary row, not the usr+sys one.
    assert parse_stress_ng_bogo_ops(STRESS) == pytest.approx(439.55)


def test_stress_ng_rejects_a_log_without_the_summary() -> None:
    with pytest.raises(ValueError, match="bogo-ops/s summary row"):
        parse_stress_ng_bogo_ops("stress-ng: info:  [1] dispatching hogs: 2 cpu\n")


def test_extract_dispatch(tmp_path: Path) -> None:
    raw = tmp_path / "raw.txt"
    raw.write_text(PGBENCH, encoding="utf-8")
    assert extract("pgbench-tps", raw, 180.0) == pytest.approx(2490.123456)


def test_extract_unknown_command(tmp_path: Path) -> None:
    raw = tmp_path / "raw.txt"
    raw.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown metric command"):
        extract("nope", raw, 180.0)


# A co-located batch job that outlives the measured window by a warmup. Note the
# two clocks: media time runs far ahead of wall time because the encoder is much
# faster than real time, so only `elapsed=` can locate the window boundary.
FFMPEG_WITH_WARMUP = (
    "frame=  354 fps=0.0 q=28.0 size=N/A time=00:00:14.66 bitrate=N/A "
    "speed=29.3x elapsed=0:00:00.50     "
    "frame=  639 fps=639 q=28.0 size=N/A time=00:00:26.54 bitrate=N/A "
    "speed=26.5x elapsed=0:00:01.00     "
    "frame= 4320 fps=528 q=-1.0 Lsize=N/A time=00:02:59.91 bitrate=N/A "
    "speed=22x elapsed=0:00:08.18"
)


def test_ffmpeg_fps_window_counts_only_the_measured_interval() -> None:
    # The last sample at or before the 0.75 s warmup boundary is the 354-frame
    # one at 0.50 s, so the window produced 4320 - 354 frames over 0.5 s.
    assert parse_ffmpeg_fps_window(FFMPEG_WITH_WARMUP, 0.5, 0.75) == pytest.approx(
        (4320 - 354) / 0.5
    )


def test_ffmpeg_fps_window_uses_wall_clock_not_media_position() -> None:
    # The first sample sits at media 14.66 s but wall 0.50 s. A boundary taken
    # from media time would treat it as inside the window and count it.
    by_wall = parse_ffmpeg_fps_window(FFMPEG_WITH_WARMUP, 0.5, 0.75)
    assert by_wall == pytest.approx((4320 - 354) / 0.5)
    assert by_wall != pytest.approx(4320 / 0.5)


def test_ffmpeg_fps_window_without_warmup_matches_whole_run() -> None:
    assert parse_ffmpeg_fps_window(FFMPEG_WITH_WARMUP, 8.18, 0.0) == pytest.approx(
        4320 / 8.18
    )


def test_ffmpeg_fps_window_requires_progress_lines() -> None:
    with pytest.raises(ValueError, match="no ffmpeg progress lines"):
        parse_ffmpeg_fps_window("no progress here\n", 180.0, 30.0)


def test_ffmpeg_fps_window_rejects_zero_duration() -> None:
    with pytest.raises(ValueError, match="duration_s must be positive"):
        parse_ffmpeg_fps_window(FFMPEG_WITH_WARMUP, 0.0, 30.0)
