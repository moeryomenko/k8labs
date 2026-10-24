"""Parse scheduler trace and perf output for one CPU-limits cell."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

TRACE_COLUMNS = (
    "protocol",
    "run_id",
    "block_id",
    "cell_id",
    "source",
    "event",
    "ts_ns",
    "cpu",
    "pid",
    "tgid",
    "comm",
    "target_pid",
    "latency_ns",
    "runtime_ns",
    "prev_state",
    "cgroup_path",
)

_WAKEUP_EVENTS = frozenset({"sched_wakeup", "sched_wakeup_new"})
_THROTTLE_EVENTS = frozenset(
    {"throttle_cfs_rq", "throttle_cfs_rq_work", "unthrottle_cfs_rq"}
)
_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|µs|ms|s)?\s*$"
)
_DURATION_TOKEN_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|µs|ms|s)\b")
_PID_RE = re.compile(r"(?:^|[/:])(?P<pid>\d+)(?:\b|$)")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One valid record from the stable trace CSV."""

    event: str
    ts_ns: int
    pid: int
    target_pid: int | None
    runtime_ns: int | None
    row: dict[str, str]


def _optional_int(value: str) -> int | None:
    """Return a base-10 integer, or None for an empty invalid field."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped, 10)
    except ValueError:
        return None


def _duration_ns(value: str, *, default_unit: str = "ms") -> int | None:
    """Convert one perf duration to nanoseconds."""
    match = _DURATION_RE.match(value)
    if match is None:
        return None
    multiplier = {
        "ns": 1,
        "us": 1_000,
        "µs": 1_000,
        "ms": 1_000_000,
        "s": 1_000_000_000,
    }[match.group("unit") or default_unit]
    return int(float(match.group("value")) * multiplier)


def parse_trace_csv(path: Path) -> list[TraceEvent]:
    """Read valid trace records and ignore malformed CSV records."""
    events: list[TraceEvent] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            if header is None or not {"event", "ts_ns", "pid"}.issubset(header):
                return events
            for values in reader:
                if len(values) != len(header):
                    continue
                row = {
                    column: value for column, value in zip(header, values, strict=True)
                }
                event = row.get("event", "").strip()
                ts_ns = _optional_int(row.get("ts_ns", ""))
                pid = _optional_int(row.get("pid", ""))
                if not event or ts_ns is None or pid is None or ts_ns < 0 or pid < 0:
                    continue
                normalized = {column: row.get(column, "") for column in TRACE_COLUMNS}
                events.append(
                    TraceEvent(
                        event=event,
                        ts_ns=ts_ns,
                        pid=pid,
                        target_pid=_optional_int(row.get("target_pid", "")),
                        runtime_ns=_optional_int(row.get("runtime_ns", "")),
                        row=normalized,
                    )
                )
    except (OSError, UnicodeError, csv.Error):
        return events
    return events


def _related(event: TraceEvent, target_pids: set[int]) -> bool:
    return event.pid in target_pids or event.target_pid in target_pids


def _target_from_wakeup(event: TraceEvent, target_pids: set[int]) -> int | None:
    candidate = event.target_pid if event.target_pid is not None else event.pid
    return candidate if candidate in target_pids else None


def _switch_in_pid(event: TraceEvent, target_pids: set[int]) -> int | None:
    if event.target_pid in target_pids:
        return event.target_pid
    if event.target_pid is None and event.pid in target_pids:
        return event.pid
    return None


def _switch_out_pid(event: TraceEvent, target_pids: set[int]) -> int | None:
    if event.pid in target_pids and event.target_pid != event.pid:
        return event.pid
    return None


def summarize_trace(
    events: Sequence[TraceEvent], target_pids: set[int]
) -> dict[str, object]:
    """Calculate scheduler intervals for the requested PIDs."""
    delays: list[dict[str, int]] = []
    on_cpu: list[dict[str, int]] = []
    off_cpu: list[dict[str, int]] = []
    throttle_events: list[dict[str, int | str]] = []
    runtime_samples: list[dict[str, int]] = []
    switches: Counter[int] = Counter()
    pending_wakeups: dict[int, TraceEvent] = {}
    on_cpu_start: dict[int, int] = {}
    off_cpu_start: dict[int, int] = {}

    for event in sorted(events, key=lambda item: item.ts_ns):
        if not _related(event, target_pids):
            continue
        if event.event in _WAKEUP_EVENTS:
            wakeup_pid = _target_from_wakeup(event, target_pids)
            if wakeup_pid is not None:
                # A newer wakeup replaces a stale wakeup for the same task.
                pending_wakeups[wakeup_pid] = event
            continue
        if event.event == "sched_switch":
            switch_out_pid = _switch_out_pid(event, target_pids)
            if switch_out_pid is not None:
                start = on_cpu_start.pop(switch_out_pid, None)
                if start is not None and event.ts_ns >= start:
                    on_cpu.append(
                        {
                            "pid": switch_out_pid,
                            "start_ts_ns": start,
                            "end_ts_ns": event.ts_ns,
                            "duration_ns": event.ts_ns - start,
                        }
                    )
                off_cpu_start[switch_out_pid] = event.ts_ns

            switch_in_pid = _switch_in_pid(event, target_pids)
            if switch_in_pid is not None:
                wakeup = pending_wakeups.pop(switch_in_pid, None)
                if wakeup is not None and event.ts_ns >= wakeup.ts_ns:
                    delays.append(
                        {
                            "pid": switch_in_pid,
                            "wakeup_ts_ns": wakeup.ts_ns,
                            "switch_ts_ns": event.ts_ns,
                            "delay_ns": event.ts_ns - wakeup.ts_ns,
                        }
                    )
                off_start = off_cpu_start.pop(switch_in_pid, None)
                if off_start is not None and event.ts_ns >= off_start:
                    off_cpu.append(
                        {
                            "pid": switch_in_pid,
                            "start_ts_ns": off_start,
                            "end_ts_ns": event.ts_ns,
                            "duration_ns": event.ts_ns - off_start,
                        }
                    )
                on_cpu_start[switch_in_pid] = event.ts_ns
                switches[switch_in_pid] += 1
            continue
        if event.event == "sched_stat_runtime" and event.runtime_ns is not None:
            runtime_pid = (
                event.target_pid if event.target_pid in target_pids else event.pid
            )
            if runtime_pid in target_pids:
                runtime_samples.append(
                    {
                        "pid": runtime_pid,
                        "ts_ns": event.ts_ns,
                        "runtime_ns": event.runtime_ns,
                    }
                )
            continue
        if event.event in _THROTTLE_EVENTS:
            throttle_pid = (
                event.target_pid if event.target_pid in target_pids else event.pid
            )
            if throttle_pid in target_pids:
                throttle_events.append(
                    {"pid": throttle_pid, "event": event.event, "ts_ns": event.ts_ns}
                )

    return {
        "protocol": "cpu-limits-v1",
        "target_pids": sorted(target_pids),
        "run_queue_delays_ns": delays,
        "on_cpu_intervals_ns": on_cpu,
        "off_cpu_intervals_ns": off_cpu,
        "context_switches": {str(pid): switches[pid] for pid in sorted(switches)},
        "throttle_events": throttle_events,
        "runtime_samples_ns": runtime_samples,
    }


def parse_perf_latency(text: str, target_pids: set[int]) -> list[dict[str, int]]:
    """Parse target rows from ``perf sched latency`` text output."""
    results: list[dict[str, int]] = []
    for line in text.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 5:
            continue
        pid_match = _PID_RE.search(fields[0])
        if pid_match is None:
            continue
        pid = int(pid_match.group("pid"))
        if pid not in target_pids:
            continue
        runtime_ns = _duration_ns(fields[1])
        switches = _optional_int(fields[2])
        average_delay_ns = _duration_ns(fields[3])
        maximum_delay_ns = _duration_ns(fields[4])
        if None in (runtime_ns, switches, average_delay_ns, maximum_delay_ns):
            continue
        results.append(
            {
                "pid": pid,
                "runtime_ns": runtime_ns,
                "switches": switches,
                "average_delay_ns": average_delay_ns,
                "maximum_delay_ns": maximum_delay_ns,
            }
        )
    return results


def parse_perf_timehist(text: str, target_pids: set[int]) -> list[dict[str, int]]:
    """Parse target rows from ``perf sched timehist`` text output."""
    results: list[dict[str, int]] = []
    for line in text.splitlines():
        pid_matches = list(_PID_RE.finditer(line))
        if not pid_matches:
            continue
        pid_match = pid_matches[-1]
        pid = int(pid_match.group("pid"))
        if pid not in target_pids:
            continue
        durations = [
            _duration_ns(match.group(0))
            for match in _DURATION_TOKEN_RE.finditer(line[pid_match.end() :])
        ]
        durations = [duration for duration in durations if duration is not None]
        if len(durations) < 3:
            continue
        wait_ns, sched_delay_ns, run_time_ns = durations[-3:]
        results.append(
            {
                "pid": pid,
                "wait_ns": wait_ns,
                "sched_delay_ns": sched_delay_ns,
                "run_time_ns": run_time_ns,
            }
        )
    return results


def _write_filtered_trace(
    events: Sequence[TraceEvent], target_pids: set[int], path: Path
) -> None:
    """Write only valid target records in compressed CSV form."""
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRACE_COLUMNS)
        writer.writeheader()
        for event in sorted(events, key=lambda item: item.ts_ns):
            if _related(event, target_pids):
                writer.writerow(event.row)


def parse_and_write(
    trace_csv: Path,
    target_pids: set[int],
    output_dir: Path,
    *,
    perf_latency_text: str = "",
    perf_timehist_text: str = "",
) -> dict[str, object]:
    """Parse input files and write the stable summary and filtered trace."""
    events = parse_trace_csv(trace_csv)
    summary = summarize_trace(events, target_pids)
    summary["perf_latency"] = parse_perf_latency(perf_latency_text, target_pids)
    summary["perf_timehist"] = parse_perf_timehist(perf_timehist_text, target_pids)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "trace-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_filtered_trace(events, target_pids, output_dir / "filtered-trace.csv.gz")
    return summary


def _read_optional_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the parser command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-csv", type=Path, required=True)
    parser.add_argument("--target-pid", type=int, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--perf-latency", type=Path)
    parser.add_argument("--perf-timehist", type=Path)
    args = parser.parse_args(argv)
    if any(pid < 0 for pid in args.target_pid):
        parser.error("--target-pid must be non-negative")
    parse_and_write(
        args.trace_csv,
        set(args.target_pid),
        args.output_dir,
        perf_latency_text=_read_optional_text(args.perf_latency),
        perf_timehist_text=_read_optional_text(args.perf_timehist),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
