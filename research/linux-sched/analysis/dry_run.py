"""Dry-run the factorial campaign analysis on synthetic data.

The confirmatory campaign cannot start until the image is signed and rolled out.
This script keeps the analysis honest in the meantime: it builds two synthetic
data sets with the campaign's shape (five workloads, six schedulers, two quota
policies), analyses them with the real module, and checks the outcome:

* null data must produce no regression or improvement;
* data with a known defect on one arm must produce exactly that detection.

It is fast enough to run in CI and is also a worked example of the expected
blocks.csv shape.
"""

from __future__ import annotations

import csv
import math
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factorial_analysis as fa  # noqa: E402

WORKLOADS = ("http", "ffmpeg", "postgres", "stress-ng", "mixed-http-ffmpeg")
SCHEDULERS = (
    "eevdf",
    "scx_bpfland",
    "scx_lavd",
    "scx_mavd",
    "scx_mlfq",
    "scx_nitosis",
)
POLICIES = ("unlimited", "limited")
BLOCKS = 6
AFFECTED_SCHEDULER = "scx_mavd"
PERFORMANCE_HIT = 0.80
THROTTLE_HIT = 1.30
FIELDS = [
    "block_id",
    "workload",
    "scheduler",
    "policy",
    "metric",
    "direction",
    "value",
]


def synthetic_rows(*, inject_defect: bool, seed: int = 11) -> list[dict[str, str]]:
    """Build paired blocks; block_id is shared across schedulers and policies."""
    rng = random.Random(seed)
    base = {workload: 1000.0 * (index + 1) for index, workload in enumerate(WORKLOADS)}
    rows: list[dict[str, str]] = []

    for workload in WORKLOADS:
        for block in range(BLOCKS):
            block_id = f"{workload}-{block}"
            for policy in POLICIES:
                for scheduler in SCHEDULERS:
                    value = base[workload]
                    if policy == "limited":
                        value *= 0.90
                    if (
                        inject_defect
                        and scheduler == AFFECTED_SCHEDULER
                        and policy == "limited"
                    ):
                        value *= PERFORMANCE_HIT
                    value *= math.exp(rng.gauss(0, 0.01))
                    rows.append(
                        {
                            "block_id": block_id,
                            "workload": workload,
                            "scheduler": scheduler,
                            "policy": policy,
                            "metric": "throughput_ops",
                            "direction": "higher-is-better",
                            "value": f"{value:.3f}",
                        }
                    )
            for scheduler in SCHEDULERS:
                ratio = 0.15 * math.exp(rng.gauss(0, 0.02))
                if inject_defect and scheduler == AFFECTED_SCHEDULER:
                    ratio *= THROTTLE_HIT
                rows.append(
                    {
                        "block_id": f"{workload}-throttle-{block}",
                        "workload": workload,
                        "scheduler": scheduler,
                        "policy": "limited",
                        "metric": "throttled_period_ratio",
                        "direction": "lower-is-better",
                        "value": f"{ratio:.5f}",
                    }
                )
    return rows


def analyse_rows(rows: list[dict[str, str]]) -> fa.AnalysisResult:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blocks.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return fa.analyse(fa.read_blocks(path))


def effect_verdicts(result: fa.AnalysisResult) -> list[fa.Verdict]:
    return [v for v in result.verdicts if v.decision in ("regression", "improvement")]


def check() -> list[str]:
    errors: list[str] = []

    null = analyse_rows(synthetic_rows(inject_defect=False))
    false_positives = effect_verdicts(null)
    if false_positives:
        errors.append(
            "null data produced "
            f"{len(false_positives)} effect verdicts: "
            f"{[(v.workload, v.scheduler) for v in false_positives[:3]]}"
        )
    if null.skipped:
        errors.append(f"null data skipped {len(null.skipped)} contrast(s)")

    affected = analyse_rows(synthetic_rows(inject_defect=True))
    detected = effect_verdicts(affected)
    expected = {(w, AFFECTED_SCHEDULER) for w in WORKLOADS}
    got = {(v.workload, v.scheduler) for v in detected}
    if got != expected:
        errors.append(f"expected exactly {sorted(expected)}, got {sorted(got)}")
    for verdict in detected:
        if verdict.decision != "regression":
            errors.append(f"unexpected {verdict.decision} for {verdict.workload}")
        if verdict.contrast not in ("performance_interaction", "throttling"):
            errors.append(f"unexpected contrast {verdict.contrast}")

    return errors


def main() -> int:
    errors = check()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "dry run OK: null data produced no effects; the injected defect was "
        f"detected on {AFFECTED_SCHEDULER} for all {len(WORKLOADS)} workloads "
        "on both contrast families"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
