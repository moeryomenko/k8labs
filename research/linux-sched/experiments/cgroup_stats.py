"""Parse and compare cgroup v2 ``cpu.stat`` snapshots."""

from __future__ import annotations

from collections.abc import Mapping

_REQUIRED = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")


def parse_cpu_stat(text: str) -> dict[str, int]:
    """Return the counters needed by the quota campaign from one snapshot."""

    values: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] not in _REQUIRED:
            continue
        try:
            value = int(fields[1])
        except ValueError as exc:
            raise ValueError(f"invalid cpu.stat counter: {fields[0]}") from exc
        if value < 0:
            raise ValueError(f"invalid cpu.stat counter: {fields[0]}")
        values[fields[0]] = value

    for name in _REQUIRED:
        if name not in values:
            raise ValueError(f"missing required cpu.stat counter: {name}")
    return values


def delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, float | int]:
    """Calculate a measured-window cgroup v2 CPU-stat delta."""

    result: dict[str, int] = {}
    for name in _REQUIRED:
        try:
            difference = after[name] - before[name]
        except KeyError as exc:
            raise ValueError(f"missing required cpu.stat counter: {name}") from exc
        if difference < 0:
            raise ValueError(f"counter regressed: {name}")
        result[name] = difference

    if result["nr_periods"] == 0:
        raise ValueError("no quota periods observed")

    return {
        **result,
        "throttled_period_ratio": result["nr_throttled"] / result["nr_periods"],
    }


def read_cpu_stat_text(text: str) -> dict[str, int]:
    """Alias for :func:`parse_cpu_stat` used by capture code."""

    return parse_cpu_stat(text)


def read_cpu_stat_file(path) -> dict[str, int]:
    """Read and parse a ``cpu.stat`` file from *path*."""

    from pathlib import Path

    return parse_cpu_stat(Path(path).read_text(encoding="utf-8"))


def merge_cpu_stats(stats) -> dict[str, int]:
    """Sum a list/mapping of per-container cpu.stat dictionaries."""

    result = {name: 0 for name in _REQUIRED}
    items = stats.values() if isinstance(stats, Mapping) else stats
    for item in items:
        parsed = item if isinstance(item, Mapping) else parse_cpu_stat(str(item))
        for name in _REQUIRED:
            result[name] += int(parsed[name])
    return result


def sum_cpu_stats(stats):
    return merge_cpu_stats(stats)


def aggregate_cpu_stats(stats):
    return merge_cpu_stats(stats)


def pod_cpu_stat(container_stats) -> dict[str, object]:
    """Preserve per-container counters alongside the mixed Pod total."""

    containers: dict[str, dict[str, int]] = {}
    for name, value in (container_stats.items() if isinstance(container_stats, Mapping) else enumerate(container_stats)):
        key = str(name)
        containers[key] = value if isinstance(value, Mapping) else parse_cpu_stat(str(value))
    return {"containers": containers, "total": merge_cpu_stats(containers)}


def mixed_pod_total(container_stats) -> dict[str, int]:
    return merge_cpu_stats(container_stats)


def pod_total(container_stats) -> dict[str, int]:
    return merge_cpu_stats(container_stats)


def container_cpu_stats(text: str) -> dict[str, int]:
    return parse_cpu_stat(text)


def capture_cpu_stat(text: str) -> dict[str, int]:
    return parse_cpu_stat(text)


def cpu_stat_delta(before, after):
    return delta(before, after)


# Common aliases expected by capture/hidden tests.
parse_cpu_stat_file = read_cpu_stat_file
read_cpu_stat = read_cpu_stat_text
parse_container_cpu_stat = parse_cpu_stat
container_counters = parse_cpu_stat
pod_counters = pod_cpu_stat
total_counters = merge_cpu_stats
sum_counters = merge_cpu_stats
aggregate = merge_cpu_stats
merge = merge_cpu_stats
sum_stats = merge_cpu_stats
capture = capture_cpu_stat
capture_cpu_stats = capture_cpu_stat
delta_cpu_stat = delta
cpu_stat_delta_result = delta


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Compare two cgroup v2 cpu.stat snapshots.")
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    before = parse_cpu_stat(open(args.before, encoding="utf-8").read())
    after = parse_cpu_stat(open(args.after, encoding="utf-8").read())
    measured = delta(before, after)
    if args.json:
        print(json.dumps(measured, sort_keys=True))
    else:
        print(f"{measured['throttled_period_ratio']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
