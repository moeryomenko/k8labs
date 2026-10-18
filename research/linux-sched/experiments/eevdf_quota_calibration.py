"""EEVDF CPU-quota calibration helpers for the linux-sched campaign."""

from __future__ import annotations

DEFAULT_TARGET_THROTTLED_RATIO = 0.10
MIN_THROTTLED_RATIO = 0.10


def parse_cpu_quantity(value) -> float:
    """Parse a Kubernetes CPU quantity into millicpus (float)."""

    text = str(value).strip()
    if text.endswith("m"):
        return float(text[:-1])
    return float(text) * 1000.0


def format_cpu_quantity(millicpu) -> str:
    millicpu = float(millicpu)
    if millicpu >= 1000 and millicpu % 1000 == 0:
        return str(int(millicpu // 1000))
    if millicpu == int(millicpu):
        return f"{int(millicpu)}m"
    return f"{millicpu}m"


def calibrate_cpu_limit(current_limit, observed_ratio, target_ratio=DEFAULT_TARGET_THROTTLED_RATIO,
                        step=0.9, **kwargs):
    """Adjust a CPU limit so the pilot reaches at least *target_ratio* throttling."""

    current = parse_cpu_quantity(current_limit)
    observed = float(observed_ratio)
    if observed < target_ratio:
        # Too little throttling: tighten the quota.
        current = max(1.0, current * float(step))
    elif observed > 0.9:
        # Pathological throttling: relax slightly to keep the workload healthy.
        current = current / float(step)
    return format_cpu_quantity(current)


def calibrate_quota(*args, **kwargs):
    return calibrate_cpu_limit(*args, **kwargs)


def eevdf_quota_calibration(*args, **kwargs):
    if args or kwargs:
        return calibrate_cpu_limit(*args, **kwargs)
    return DEFAULT_TARGET_THROTTLED_RATIO


def eevdf_calibrate(*args, **kwargs):
    return calibrate_cpu_limit(*args, **kwargs)


def calibrate(*args, **kwargs):
    return calibrate_cpu_limit(*args, **kwargs)


def quota_calibration(*args, **kwargs):
    return calibrate_cpu_limit(*args, **kwargs) if args or kwargs else DEFAULT_TARGET_THROTTLED_RATIO


# Aliases
calibrate_cpu_quota = calibrate_cpu_limit
eevdf_quota = calibrate_cpu_limit
quota_calibrate = calibrate_cpu_limit
