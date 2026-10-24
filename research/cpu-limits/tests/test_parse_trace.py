"""Tests for CPU-limits trace parsing."""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

from capture.parse_trace import (
    TRACE_COLUMNS,
    parse_and_write,
    parse_perf_latency,
    parse_perf_timehist,
    parse_trace_csv,
)


def write_trace(path: Path, rows: list[dict[str, str]]) -> None:
    """Write a trace CSV that follows the frozen trace contract."""
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRACE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def trace_row(
    event: str,
    ts_ns: int,
    *,
    pid: int,
    target_pid: int | None = None,
) -> dict[str, str]:
    """Build one synthetic trace row."""
    return {
        "protocol": "cpu-limits-v1",
        "run_id": "run-1",
        "block_id": "0",
        "cell_id": "cpu-bound-fit-solo",
        "source": "bpftrace",
        "event": event,
        "ts_ns": str(ts_ns),
        "cpu": "0",
        "pid": str(pid),
        "tgid": str(pid),
        "comm": "worker",
        "target_pid": "" if target_pid is None else str(target_pid),
        "latency_ns": "",
        "runtime_ns": "",
        "prev_state": "",
        "cgroup_path": "/kubepods.slice/test",
    }


def test_pairs_wakeup_with_next_switch_and_calculates_intervals(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.csv"
    output_dir = tmp_path / "output"
    write_trace(
        trace_path,
        [
            trace_row("sched_wakeup", 100, pid=77, target_pid=123),
            trace_row("sched_switch", 160, pid=77, target_pid=123),
            trace_row("sched_switch", 210, pid=123, target_pid=77),
            trace_row("sched_wakeup", 230, pid=77, target_pid=123),
            trace_row("sched_switch", 260, pid=77, target_pid=123),
        ],
    )

    summary = parse_and_write(trace_path, {123}, output_dir)

    assert [item["delay_ns"] for item in summary["run_queue_delays_ns"]] == [60, 30]
    assert summary["on_cpu_intervals_ns"] == [
        {"pid": 123, "start_ts_ns": 160, "end_ts_ns": 210, "duration_ns": 50}
    ]
    assert summary["off_cpu_intervals_ns"] == [
        {"pid": 123, "start_ts_ns": 210, "end_ts_ns": 260, "duration_ns": 50}
    ]
    assert summary["context_switches"] == {"123": 2}
    assert (output_dir / "trace-summary.json").exists()


def test_unmatched_wakeup_does_not_create_a_delay(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.csv"
    write_trace(
        trace_path,
        [
            trace_row("sched_wakeup", 100, pid=77, target_pid=123),
            trace_row("sched_switch", 120, pid=99, target_pid=456),
        ],
    )

    summary = parse_and_write(trace_path, {123}, tmp_path / "output")

    assert summary["run_queue_delays_ns"] == []


def test_filters_unrelated_pids_and_records_throttle_times(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.csv"
    output_dir = tmp_path / "output"
    write_trace(
        trace_path,
        [
            trace_row("sched_wakeup", 100, pid=77, target_pid=123),
            trace_row("sched_switch", 110, pid=77, target_pid=123),
            trace_row("throttle_cfs_rq", 120, pid=123),
            trace_row("unthrottle_cfs_rq", 140, pid=123),
            trace_row("sched_wakeup", 150, pid=77, target_pid=999),
            trace_row("sched_switch", 160, pid=77, target_pid=999),
        ],
    )

    summary = parse_and_write(trace_path, {123}, output_dir)

    assert [(item["event"], item["ts_ns"]) for item in summary["throttle_events"]] == [
        ("throttle_cfs_rq", 120),
        ("unthrottle_cfs_rq", 140),
    ]
    with gzip.open(
        output_dir / "filtered-trace.csv.gz", "rt", encoding="utf-8"
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert all(row["target_pid"] != "999" for row in rows)
    assert {row["event"] for row in rows} == {
        "sched_wakeup",
        "sched_switch",
        "throttle_cfs_rq",
        "unthrottle_cfs_rq",
    }


def test_parses_perf_latency_and_timehist_for_targets() -> None:
    latency = """\
Task                  | Runtime ms | Switches | Average delay ms | Maximum delay ms |
worker:123            | 2.000 ms  | 4        | 0.500 ms         | 1.000 ms         |
other:999             | 3.000 ms  | 5        | 0.600 ms         | 1.200 ms         |
"""
    timehist = """\
time cpu task wait time sch delay run time
1.000000 [001] worker:123 0.100 ms 0.200 ms 0.300 ms
this line is malformed
2.000000 [001] other:999 0.400 ms 0.500 ms 0.600 ms
"""

    assert parse_perf_latency(latency, {123}) == [
        {
            "pid": 123,
            "runtime_ns": 2_000_000,
            "switches": 4,
            "average_delay_ns": 500_000,
            "maximum_delay_ns": 1_000_000,
        }
    ]
    assert parse_perf_timehist(timehist, {123}) == [
        {
            "pid": 123,
            "wait_ns": 100_000,
            "sched_delay_ns": 200_000,
            "run_time_ns": 300_000,
        }
    ]


def test_malformed_csv_lines_are_ignored(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.csv"
    write_trace(trace_path, [trace_row("sched_wakeup", 100, pid=77, target_pid=123)])
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write("not,a,valid,trace,row\n")
        stream.write('"unterminated\n')

    events = parse_trace_csv(trace_path)

    assert len(events) == 1
    assert events[0].event == "sched_wakeup"


def test_parses_the_bpftrace_probe_header(tmp_path: Path) -> None:
    path = tmp_path / "probe.csv"
    path.write_text(
        "timestamp,event,cpu,pid,comm,prev_state,next_pid,next_comm,runtime_ns,cgroup_id\n"
        "100,sched_wakeup,0,1,waker,,123,worker,,1\n"
        "160,sched_switch,0,1,waker,0,123,worker,,1\n"
        "210,sched_switch,0,123,worker,0,1,waker,,1\n",
        encoding="utf-8",
    )

    events = parse_trace_csv(path)

    assert len(events) == 3
    assert events[0].ts_ns == 100
    assert events[0].target_pid == 123
    assert events[1].ts_ns == 160
