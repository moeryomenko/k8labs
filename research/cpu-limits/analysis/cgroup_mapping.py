"""Summarize configured CPU resources and measured cgroup parameters."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from experiments.design import resources_for

PARAMETER_KEYS = ("cpu.weight", "cpu.max.quota", "cpu.max.period")
LEVELS = ("pod_slice", "container_scope", "container_leaf")


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read one CSV file with its header preserved."""

    with path.open(encoding="utf-8", newline="") as stream:
        return [dict(row) for row in csv.DictReader(stream)]


def _value(values: Iterable[str]) -> str:
    """Return one stable value or a clear list when values differ."""

    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else ", ".join(unique)


def summarize_cgroup_mapping(
    blocks_path: Path, cgroups_path: Path, *, phase: str
) -> list[dict[str, object]]:
    """Return configured resources and measured CPU parameters by policy."""

    completed: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in _read_csv(blocks_path):
        if row.get("phase") != phase or row.get("status") != "complete":
            continue
        completed[(row["block_id"], row["cell_id"])] = row

    parameters: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in _read_csv(cgroups_path):
        cell = completed.get((row["block_id"], row["cell_id"]))
        if cell is None or row.get("window") != "end":
            continue
        level, key = row.get("level"), row.get("key")
        if level in LEVELS and key in PARAMETER_KEYS:
            group = (str(cell["workload"]), str(cell["policy"]))
            parameters[group][f"{level}.{key}"].append(row["value"])

    rows: list[dict[str, object]] = []
    for workload, policy in sorted(parameters):
        request, limit = resources_for(workload, policy)
        values = parameters[(workload, policy)]
        row: dict[str, object] = {
            "workload": workload,
            "policy": policy,
            "request_millicores": request,
            "limit_millicores": limit,
        }
        for level, prefix in (("pod_slice", "pod"), ("container_leaf", "container")):
            weight = values.get(f"{level}.cpu.weight", [])
            quota = values.get(f"{level}.cpu.max.quota", [])
            period = values.get(f"{level}.cpu.max.period", [])
            row[f"{prefix}_cpu_weight"] = _value(weight) if weight else "not recorded"
            row[f"{prefix}_cpu_max"] = (
                f"{_value(quota)} {_value(period)}"
                if quota and period
                else "not recorded"
            )
        rows.append(row)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """Write a JSON cgroup mapping from one complete campaign phase."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, required=True)
    parser.add_argument("--cgroups", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "confirmatory"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    mapping = summarize_cgroup_mapping(args.blocks, args.cgroups, phase=args.phase)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(mapping)} cgroup mapping rows")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
