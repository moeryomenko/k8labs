#!/usr/bin/env python3
"""Run the stock-versus-custom scheduler delta matrix on a lab worker.

This script is executed as root on the worker by ``run-matrix.sh``. It creates
a dedicated cgroup-v2 parent, runs one scenario at a time for the requested
kernel arm, and writes one JSON object per measured cgroup per repetition plus
a ``system`` row per repetition.

The interesting part is not the raw CPU share (the sibling sched-core-modes
series owns cgroup weights) but the delay distributions and the interaction
with CFS bandwidth and host steal time:

- wakeup / periodic probes report p50, p99, p99.9 and max delay;
- periodic workloads report missed periods and per-thread fairness;
- quota groups report delivered CPU and throttle ratios;
- churn reports task start delay and completion time;
- pingpong reports cross-vCPU round-trip time;
- the system sampler records steal time and the preferred-CPU mask.

Safety: the original steal-governor module state and
``kernel.sched_schedstats`` setting are restored in a ``finally`` block, the
test cgroup tree is removed, and SIGINT/SIGTERM are converted to SystemExit so
the cleanup still runs when the SSH session drops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scenarios import (
    ALL_ARMS,
    DEFAULT_SCENARIOS,
    PARENT_WEIGHT,
    scenario_groups,
    scenario_names,
)

CGROUP_ROOT = Path("/sys/fs/cgroup/sched-delta")
WORK_DIR = Path("/var/tmp/sched-delta")
PROC_STAT = Path("/proc/stat")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PREFERRED_FILE = Path("/sys/devices/system/cpu/preferred")
PRESSURE_CPU = Path("/proc/pressure/cpu")
SCHEDSTATS = Path("/proc/sys/kernel/sched_schedstats")
GOVERNOR_MODULE = Path("/sys/module/steal_governor")
GOVERNOR_PARAMS = GOVERNOR_MODULE / "parameters"

CONFIG_KEYS = (
    "CONFIG_HZ",
    "CONFIG_PREEMPT_LAZY",
    "CONFIG_PREEMPT_DYNAMIC",
    "CONFIG_SCHED_CORE",
    "CONFIG_CFS_BANDWIDTH",
    "CONFIG_CGROUP_SCHED",
    "CONFIG_FAIR_GROUP_SCHED",
    "CONFIG_SCHED_CLASS_EXT",
    "CONFIG_PREFERRED_CPU",
    "CONFIG_STEAL_GOVERNOR",
    "CONFIG_SCHEDSTATS",
)


@dataclass
class Workload:
    kind: str
    group: str
    output: Path
    proc: subprocess.Popen[str] | None = None
    pid: int = 0


@dataclass
class GovernorState:
    loaded: bool
    params: dict[str, int] = field(default_factory=dict)


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def read_boot_id() -> str:
    try:
        return read_text(BOOT_ID).strip()
    except OSError:
        return ""


def cpus() -> int:
    return len(os.sched_getaffinity(0))


def parse_cpulist(value: str) -> list[int]:
    result: list[int] = []
    for part in value.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            result.extend(range(int(start), int(end) + 1))
        else:
            result.append(int(part))
    return result


def read_preferred() -> str:
    try:
        return read_text(PREFERRED_FILE).strip()
    except OSError:
        return ""


def preferred_count(value: str) -> int:
    try:
        return len(parse_cpulist(value))
    except ValueError:
        return 0


def read_pressure() -> float:
    try:
        line = read_text(PRESSURE_CPU).splitlines()[0]
    except (OSError, IndexError):
        return 0.0
    for token in line.split():
        if token.startswith("avg10="):
            try:
                return float(token.split("=", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def read_stat() -> dict[str, Any]:
    """Return aggregate and per-CPU CPU time, including steal."""

    aggregate: dict[str, int] = {}
    per_cpu: dict[int, dict[str, int]] = {}
    ctxt = 0
    for line in read_text(PROC_STAT).splitlines():
        if line.startswith("ctxt "):
            ctxt = int(line.split()[1])
            continue
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if parts[0] == "cpu":
            target = aggregate
        elif parts[0][3:].isdigit():
            target = per_cpu.setdefault(int(parts[0][3:]), {})
        else:
            continue
        values = [int(value) for value in parts[1:]]
        if len(values) < 8:
            continue
        target["total"] = sum(values[:8])
        target["idle"] = values[3] + values[4]
        target["steal"] = values[7]
    if not aggregate:
        raise RuntimeError("no aggregate cpu line in /proc/stat")
    return {"aggregate": aggregate, "per_cpu": per_cpu, "ctxt": ctxt}


def stat_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def delta(key: str) -> int:
        return max(
            0,
            int(after["aggregate"].get(key, 0)) - int(before["aggregate"].get(key, 0)),
        )

    total = delta("total")
    steal = delta("steal")
    idle = delta("idle")
    busy = max(0, total - idle)
    per_cpu = []
    for cpu in sorted(set(before["per_cpu"]) & set(after["per_cpu"])):
        b = before["per_cpu"][cpu]
        a = after["per_cpu"][cpu]
        cpu_total = max(0, int(a.get("total", 0)) - int(b.get("total", 0)))
        cpu_steal = max(0, int(a.get("steal", 0)) - int(b.get("steal", 0)))
        per_cpu.append(
            {
                "cpu": cpu,
                "steal_pct": 100.0 * cpu_steal / cpu_total if cpu_total else 0.0,
            }
        )
    return {
        "total_usec": total,
        "steal_usec": steal,
        "idle_usec": idle,
        "steal_pct_total": 100.0 * steal / total if total else 0.0,
        "steal_pct_busy": 100.0 * steal / busy if busy else 0.0,
        "steal_pct_max_cpu": max((item["steal_pct"] for item in per_cpu), default=0.0),
        "per_cpu": per_cpu,
        "ctxt": int(after["ctxt"]) - int(before["ctxt"]),
    }


class SystemSampler:
    """Sample steal time and the preferred CPU mask during a measurement."""

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self.preferred_supported = PREFERRED_FILE.exists()
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._first: dict[str, Any] | None = None
        self._last: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        stat = read_stat()
        return {
            "t": time.monotonic(),
            "ctxt": stat["ctxt"],
            "total": int(stat["aggregate"]["total"]),
            "steal": int(stat["aggregate"]["steal"]),
            "preferred": read_preferred(),
            "psi": read_pressure(),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._read()
            except (OSError, RuntimeError):
                break
            with self._lock:
                if self._first is None:
                    self._first = sample
                self._last = sample
                self.samples.append(
                    {
                        "t": round(sample["t"], 6),
                        "preferred": sample["preferred"],
                        "psi": sample["psi"],
                    }
                )
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            first = self._first
            last = self._last
            samples = list(self.samples)
        if first is None or last is None:
            return {"samples": 0}
        elapsed = max(0.0, float(last["t"]) - float(first["t"]))
        total = max(0, int(last["total"]) - int(first["total"]))
        steal = max(0, int(last["steal"]) - int(first["steal"]))
        ctxt = max(0, int(last["ctxt"]) - int(first["ctxt"]))
        transitions: list[dict[str, Any]] = []
        previous = None
        counts = []
        for sample in samples:
            value = str(sample["preferred"])
            counts.append(preferred_count(value))
            if previous is not None and value != previous:
                transitions.append(
                    {
                        "t": sample["t"],
                        "preferred": value,
                        "count": preferred_count(value),
                    }
                )
            previous = value
        return {
            "samples": len(samples),
            "elapsed_s": round(elapsed, 6),
            "steal_pct_total": 100.0 * steal / total if total else 0.0,
            "ctxt_per_s": ctxt / elapsed if elapsed > 0 else 0.0,
            "psi_some_avg10_max": max(
                (float(item["psi"]) for item in samples), default=0.0
            ),
            "preferred_first": str(first["preferred"])
            if self.preferred_supported
            else "",
            "preferred_last": str(last["preferred"])
            if self.preferred_supported
            else "",
            "preferred_min": min(counts)
            if self.preferred_supported and counts
            else None,
            "preferred_max": max(counts)
            if self.preferred_supported and counts
            else None,
            "preferred_changes": len(transitions) if self.preferred_supported else 0,
            "preferred_transitions": transitions if self.preferred_supported else [],
        }


def ensure_root() -> None:
    controllers = read_text(Path("/sys/fs/cgroup/cgroup.controllers")).split()
    for controller in ("cpu", "cpuset"):
        if controller not in controllers:
            raise RuntimeError(f"cgroup v2 controller {controller!r} is not available")
    subtree = read_text(Path("/sys/fs/cgroup/cgroup.subtree_control")).split()
    missing = [
        controller for controller in ("cpu", "cpuset") if controller not in subtree
    ]
    if missing:
        raise RuntimeError(
            "root cgroup.subtree_control is missing "
            + ", ".join(missing)
            + "; enable it on the worker before running the matrix"
        )


def clean_cgroup(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            clean_cgroup(child)
    path.rmdir()


def ensure_parent(parent_weight: int, cpu_count: int) -> None:
    if CGROUP_ROOT.exists():
        clean_cgroup(CGROUP_ROOT)
    CGROUP_ROOT.mkdir(parents=True)
    write_text(CGROUP_ROOT / "cpu.weight", f"{parent_weight}\n")
    write_text(CGROUP_ROOT / "cpuset.cpus", f"0-{cpu_count - 1}\n")
    write_text(CGROUP_ROOT / "cgroup.subtree_control", "+cpu +cpuset\n")


def make_group(path: Path, weight: int, cpu_max: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    write_text(path / "cpu.weight", f"{weight}\n")
    if cpu_max:
        write_text(path / "cpu.max", cpu_max + "\n")


def move_to_group(proc: subprocess.Popen[str], group: Path) -> None:
    try:
        write_text(group / "cgroup.procs", f"{proc.pid}\n")
    except OSError as exc:
        proc.kill()
        raise RuntimeError(f"could not move pid {proc.pid} to {group}: {exc}") from exc


def read_group(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("cpu.weight", "cpu.max", "cpuset.cpus"):
        try:
            result[name] = read_text(path / name).strip()
        except OSError:
            pass
    return result


def read_cpu_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in read_text(path).splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                values[fields[0]] = int(fields[1])
            except ValueError:
                pass
    if "usage_usec" not in values:
        raise RuntimeError(f"no usage_usec in {path}")
    return values


def stat_group_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = (
        "usage_usec",
        "user_usec",
        "system_usec",
        "nr_periods",
        "nr_throttled",
        "throttled_usec",
    )
    return {
        key: max(0, int(after.get(key, 0)) - int(before.get(key, 0))) for key in keys
    }


def read_proc_sched(pid: int) -> dict[str, int]:
    """Read the per-task scheduler counters, if the process still exists."""

    try:
        text = read_text(Path(f"/proc/{pid}/sched"))
    except OSError:
        return {}
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"se.nr_migrations", "nr_migrations_cpu_non_preferred"}:
            try:
                result[key] = int(value.strip())
            except ValueError:
                pass
    return result


def sched_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        key: max(0, int(after.get(key, 0)) - int(before.get(key, 0))) for key in after
    }


def read_governor_state() -> GovernorState:
    loaded = GOVERNOR_MODULE.is_dir()
    params: dict[str, int] = {}
    if loaded:
        for name in ("interval_ms", "high_threshold", "low_threshold"):
            try:
                params[name] = int(read_text(GOVERNOR_PARAMS / name).strip())
            except (OSError, ValueError):
                pass
    return GovernorState(loaded=loaded, params=params)


def run_command(
    command: list[str], timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def unload_governor() -> None:
    if GOVERNOR_MODULE.is_dir():
        run_command(["rmmod", "steal_governor"])


def load_governor(interval_ms: int, high: int, low: int) -> None:
    unload_governor()
    run_command(
        [
            "modprobe",
            "steal_governor",
            f"interval_ms={interval_ms}",
            f"high_threshold={high}",
            f"low_threshold={low}",
        ]
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = read_governor_state()
        if state.loaded:
            return
        time.sleep(0.05)
    raise RuntimeError("steal_governor did not appear after modprobe")


def apply_governor(arm: str, args: argparse.Namespace) -> GovernorState:
    original = read_governor_state()
    if arm == "stock":
        if original.loaded:
            raise RuntimeError("stock kernel unexpectedly has steal_governor loaded")
        return original
    if arm == "custom":
        if original.loaded:
            raise RuntimeError(
                "the custom arm requires the steal governor to be unloaded; "
                "unload it or use the custom-nogov arm"
            )
        return original
    if arm == "custom-nogov":
        unload_governor()
        if read_governor_state().loaded:
            raise RuntimeError("steal_governor is still loaded after rmmod")
        return original
    if arm == "custom-gov":
        load_governor(args.gov_interval_ms, args.gov_high, args.gov_low)
        state = read_governor_state()
        expected = {
            "interval_ms": args.gov_interval_ms,
            "high_threshold": args.gov_high,
            "low_threshold": args.gov_low,
        }
        if state.params != expected:
            raise RuntimeError(
                f"governor parameters are {state.params}, expected {expected}"
            )
        return original
    raise RuntimeError(f"unknown arm: {arm}")


def restore_governor(original: GovernorState) -> None:
    current = read_governor_state()
    if original.loaded:
        if current.loaded and current.params == original.params:
            return
        load_governor(
            int(original.params.get("interval_ms", 1000)),
            int(original.params.get("high_threshold", 500)),
            int(original.params.get("low_threshold", 200)),
        )
    elif current.loaded:
        unload_governor()


def set_schedstats(enabled: bool) -> None:
    try:
        write_text(SCHEDSTATS, "1\n" if enabled else "0\n")
    except OSError as exc:
        raise RuntimeError(f"could not set {SCHEDSTATS}: {exc}") from exc


def read_config() -> dict[str, str]:
    release = os.uname().release
    candidates = [
        Path(f"/boot/config-{release}"),
        Path(f"/usr/lib/modules/{release}/config"),
        Path(f"/lib/modules/{release}/config"),
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result: dict[str, str] = {}
        digest = hashlib.sha256()
        for line in candidate.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            digest.update(line.encode("utf-8", "replace"))
            digest.update(b"\n")
            for key in CONFIG_KEYS:
                if line.startswith(key + "="):
                    result[key] = line.split("=", 1)[1]
        result["sha256"] = digest.hexdigest()
        result["path"] = str(candidate)
        return result
    return {}


def compile_workloads(base: Path) -> dict[str, Path]:
    binaries = {
        "cpu": (base / "cpu_burn.c", base / "cpu_burn", []),
        "wakeup": (base / "wakeup_latency.c", base / "wakeup_latency", []),
        "periodic": (base / "periodic_burn.c", base / "periodic_burn", ["-lm"]),
        "churn": (base / "task_churn.c", base / "task_churn", []),
        "pingpong": (base / "pingpong.c", base / "pingpong", []),
    }
    result: dict[str, Path] = {}
    for kind, (source, output, extra) in binaries.items():
        if output.exists() and output.stat().st_mtime >= source.stat().st_mtime:
            result[kind] = output
            continue
        subprocess.run(
            [
                "gcc",
                "-O2",
                "-std=gnu11",
                "-pthread",
                "-o",
                str(output),
                str(source),
                *extra,
            ],
            check=True,
        )
        result[kind] = output
    return result


def workload_command(
    *,
    spec: dict[str, Any],
    binary: Path,
    output: Path,
    duration: float,
    warmup: float,
    go_load: Path,
    go_probe: Path,
    cpus: int,
) -> list[str]:
    kind = str(spec["kind"])
    if kind == "cpu":
        return [
            str(binary),
            "--threads",
            str(int(spec.get("threads", 1))),
            "--duration",
            str(warmup + duration + 0.5),
            "--wait-file",
            str(go_load),
            "--output",
            str(output),
        ]
    if kind == "wakeup":
        return [
            str(binary),
            "--duration",
            str(duration),
            "--interval-us",
            str(int(spec.get("interval_us", 1000))),
            "--wait-file",
            str(go_probe),
            "--output",
            str(output),
        ]
    if kind == "periodic":
        return [
            str(binary),
            "--threads",
            str(int(spec.get("threads", 1))),
            "--period-us",
            str(int(spec.get("period_us", 2000))),
            "--burn-us",
            str(int(spec.get("burn_us", 200))),
            "--duration",
            str(duration),
            "--wait-file",
            str(go_probe),
            "--output",
            str(output),
        ]
    if kind == "churn":
        return [
            str(binary),
            "--rate",
            str(int(spec.get("rate", 200))),
            "--duration",
            str(duration),
            "--work-us",
            str(int(spec.get("work_us", 20))),
            "--wait-file",
            str(go_probe),
            "--output",
            str(output),
        ]
    if kind == "pingpong":
        return [
            str(binary),
            "--duration",
            str(duration),
            "--burn-us",
            str(int(spec.get("burn_us", 0))),
            "--pin",
            "1" if cpus >= 2 else "0",
            "--pin-cpu",
            "1",
            "--wait-file",
            str(go_probe),
            "--output",
            str(output),
        ]
    raise RuntimeError(f"unknown workload kind: {kind}")


def parse_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def merge_workload(row: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
    if kind == "cpu":
        iterations = int(data.get("iterations", 0))
        row["iterations"] = iterations
        row["iterations_per_s"] = (
            iterations / float(row["elapsed_s"]) if row["elapsed_s"] else 0.0
        )
        row["cpu_threads"] = int(data.get("threads", 0))
        return
    if kind in {"wakeup", "periodic", "pingpong"}:
        for key in ("samples", "round_trips", "p50_us", "p99_us", "p999_us", "max_us"):
            if key in data:
                row[key] = data[key]
        if kind == "periodic":
            row["iterations"] = int(data.get("iterations", 0))
            row["overruns"] = int(data.get("overruns", 0))
            row["missed_periods"] = int(data.get("missed_periods", 0))
            samples = int(data.get("samples", 0))
            row["missed_period_rate_pct"] = (
                100.0 * row["missed_periods"] / (samples + row["missed_periods"])
                if samples + row["missed_periods"]
                else 0.0
            )
            row["thread_cv"] = float(data.get("thread_cv", 0.0))
            row["thread_iters"] = data.get("thread_iters", [])
        if kind == "pingpong":
            row["round_trips_per_s"] = float(data.get("round_trips_per_s", 0.0))
            row["pinned"] = int(data.get("pinned", 0))
            row["failed"] = int(data.get("failed", 0))
            row["burn_us"] = int(data.get("burn_us", 0))
        return
    if kind == "churn":
        for key in (
            "rate",
            "work_us",
            "created",
            "completed",
            "tasks_per_s",
            "p50_us",
            "p99_us",
            "p999_us",
            "max_us",
            "completion_p50_us",
            "completion_p99_us",
            "completion_p999_us",
            "completion_max_us",
        ):
            if key in data:
                row[key] = data[key]
        return
    raise RuntimeError(f"unknown workload kind: {kind}")


def stop(proc: subprocess.Popen[str], strict: bool = True) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if not strict or proc.returncode in (0, -15):
        return
    stderr = proc.stderr.read() if proc.stderr is not None else ""
    raise RuntimeError(f"workload exited {proc.returncode}: {stderr.strip()}")


def wait_for(procs: list[Workload], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for workload in procs:
        proc = workload.proc
        if proc is None or proc.poll() is not None:
            continue
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass


def run_scenario(
    *,
    scenario: str,
    arm: str,
    rep: int,
    run_id: str,
    block: str,
    node: str,
    kernel: str,
    cpu_count: int,
    duration: float,
    warmup: float,
    binaries: dict[str, Path],
    scenario_order: list[str],
) -> list[dict[str, Any]]:
    work = WORK_DIR / f"work-{scenario}-{arm}-{rep}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    go_load = work / "go-load"
    go_probe = work / "go-probe"

    groups: dict[str, Path] = {}
    created: list[Path] = []
    workloads: list[Workload] = []

    try:
        for spec in scenario_groups(scenario):
            name = str(spec["name"])
            path = CGROUP_ROOT / f"{scenario}-{arm}-{rep}-{name}"
            created.append(path)
            make_group(path, int(spec["weight"]), str(spec["cpu_max"]))
            groups[name] = path

        for spec in scenario_groups(scenario):
            name = str(spec["name"])
            for index, spec_workload in enumerate(spec["workloads"]):
                kind = str(spec_workload["kind"])
                output = work / f"{name}-{kind}-{index}.json"
                command = workload_command(
                    spec=spec_workload,
                    binary=binaries[kind],
                    output=output,
                    duration=duration,
                    warmup=warmup,
                    go_load=go_load,
                    go_probe=go_probe,
                    cpus=cpu_count,
                )
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                move_to_group(proc, groups[name])
                workloads.append(
                    Workload(
                        kind=kind, group=name, output=output, proc=proc, pid=proc.pid
                    )
                )

        sampler = SystemSampler()
        sampler.start()
        time.sleep(0.5)
        go_load.touch()
        time.sleep(warmup)

        stat_before = read_stat()
        cpu_before = {
            name: read_cpu_stat(path / "cpu.stat") for name, path in groups.items()
        }
        sched_before = {
            workload.pid: read_proc_sched(workload.pid)
            for workload in workloads
            if workload.kind == "pingpong"
        }
        started = time.monotonic()

        go_probe.touch()
        time.sleep(duration)

        elapsed = time.monotonic() - started
        stat_after = read_stat()
        sampler.stop()
        cpu_after = {
            name: read_cpu_stat(path / "cpu.stat") for name, path in groups.items()
        }
        sched_after = {pid: read_proc_sched(pid) for pid in sched_before}

        wait_for(workloads, timeout=3.0)
        for workload in workloads:
            if workload.proc is not None:
                stop(workload.proc)

        system_delta = stat_delta(stat_before, stat_after)
        system = sampler.summary()
        system.update(
            {
                "steal_pct_total": system_delta["steal_pct_total"],
                "steal_pct_busy": system_delta["steal_pct_busy"],
                "steal_pct_max_cpu": system_delta["steal_pct_max_cpu"],
                "ctxt": system_delta["ctxt"],
                "ctxt_per_s": system_delta["ctxt"] / elapsed if elapsed else 0.0,
                "per_cpu_steal": system_delta["per_cpu"],
            }
        )

        timestamp = datetime.now(UTC).isoformat()
        by_name = {str(spec["name"]): spec for spec in scenario_groups(scenario)}
        rows: list[dict[str, Any]] = []
        for name, path in groups.items():
            spec = by_name[name]
            delta = stat_group_delta(cpu_before[name], cpu_after[name])
            row: dict[str, Any] = {
                "run_id": run_id,
                "block": block,
                "arm": arm,
                "node": node,
                "kernel": kernel,
                "cpus": cpu_count,
                "scenario": scenario,
                "scenario_order": ",".join(scenario_order),
                "rep": rep,
                "group": name,
                "kind": "group",
                "weight": int(spec["weight"]),
                "cpu_max": read_group(path).get("cpu.max", ""),
                "usage_usec": delta["usage_usec"],
                "user_usec": delta["user_usec"],
                "system_usec": delta["system_usec"],
                "nr_periods": delta["nr_periods"],
                "nr_throttled": delta["nr_throttled"],
                "throttled_usec": delta["throttled_usec"],
                "elapsed_s": elapsed,
                "timestamp": timestamp,
            }
            row["share_pct"] = (
                100.0 * row["usage_usec"] / (elapsed * cpu_count * 1_000_000.0)
                if elapsed
                else 0.0
            )
            row["delivered_cpu_pct"] = (
                100.0 * row["usage_usec"] / (elapsed * 1_000_000.0) if elapsed else 0.0
            )
            row["throttled_period_pct"] = (
                100.0 * row["nr_throttled"] / row["nr_periods"]
                if row["nr_periods"]
                else 0.0
            )
            row["throttled_usage_pct"] = (
                100.0 * row["throttled_usec"] / row["usage_usec"]
                if row["usage_usec"]
                else 0.0
            )
            for index, spec_workload in enumerate(spec["workloads"]):
                kind = str(spec_workload["kind"])
                data = parse_json(work / f"{name}-{kind}-{index}.json")
                merge_workload(row, kind, data)
            rows.append(row)

        governor = read_governor_state()
        system_row: dict[str, Any] = {
            "run_id": run_id,
            "block": block,
            "arm": arm,
            "node": node,
            "kernel": kernel,
            "cpus": cpu_count,
            "scenario": scenario,
            "scenario_order": ",".join(scenario_order),
            "rep": rep,
            "group": "system",
            "kind": "system",
            "elapsed_s": elapsed,
            "timestamp": timestamp,
            "governor_loaded": governor.loaded,
            "governor_params": governor.params,
            **system,
        }
        for pid, before in sched_before.items():
            after = sched_after.get(pid, {})
            delta_sched = sched_delta(before, after)
            if delta_sched:
                system_row["pingpong_migrations"] = delta_sched.get(
                    "se.nr_migrations", 0
                )
                system_row["pingpong_non_preferred_migrations"] = delta_sched.get(
                    "nr_migrations_cpu_non_preferred", 0
                )
        rows.append(system_row)
        return rows
    finally:
        for workload in workloads:
            if workload.proc is not None:
                stop(workload.proc, strict=False)
        for path in sorted(created, key=lambda p: len(p.parts), reverse=True):
            try:
                clean_cgroup(path)
            except OSError as exc:
                log(f"warning: cgroup cleanup failed for {path}: {exc}")
        shutil.rmtree(work, ignore_errors=True)


def check_environment(arm: str) -> dict[str, Any]:
    ensure_root()
    release = os.uname().release
    kernel_custom = "k8labs" in release
    preferred = PREFERRED_FILE.exists()
    if arm == "stock":
        if kernel_custom:
            raise RuntimeError(f"stock arm is running a custom kernel: {release}")
        if preferred:
            raise RuntimeError("stock arm exposes /sys/devices/system/cpu/preferred")
    elif arm.startswith("custom"):
        if not kernel_custom:
            raise RuntimeError(f"custom arm is running a stock kernel: {release}")
        if not preferred:
            raise RuntimeError(
                "custom arm does not expose /sys/devices/system/cpu/preferred"
            )
    else:
        raise RuntimeError(f"unknown arm: {arm}")

    for tool in ("gcc", "python3", "modprobe", "rmmod"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"required tool not found: {tool}")

    return {
        "arm": arm,
        "kernel": release,
        "node": os.uname().nodename,
        "cpus": cpus(),
        "preferred": read_preferred(),
        "governor": read_governor_state().__dict__,
        "sched_schedstats": read_text(SCHEDSTATS).strip()
        if SCHEDSTATS.exists()
        else "",
        "config": read_config(),
        "work_dir": str(WORK_DIR),
    }


def plan(args: argparse.Namespace, scenarios: list[str]) -> dict[str, Any]:
    reps = []
    for rep in range(1, args.reps + 1):
        order = list(scenarios)
        random.Random(args.seed + rep).shuffle(order)
        reps.append({"rep": rep, "scenario_order": order})
    return {
        "run_id": args.run_id,
        "arm": args.arm,
        "block": args.block,
        "reps": reps,
        "scenarios": {name: scenario_groups(name) for name in scenarios},
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "parent_weight": args.parent_weight,
        "cpus": cpus(),
        "kernel": os.uname().release,
        "node": os.uname().nodename,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=ALL_ARMS)
    parser.add_argument("--block", default="")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--cooldown", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=20261024)
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    parser.add_argument("--parent-weight", type=int, default=PARENT_WEIGHT)
    parser.add_argument("--gov-interval-ms", type=int, default=1000)
    parser.add_argument("--gov-high", type=int, default=500)
    parser.add_argument("--gov-low", type=int, default=200)
    parser.add_argument("--out", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.reps < 1:
        raise SystemExit("--reps must be >= 1")
    if args.duration <= 0:
        raise SystemExit("--duration must be > 0")
    if args.warmup < 0:
        raise SystemExit("--warmup must be >= 0")
    if args.parent_weight <= 0 or args.parent_weight > 10000:
        raise SystemExit("--parent-weight must be in 1..10000")
    if not (100 <= args.gov_interval_ms <= 100000):
        raise SystemExit("--gov-interval-ms must be in 100..100000")
    if not (0 <= args.gov_low < args.gov_high < 10000):
        raise SystemExit("governor thresholds require 0 <= low < high < 10000")

    scenarios = [name for name in args.scenarios.split(",") if name]
    if not scenarios:
        raise SystemExit("at least one scenario is required")
    for name in scenarios:
        if name not in scenario_names():
            raise SystemExit(f"unknown scenario: {name}")
    if "steal" in scenarios and not args.arm.startswith("custom-"):
        raise SystemExit(
            "the steal scenario is a steal-governor contrast; use "
            "--arm custom-nogov or --arm custom-gov"
        )
    if not args.run_id:
        args.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    if args.plan:
        print(json.dumps(plan(args, scenarios), indent=2, sort_keys=True))
        return 0

    if os.geteuid() != 0:
        raise SystemExit("run_matrix.py must run as root on the worker")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    environment = check_environment(args.arm)
    if args.check:
        print(json.dumps(environment, indent=2, sort_keys=True))
        return 0

    binaries = compile_workloads(Path(__file__).resolve().parent)
    node = str(environment["node"])
    kernel = str(environment["kernel"])
    cpu_count = int(environment["cpus"])

    original_governor = apply_governor(args.arm, args)
    original_schedstats = (
        SCHEDSTATS.read_text(encoding="utf-8").strip() if SCHEDSTATS.exists() else "0"
    )
    steer_schedstats = "steal" in scenarios and args.arm.startswith("custom")
    all_rows: list[dict[str, Any]] = []
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        try:
            restore_governor(original_governor)
        finally:
            if steer_schedstats:
                try:
                    set_schedstats(original_schedstats == "1")
                except RuntimeError as exc:
                    log(f"warning: could not restore sched_schedstats: {exc}")
            try:
                if CGROUP_ROOT.exists():
                    clean_cgroup(CGROUP_ROOT)
            except OSError as exc:
                log(f"warning: could not remove {CGROUP_ROOT}: {exc}")

    def on_signal(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log(
        f"run {args.run_id} arm={args.arm} block={args.block} node={node} "
        f"kernel={kernel} cpus={cpu_count} scenarios={scenarios}"
    )
    try:
        if steer_schedstats:
            set_schedstats(True)
        ensure_parent(args.parent_weight, cpu_count)
        for rep in range(1, args.reps + 1):
            order = list(scenarios)
            random.Random(args.seed + rep).shuffle(order)
            for scenario in order:
                log(f"rep {rep}/{args.reps}: {scenario}")
                rows = run_scenario(
                    scenario=scenario,
                    arm=args.arm,
                    rep=rep,
                    run_id=args.run_id,
                    block=args.block,
                    node=node,
                    kernel=kernel,
                    cpu_count=cpu_count,
                    duration=args.duration,
                    warmup=args.warmup,
                    binaries=binaries,
                    scenario_order=order,
                )
                all_rows.extend(rows)
                time.sleep(args.cooldown)
    finally:
        restore()

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in all_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    meta = {
        "run_id": args.run_id,
        "arm": args.arm,
        "block": args.block,
        "node": node,
        "kernel": kernel,
        "cpus": cpu_count,
        "scenarios": scenarios,
        "reps": args.reps,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "parent_weight": args.parent_weight,
        "seed": args.seed,
        "governor": {
            "before": original_governor.__dict__,
            "after": read_governor_state().__dict__,
            "parameters": {
                "interval_ms": args.gov_interval_ms,
                "high_threshold": args.gov_high,
                "low_threshold": args.gov_low,
            },
        },
        "sched_schedstats": {
            "before": original_schedstats,
            "after": SCHEDSTATS.read_text(encoding="utf-8").strip()
            if SCHEDSTATS.exists()
            else "",
            "enabled_for_run": steer_schedstats,
        },
        "config": environment["config"],
        "boot_id": read_boot_id(),
        "preferred": read_preferred(),
        "finished": datetime.now(UTC).isoformat(),
        "rows": len(all_rows),
    }
    Path(args.meta).parent.mkdir(parents=True, exist_ok=True)
    Path(args.meta).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log(f"wrote {len(all_rows)} rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
