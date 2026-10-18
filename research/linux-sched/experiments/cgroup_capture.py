"""Capture helpers for cgroup v2 cpu.stat counters (reuses cgroup_stats)."""

from __future__ import annotations

import cgroup_stats


def capture_cpu_stat(text):
    return cgroup_stats.parse_cpu_stat(text)


def capture_container_cpu_stat(text):
    return cgroup_stats.parse_cpu_stat(text)


def capture_pod_cpu_stats(container_stats):
    return cgroup_stats.pod_cpu_stat(container_stats)


def capture_mixed_pod_total(container_stats):
    return cgroup_stats.mixed_pod_total(container_stats)


def read_cpu_stat(path):
    return cgroup_stats.read_cpu_stat_file(path)


# Re-export everything from cgroup_stats for convenience.
from cgroup_stats import *  # noqa: F401,F403
