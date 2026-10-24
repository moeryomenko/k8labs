"""Read CPU-limits Pod cgroups without changing the cgroup filesystem."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROTOCOL = "cpu-limits-v1"
QOS_CLASSES = ("guaranteed", "burstable", "besteffort")
CPU_STAT_KEYS = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
REQUIRED_FILES = (
    "cpu.weight",
    "cpu.max",
    "cpu.stat",
    "cpu.pressure",
    "memory.current",
    "memory.peak",
    "cgroup.type",
    "cgroup.controllers",
)
_UID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize_pod_uid(pod_uid: str) -> str:
    """Validate a Kubernetes UID and make its systemd slice form."""

    if not _UID_RE.fullmatch(pod_uid):
        raise ValueError(f"invalid pod UID: {pod_uid!r}")
    return pod_uid.replace("-", "_")


def _cgroup_root(cgroup_root: Path | str) -> Path:
    root = Path(cgroup_root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"cgroup root is not a directory: {root}")
    return root


def resolve_pod_slice(cgroup_root: Path | str, pod_uid: str) -> Path:
    """Find exactly one Pod slice below the Kubernetes QoS slices."""

    root = _cgroup_root(cgroup_root)
    escaped_uid = normalize_pod_uid(pod_uid)
    matches: list[Path] = []
    for qos in QOS_CLASSES:
        pod_slice = (
            root
            / "kubepods.slice"
            / f"kubepods-{qos}.slice"
            / f"kubepods-{qos}-pod{escaped_uid}.slice"
        )
        if pod_slice.is_dir():
            matches.append(pod_slice)
    if not matches:
        raise FileNotFoundError(f"Pod slice not found for UID {pod_uid}")
    if len(matches) != 1:
        rendered = ", ".join(str(path) for path in matches)
        raise RuntimeError(f"Pod UID {pod_uid} has multiple slices: {rendered}")
    return matches[0]


def _read_required(path: Path) -> str:
    """Read one required cgroup value and retain its textual value."""

    if not path.is_file():
        raise FileNotFoundError(f"missing required cgroup file: {path}")
    return path.read_text(encoding="utf-8").rstrip("\n")


def _parse_integer(value: str, source: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"invalid integer in {source}: {value!r}") from error


def _parse_cpu_max(value: str) -> tuple[int | None, int]:
    fields = value.split()
    if len(fields) != 2:
        raise ValueError(f"invalid cpu.max value: {value!r}")
    quota = None if fields[0] == "max" else _parse_integer(fields[0], "cpu.max")
    period = _parse_integer(fields[1], "cpu.max")
    if quota is not None and quota < 0:
        raise ValueError(f"cpu.max quota must not be negative: {value!r}")
    if period <= 0:
        raise ValueError(f"cpu.max period must be positive: {value!r}")
    return quota, period


def _parse_key_values(value: str, source: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for line in value.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"invalid {source} line: {line!r}")
        key, raw_number = fields
        if key in parsed:
            raise ValueError(f"duplicate {source} key: {key}")
        parsed[key] = _parse_integer(raw_number, source)
    return parsed


def _parse_cpu_stat(value: str) -> dict[str, int]:
    parsed = _parse_key_values(value, "cpu.stat")
    missing = [key for key in CPU_STAT_KEYS if key not in parsed]
    if missing:
        raise ValueError(f"cpu.stat is missing required counters: {', '.join(missing)}")
    return {key: parsed[key] for key in CPU_STAT_KEYS}


def _parse_cpu_pressure(value: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for line in value.splitlines():
        fields = line.split()
        if not fields:
            continue
        category = fields[0]
        values = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
        if "total" not in values:
            raise ValueError(f"cpu.pressure line has no total: {line!r}")
        if category in totals:
            raise ValueError(f"duplicate cpu.pressure category: {category}")
        totals[category] = _parse_integer(values["total"], "cpu.pressure")
    missing = [category for category in ("some", "full") if category not in totals]
    if missing:
        raise ValueError(f"cpu.pressure is missing categories: {', '.join(missing)}")
    return {"some_total_usec": totals["some"], "full_total_usec": totals["full"]}


def _scope_paths(pod_slice: Path) -> tuple[Path, ...]:
    """Return direct CRI-O and other systemd scopes in name order."""

    return tuple(
        path
        for path in sorted(pod_slice.iterdir(), key=lambda item: item.name)
        if path.is_dir() and path.name.endswith(".scope")
    )


def _level(path: Path, kind: str, *, scope_type: str | None = None) -> dict[str, object]:
    """Read the required raw and parsed values for one cgroup level."""

    raw = {name: _read_required(path / name) for name in REQUIRED_FILES}
    quota, period = _parse_cpu_max(raw["cpu.max"])
    level: dict[str, object] = {
        "kind": kind,
        "path": str(path),
        "cpu_weight": _parse_integer(raw["cpu.weight"], "cpu.weight"),
        "cpu_max_quota_us": quota,
        "cpu_max_period_us": period,
        "cpu_stat": _parse_cpu_stat(raw["cpu.stat"]),
        "cpu_pressure": _parse_cpu_pressure(raw["cpu.pressure"]),
        "memory": {
            "current_bytes": _parse_integer(raw["memory.current"], "memory.current"),
            "peak_bytes": _parse_integer(raw["memory.peak"], "memory.peak"),
        },
        "cgroup_type": raw["cgroup.type"],
        "cgroup_controllers": raw["cgroup.controllers"],
        "raw": raw,
    }
    if scope_type is not None:
        level["scope_type"] = scope_type
    return level


def capture(
    cgroup_root: Path | str,
    pod_uid: str,
    *,
    captured_at: str | None = None,
    run_id: str = "unknown",
    block_id: int = 0,
    cell_id: str = "unknown",
    node: str = "unknown",
    scheduler_name: str = "unknown",
    scheduler_parameters: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Capture the Pod hierarchy as one read-only cgroup probe record."""

    pod_slice = resolve_pod_slice(cgroup_root, pod_uid)
    qos_slice = pod_slice.parent
    levels: list[dict[str, object]] = [
        _level(qos_slice, "qos_slice"),
        _level(pod_slice, "pod_slice"),
    ]
    for scope in _scope_paths(pod_slice):
        scope_type = "crio" if scope.name.startswith("crio-") else "systemd"
        levels.append(_level(scope, "container_scope", scope_type=scope_type))
        container_leaf = scope / "container"
        if container_leaf.is_dir():
            levels.append(_level(container_leaf, "container_leaf"))
    timestamp = captured_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "protocol": PROTOCOL,
        "captured_at": timestamp,
        "run_id": run_id,
        "block_id": block_id,
        "cell_id": cell_id,
        "node": node,
        "pod_uid": pod_uid,
        "scheduler": {
            "name": scheduler_name,
            "parameters": dict(scheduler_parameters or {}),
        },
        "levels": levels,
    }


def _levels(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    levels = record.get("levels")
    if not isinstance(levels, list):
        raise TypeError("probe record has no levels list")
    if not all(isinstance(level, Mapping) for level in levels):
        raise TypeError("probe record has a non-object level")
    return [cast(Mapping[str, object], level) for level in levels]


def _cpu_stat(level: Mapping[str, object]) -> Mapping[str, object]:
    value = level.get("cpu_stat")
    if not isinstance(value, Mapping):
        raise TypeError(f"level has no cpu_stat mapping: {level.get('path')!r}")
    return cast(Mapping[str, object], value)


def cpu_stat_deltas(
    before: Mapping[str, object], after: Mapping[str, object]
) -> list[dict[str, object]]:
    """Compute required cumulative CPU-counter deltas for matching levels."""

    before_by_path = {str(level.get("path")): level for level in _levels(before)}
    deltas: list[dict[str, object]] = []
    for after_level in _levels(after):
        path = str(after_level.get("path"))
        before_level = before_by_path.pop(path, None)
        if before_level is None:
            raise ValueError(f"after record has unknown cgroup level: {path}")
        before_stat = _cpu_stat(before_level)
        after_stat = _cpu_stat(after_level)
        counters: dict[str, int] = {}
        for key in CPU_STAT_KEYS:
            if key not in before_stat or key not in after_stat:
                raise ValueError(f"cgroup level {path} is missing cpu.stat counter: {key}")
            before_value = before_stat[key]
            after_value = after_stat[key]
            if not isinstance(before_value, int) or not isinstance(after_value, int):
                raise TypeError(f"cgroup level {path} has a non-integer cpu.stat counter: {key}")
            if after_value < before_value:
                raise ValueError(f"cgroup level {path} counter decreased: {key}")
            counters[key] = after_value - before_value
        deltas.append(
            {
                "kind": after_level.get("kind"),
                "path": path,
                "cpu_stat": counters,
            }
        )
    if before_by_path:
        paths = ", ".join(sorted(before_by_path))
        raise ValueError(f"after record is missing cgroup levels: {paths}")
    return deltas


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse a read-only probe request."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cgroup_root", type=Path, help="host cgroup-v2 root")
    parser.add_argument("pod_uid", help="Kubernetes Pod UID")
    parser.add_argument("--before", type=Path, help="prior probe JSON record")
    parser.add_argument("--run-id", default="unknown")
    parser.add_argument("--block-id", type=int, default=0)
    parser.add_argument("--cell-id", default="unknown")
    parser.add_argument("--node", default="unknown")
    parser.add_argument("--scheduler-name", default="unknown")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON record and optional CPU-stat deltas."""

    args = parse_args(argv)
    record = capture(
        args.cgroup_root,
        args.pod_uid,
        run_id=args.run_id,
        block_id=args.block_id,
        cell_id=args.cell_id,
        node=args.node,
        scheduler_name=args.scheduler_name,
    )
    if args.before is not None:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        if not isinstance(before, Mapping):
            raise ValueError("prior probe JSON record must be an object")
        record["deltas"] = cpu_stat_deltas(before, record)
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
