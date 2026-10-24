#!/usr/bin/env python3
"""Freeze fit and low CPU policy values from pilot cgroup observations.

The command records the pilot evidence. If no candidate reaches the required
10 to 40 percent throttled-period band, it writes a FAIL record and retains the
approved frozen defaults with an explicit limitation.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

SERIES_ROOT = Path(__file__).resolve().parents[1]
if str(SERIES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERIES_ROOT))

from experiments.design import CPU_VALUES, WORKLOADS

PROTOCOL = "cpu-limits-v1"
THROTTLE_MIN = 0.10
THROTTLE_MAX = 0.40
THROTTLE_TARGET = 0.25


@dataclass(frozen=True)
class PilotSample:
    """The cgroup values used to calibrate one workload policy."""

    block_id: str
    cell_id: str
    level: str
    workload: str
    policy: str
    quota_us: int | None
    period_us: int | None
    periods: int
    throttled_periods: int

    @property
    def throttled_ratio(self) -> float:
        """Return a defined measured throttled-period ratio."""

        if self.periods <= 0:
            raise ValueError("nr_periods must be positive for calibration")
        if self.throttled_periods < 0 or self.throttled_periods > self.periods:
            raise ValueError("nr_throttled must be within nr_periods")
        return self.throttled_periods / self.periods

    @property
    def millicores(self) -> int | None:
        """Convert one finite cgroup quota to a whole CPU millicore value."""

        if self.quota_us is None or self.period_us is None or self.period_us <= 0:
            return None
        if self.quota_us <= 0:
            raise ValueError("cpu.max quota must be positive")
        return round(1_000 * self.quota_us / self.period_us)


@dataclass(frozen=True)
class FrozenPolicy:
    """One workload's frozen values and calibration decision."""

    workload: str
    fit_millicores: int
    low_millicores: int
    status: str
    evidence: tuple[dict[str, object], ...]
    limitation: str | None


def _required(row: Mapping[str, str], key: str) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"cgroups CSV row is missing {key}")
    return value


def _as_int(value: str, key: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error
    if result < 0:
        raise ValueError(f"{key} must not be negative")
    return result


def _workloads_by_cell(blocks_path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    with blocks_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"block_id", "cell_id", "workload", "policy", "status"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("blocks CSV is missing required columns")
        result: dict[tuple[str, str], tuple[str, str]] = {}
        for row in reader:
            if row.get("status") != "complete":
                continue
            block_id = _required(row, "block_id")
            cell_id = _required(row, "cell_id")
            workload = _required(row, "workload")
            policy = _required(row, "policy")
            if workload not in WORKLOADS:
                raise ValueError(f"blocks CSV has unknown workload: {workload}")
            result[(block_id, cell_id)] = (workload, policy)
    return result


def read_pilot_samples(cgroups_path: Path, blocks_path: Path) -> list[PilotSample]:
    """Read complete pilot cgroup deltas without manufacturing missing values."""

    workloads = _workloads_by_cell(blocks_path)
    values: dict[tuple[str, str, str, str], dict[str, str]] = defaultdict(dict)
    with cgroups_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"block_id", "cell_id", "window", "level", "key", "value"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("cgroups CSV is missing required columns")
        for row in reader:
            block_id = row.get("block_id", "")
            cell_id = row.get("cell_id", "")
            if (block_id, cell_id) not in workloads:
                continue
            if row.get("level") not in {
                "pod_slice",
                "container_leaf",
                "container_scope",
            }:
                continue
            key = row.get("key", "")
            if key in {"cpu.stat.nr_periods", "cpu.stat.nr_throttled"}:
                if row.get("window") != "measurement_delta":
                    continue
            elif key in {"cpu.max.quota", "cpu.max.period"}:
                if row.get("window") not in {"start", "end"}:
                    continue
            else:
                continue
            values[(block_id, cell_id, row["level"], key)][key] = _required(
                row, "value"
            )

    samples: list[PilotSample] = []
    for (block_id, cell_id), (workload, policy) in workloads.items():
        for level in ("container_leaf", "container_scope", "pod_slice"):
            level_values = {
                key.rsplit(".", 1)[-1]: values.get(
                    (block_id, cell_id, level, key), {}
                ).get(key, "")
                for key in ("cpu.stat.nr_periods", "cpu.stat.nr_throttled")
            }
            if not all(level_values.values()):
                continue
            quota_text = values.get(
                (block_id, cell_id, level, "cpu.max.quota"), {}
            ).get("cpu.max.quota", "")
            period_text = values.get(
                (block_id, cell_id, level, "cpu.max.period"), {}
            ).get("cpu.max.period", "")
            quota = (
                None if quota_text == "max" else _as_int(quota_text, "cpu.max.quota")
            )
            period = _as_int(period_text, "cpu.max.period") if period_text else None
            samples.append(
                PilotSample(
                    block_id=block_id,
                    cell_id=cell_id,
                    level=level,
                    workload=workload,
                    policy=policy,
                    quota_us=quota,
                    period_us=period,
                    periods=_as_int(level_values["nr_periods"], "cpu.stat.nr_periods"),
                    throttled_periods=_as_int(
                        level_values["nr_throttled"], "cpu.stat.nr_throttled"
                    ),
                )
            )
            break
    return samples


def freeze_values(workload: str, samples: Iterable[PilotSample]) -> FrozenPolicy:
    """Choose pilot-supported values or preserve defaults with a FAIL record."""

    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload}")
    defaults = CPU_VALUES[workload]
    sample_list = [sample for sample in samples if sample.workload == workload]
    evidence: list[dict[str, object]] = []
    fit_candidates = [
        sample for sample in sample_list if sample.policy == "fit" and sample.millicores
    ]
    fit = max(
        (
            sample.millicores
            for sample in fit_candidates
            if sample.millicores is not None
        ),
        default=defaults["fit"],
    )
    for sample in sample_list:
        try:
            ratio = sample.throttled_ratio
            millicores = sample.millicores
        except ValueError as error:
            evidence.append(
                {
                    "block_id": sample.block_id,
                    "cell_id": sample.cell_id,
                    "level": sample.level,
                    "policy": sample.policy,
                    "status": "invalid",
                    "reason": str(error),
                }
            )
            continue
        evidence.append(
            {
                "block_id": sample.block_id,
                "cell_id": sample.cell_id,
                "level": sample.level,
                "policy": sample.policy,
                "quota_millicores": millicores,
                "throttled_period_ratio": ratio,
                "in_target_band": THROTTLE_MIN <= ratio <= THROTTLE_MAX,
            }
        )
    low_candidates = [
        sample
        for sample in sample_list
        if sample.millicores is not None
        and THROTTLE_MIN <= sample.throttled_ratio <= THROTTLE_MAX
    ]
    if not low_candidates:
        return FrozenPolicy(
            workload=workload,
            fit_millicores=defaults["fit"],
            low_millicores=defaults["low"],
            status="FAIL",
            evidence=tuple(evidence),
            limitation=(
                "pilot calibration did not produce a finite quota with a 10 to 40 percent "
                "throttled-period ratio; approved defaults remain in use"
            ),
        )
    chosen = min(
        low_candidates,
        key=lambda sample: (
            abs(sample.throttled_ratio - THROTTLE_TARGET),
            sample.millicores or 0,
        ),
    )
    assert chosen.millicores is not None
    return FrozenPolicy(
        workload=workload,
        fit_millicores=fit,
        low_millicores=chosen.millicores,
        status="PASS",
        evidence=tuple(evidence),
        limitation=None,
    )


def write_policies(path: Path, policies: Sequence[FrozenPolicy]) -> dict[str, object]:
    """Write the versioned policy record and retain all selection evidence."""

    status = "PASS" if all(policy.status == "PASS" for policy in policies) else "FAIL"
    payload = {
        "protocol": PROTOCOL,
        "status": status,
        "throttled_period_ratio_target": {
            "minimum": THROTTLE_MIN,
            "maximum": THROTTLE_MAX,
        },
        "policies": [asdict(policy) for policy in policies],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse pilot input and output paths."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--cgroups", type=Path)
    parser.add_argument("--blocks", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Freeze policy values and return non-zero when calibration failed."""

    args = parse_args(argv)
    cgroups = args.cgroups or args.data_root / "pilot" / "cgroups.csv"
    blocks = args.blocks or args.data_root / "pilot" / "blocks.csv"
    output = args.output or args.data_root / "policies.json"
    try:
        samples = read_pilot_samples(cgroups, blocks)
        policies = [freeze_values(workload, samples) for workload in WORKLOADS]
    except (OSError, ValueError) as error:
        policies = [
            FrozenPolicy(
                workload=workload,
                fit_millicores=CPU_VALUES[workload]["fit"],
                low_millicores=CPU_VALUES[workload]["low"],
                status="FAIL",
                evidence=(),
                limitation=f"pilot calibration input failed: {error}",
            )
            for workload in WORKLOADS
        ]
    payload = write_policies(output, policies)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
