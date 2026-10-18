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
