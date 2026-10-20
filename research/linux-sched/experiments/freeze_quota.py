"""Freeze the per-workload CPU quota from EEVDF pilot calibration.

The campaign crosses each scheduler with two quota policies: no quota, and one
quota per workload frozen *before* confirmatory data is collected. The plan
requires that frozen quota to produce at least 10% throttled periods while the
workload stays healthy.

This module turns pilot calibration rows into a frozen, versioned manifest. It
fails closed: a workload that never reached the throttling target, or that
became unhealthy, blocks the freeze instead of being silently accepted.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

TARGET_THROTTLED_RATIO = 0.10
HEALTHY_MAX_THROTTLED_RATIO = 0.90
# Saturation controls are meant to be throttled hard: stress-ng makes progress
# under any quota, so the production healthy band does not apply to it.
SATURATION_WORKLOADS = ("stress-ng",)
CONFIRMATORY_WORKLOADS = (
    "http",
    "ffmpeg",
    "postgres",
    "stress-ng",
    "mixed-http-ffmpeg",
)

_TRUE = {"1", "true", "yes", "y"}
_FALSE = {"0", "false", "no", "n"}


@dataclass(frozen=True)
class Calibration:
    workload: str
    cpu_limit: str
    throttled_period_ratio: float
    healthy: bool


def _parse_bool(value: str, *, workload: str) -> bool:
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{workload}: invalid healthy value: {value!r}")


def read_calibrations(path: str | Path) -> list[Calibration]:
    rows: list[Calibration] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"workload", "cpu_limit", "throttled_period_ratio", "healthy"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing required column(s): {sorted(missing)}")
        for row in reader:
            workload = row["workload"].strip()
            rows.append(
                Calibration(
                    workload=workload,
                    cpu_limit=row["cpu_limit"].strip(),
                    throttled_period_ratio=float(row["throttled_period_ratio"]),
                    healthy=_parse_bool(row["healthy"], workload=workload),
                )
            )
    return rows


def freeze(
    calibrations: list[Calibration],
    *,
    target_ratio: float = TARGET_THROTTLED_RATIO,
    max_ratio: float = HEALTHY_MAX_THROTTLED_RATIO,
) -> dict:
    """Validate pilot rows and return the frozen quota manifest."""
    errors: list[str] = []
    seen: dict[str, Calibration] = {}

    for row in calibrations:
        if row.workload in seen:
            errors.append(f"{row.workload}: duplicate calibration row")
            continue
        seen[row.workload] = row

    for workload in CONFIRMATORY_WORKLOADS:
        row = seen.get(workload)
        if row is None:
            errors.append(f"{workload}: no calibration row")
            continue
        if not row.cpu_limit:
            errors.append(f"{workload}: empty cpu_limit")
        if not row.healthy:
            errors.append(f"{workload}: workload is not healthy under {row.cpu_limit}")
        if row.throttled_period_ratio < target_ratio:
            errors.append(
                f"{workload}: throttled_period_ratio {row.throttled_period_ratio:.4f} "
                f"< {target_ratio:.2f}; tighten the limit and rerun the pilot"
            )
        row_max = 1.0 if workload in SATURATION_WORKLOADS else max_ratio
        if row.throttled_period_ratio > row_max:
            errors.append(
                f"{workload}: throttled_period_ratio {row.throttled_period_ratio:.4f} "
                f"> {row_max:.2f}; the workload is over-throttled"
            )

    unknown = sorted(set(seen) - set(CONFIRMATORY_WORKLOADS))
    if unknown:
        errors.append(f"unknown workload(s): {unknown}")

    if errors:
        raise ValueError("; ".join(errors))

    quotas = [
        {
            "workload": workload,
            "cpu_limit": seen[workload].cpu_limit,
            "observed_throttled_period_ratio": round(
                seen[workload].throttled_period_ratio, 6
            ),
        }
        for workload in CONFIRMATORY_WORKLOADS
    ]
    return {
        "schema_version": 1,
        "kind": "frozen-cpu-quotas",
        "target_throttled_period_ratio": target_ratio,
        "maximum_throttled_period_ratio": max_ratio,
        "control_scheduler": "eevdf",
        "quotas": quotas,
    }


def fingerprint(payload: dict) -> str:
    """Stable digest of the frozen manifest, independent of key order."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of YAML")
    args = parser.parse_args(argv)

    try:
        manifest = freeze(read_calibrations(args.calibrations))
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.json:
        args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    else:
        import yaml

        args.output.write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
    print(f"frozen quotas: {args.output}")
    print(f"manifest sha256: {fingerprint(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
