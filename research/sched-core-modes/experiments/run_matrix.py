#!/usr/bin/env python3
"""Run the cgroup-mode workload matrix on the local node.

This script is executed as root on a lab worker. It creates a dedicated
cgroup-v2 parent, runs the workload matrix for each scheduler mode, and writes
one JSON object per measured cgroup per repetition. The host-side runner copies
it to the worker and pulls the JSONL result back.

Safety: the original cgroup_mode is restored in a finally block and the test
cgroup tree is removed. SIGINT/SIGTERM are converted to SystemExit so the
finally block still runs when the SSH session drops.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from scenarios import MODES, SCENARIOS, parse_mode_file

CGROUP_ROOT = Path("/sys/fs/cgroup/sched-modes")
MODE_FILE = Path("/sys/kernel/debug/sched/cgroup_mode")
PROC_STAT = Path("/proc/stat")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
WORK_DIR = Path("/var/tmp/sched-modes")


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def read_mode() -> str:
    return parse_mode_file(read_text(MODE_FILE))


def set_mode(mode: str, settle: float = 0.5) -> None:
    if mode not in MODES:
        raise RuntimeError(f"refusing to write unknown cgroup mode {mode!r}")
    write_text(MODE_FILE, mode + "\n")
    time.sleep(settle)
    actual = read_mode()
    if actual != mode:
        raise RuntimeError(f"mode write requested {mode!r}, read back {actual!r}")


def cpus() -> int:
    return len(os.sched_getaffinity(0))


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


def stat_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = (
        "usage_usec",
        "user_usec",
        "system_usec",
        "nr_periods",
        "nr_throttled",
        "throttled_usec",
    )
    result: dict[str, int] = {}
    for key in keys:
        result[key] = max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
    return result


def read_ctxt() -> int:
    for line in read_text(PROC_STAT).splitlines():
        if line.startswith("ctxt "):
            return int(line.split()[1])
    raise RuntimeError("no ctxt line in /proc/stat")


def parse_json(path: Path) -> dict:
    try:
        return json.loads(read_text(path))
    except (OSError, json.JSONDecodeError):
        return {}


def clean_cgroup(path: Path) -> None:
    if not path.exists():
        return
    # The caller has already stopped its processes. A child that still holds a
    # process makes rmdir fail; surface that instead of reusing stale counters.
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            clean_cgroup(child)
    path.rmdir()


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


def ensure_parent(parent_weight: int, cpuset: str) -> None:
    if CGROUP_ROOT.exists():
        clean_cgroup(CGROUP_ROOT)
    CGROUP_ROOT.mkdir(parents=True)
    write_text(CGROUP_ROOT / "cpu.weight", f"{parent_weight}\n")
    write_text(CGROUP_ROOT / "cpuset.cpus", cpuset + "\n")
    write_text(CGROUP_ROOT / "cgroup.subtree_control", "+cpu +cpuset\n")


def make_group(
    path: Path, weight: int, cpu_max: str | None = None, subtree: bool = False
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    write_text(path / "cpu.weight", f"{weight}\n")
    if cpu_max:
        write_text(path / "cpu.max", cpu_max + "\n")
    if subtree:
        write_text(path / "cgroup.subtree_control", "+cpu +cpuset\n")


def read_group(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("cpu.weight", "cpu.max", "cpuset.cpus"):
        try:
            result[name] = read_text(path / name).strip()
        except OSError:
            pass
    return result


def start(cmd: list[str], cgroup: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    try:
        write_text(cgroup / "cgroup.procs", f"{proc.pid}\n")
    except OSError as exc:
        proc.kill()
        raise RuntimeError(f"could not move pid {proc.pid} to {cgroup}: {exc}") from exc
    return proc


def wait_for(
    procs: list[tuple[str, subprocess.Popen[str], Path]], timeout: float
) -> None:
    """Give the workloads time to finish and write their JSON output.

    The measurement window ends first, so this CPU time is outside the sample;
    it only prevents SIGTERM from truncating the workload's own result file.
    """

    deadline = time.monotonic() + timeout
    for _, proc, _ in procs:
        remaining = deadline - time.monotonic()
        if proc.poll() is None and remaining > 0:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass


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


def compile_workloads(base: Path) -> tuple[Path, Path]:
    cpu_burn = base / "cpu_burn"
    wakeup = base / "wakeup_latency"
    for source, output in (
        (base / "cpu_burn.c", cpu_burn),
        (base / "wakeup_latency.c", wakeup),
    ):
        if output.exists() and output.stat().st_mtime >= source.stat().st_mtime:
            continue
        subprocess.run(
            ["gcc", "-O2", "-pthread", "-o", str(output), str(source)],
            check=True,
        )
    return cpu_burn, wakeup


def group_specs(scenario_name: str) -> list[dict[str, object]]:
    """Return every cgroup that the runner creates and measures."""

    scenario = SCENARIOS[scenario_name]
    if scenario["kind"] == "nested":
        specs: list[dict[str, object]] = []
        for parent in scenario["parents"]:
            specs.append(
                {
                    "name": str(parent["name"]),
                    "kind": "parent",
                    "parent": "",
                    "weight": int(parent["weight"]),
                    "threads": int(parent["threads"]),
                    "cpu_max": "",
                    "workload": "cpu" if not parent["children"] else "none",
                    "subtree": bool(parent["children"]),
                }
            )
            for child in parent["children"]:
                specs.append(
                    {
                        "name": f"{parent['name']}-{child['name']}",
                        "kind": "child",
                        "parent": str(parent["name"]),
                        "weight": int(child["weight"]),
                        "threads": int(child["threads"]),
                        "cpu_max": "",
                        "workload": "cpu",
                        "subtree": False,
                    }
                )
        return specs

    return [
        {
            "name": str(group["name"]),
            "kind": "group",
            "parent": "",
            "weight": int(group["weight"]),
            "threads": int(group["threads"]),
            "cpu_max": str(group.get("cpu_max", "")),
            "workload": str(group.get("workload", "cpu")),
            "subtree": False,
        }
        for group in scenario["groups"]
    ]


def run_scenario(
    *,
    scenario_name: str,
    mode: str,
    rep: int,
    duration: float,
    warmup: float,
    run_id: str,
    node: str,
    kernel: str,
    cpu_count: int,
    cpu_burn: Path,
    wakeup: Path,
    mode_order: list[str],
) -> list[dict[str, object]]:
    work = WORK_DIR / f"work-{scenario_name}-{mode}-{rep}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    go_load = work / "go-load"
    go_probe = work / "go-probe"

    groups: dict[str, Path] = {}
    created: list[Path] = []
    procs: list[tuple[str, subprocess.Popen[str], Path]] = []

    def add_group(spec: dict[str, object]) -> Path:
        name = str(spec["name"])
        parent = str(spec["parent"])
        if parent:
            # The nested hierarchy is the point of this scenario: the pod
            # children must be children of the QoS parent, not siblings.
            child_name = name.removeprefix(f"{parent}-")
            path = groups[parent] / child_name
        else:
            path = CGROUP_ROOT / f"{scenario_name}-{mode}-{rep}-{name}"
        created.append(path)
        make_group(
            path,
            int(spec["weight"]),
            str(spec["cpu_max"]) or None,
            bool(spec["subtree"]),
        )
        groups[name] = path
        return path

    try:
        specs = group_specs(scenario_name)
        for spec in specs:
            add_group(spec)

        for spec in specs:
            workload = str(spec["workload"])
            if workload == "none":
                continue
            name = str(spec["name"])
            output = work / f"{name}.json"
            if workload == "latency":
                cmd = [
                    str(wakeup),
                    "--duration",
                    str(duration),
                    "--interval-us",
                    "1000",
                    "--wait-file",
                    str(go_probe),
                    "--output",
                    str(output),
                ]
            elif workload == "cpu":
                cmd = [
                    str(cpu_burn),
                    "--threads",
                    str(spec["threads"]),
                    "--duration",
                    str(warmup + duration),
                    "--wait-file",
                    str(go_load),
                    "--output",
                    str(output),
                ]
            else:
                raise RuntimeError(f"unknown workload {workload!r} in {scenario_name}")
            procs.append((name, start(cmd, groups[name]), output))

        time.sleep(0.5)
        go_load.touch()
        time.sleep(warmup)

        before = {
            name: read_cpu_stat(path / "cpu.stat") for name, path in groups.items()
        }
        ctxt_before = read_ctxt()

        if scenario_name == "latency":
            go_probe.touch()

        time.sleep(duration)

        after = {
            name: read_cpu_stat(path / "cpu.stat") for name, path in groups.items()
        }
        ctxt_after = read_ctxt()

        # The workloads were started for warmup+duration; the measurement ends
        # at the same offset, but process startup can lag the go-file by a few
        # milliseconds. Wait outside the sample so their result files close.
        wait_for(procs, timeout=3.0)
        for _, proc, _ in procs:
            stop(proc)

        timestamp = datetime.now(UTC).isoformat()
        by_name = {str(spec["name"]): spec for spec in specs}
        rows: list[dict[str, object]] = []
        for name, path in groups.items():
            spec = by_name[name]
            delta = stat_delta(before[name], after[name])
            output = work / f"{name}.json"
            row: dict[str, object] = {
                "run_id": run_id,
                "node": node,
                "kernel": kernel,
                "cpus": cpu_count,
                "mode": mode,
                "mode_order": ",".join(mode_order),
                "scenario": scenario_name,
                "rep": rep,
                "group": name,
                "kind": str(spec["kind"]),
                "parent": str(spec["parent"]),
                "weight": int(spec["weight"]),
                "threads": int(spec["threads"]),
                "cpu_max": read_group(path).get("cpu.max", ""),
                "cpu_weight_read": read_group(path).get("cpu.weight", ""),
                "usage_usec": delta["usage_usec"],
                "user_usec": delta["user_usec"],
                "system_usec": delta["system_usec"],
                "nr_periods": delta["nr_periods"],
                "nr_throttled": delta["nr_throttled"],
                "throttled_usec": delta["throttled_usec"],
                "ctxt": ctxt_after - ctxt_before,
                "elapsed_s": duration,
                "timestamp": timestamp,
            }
            if output.exists():
                row.update(parse_json(output))
            rows.append(row)
        return rows
    finally:
        for _, proc, _ in procs:
            stop(proc, strict=False)
        for path in sorted(created, key=lambda p: len(p.parts), reverse=True):
            try:
                clean_cgroup(path)
            except OSError as exc:
                log(f"warning: cgroup cleanup failed for {path}: {exc}")
        shutil.rmtree(work, ignore_errors=True)


def read_boot_id() -> str:
    try:
        return read_text(BOOT_ID).strip()
    except OSError:
        return ""


def check_environment() -> dict[str, object]:
    ensure_root()
    if not MODE_FILE.is_file():
        raise RuntimeError(f"cgroup_mode is not available: {MODE_FILE}")
    cpu_burn = WORK_DIR / "cpu_burn"
    wakeup = WORK_DIR / "wakeup_latency"
    for tool in ("gcc", "python3"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"required tool not found: {tool}")
    return {
        "mode": read_mode(),
        "cpus": cpus(),
        "kernel": os.uname().release,
        "node": os.uname().nodename,
        "boot_id": read_boot_id(),
        "cpu_weight": read_text(CGROUP_ROOT.parent / "cpu.weight").strip()
        if (CGROUP_ROOT.parent / "cpu.weight").is_file()
        else "",
        "work_dir": str(WORK_DIR),
        "cpu_burn": str(cpu_burn),
        "wakeup_latency": str(wakeup),
    }


def plan(
    args: argparse.Namespace, scenarios: list[str], modes: list[str]
) -> dict[str, object]:
    reps = []
    for rep in range(1, args.reps + 1):
        order = list(modes)
        random.Random(args.seed + rep).shuffle(order)
        reps.append({"rep": rep, "mode_order": order})
    return {
        "run_id": args.run_id,
        "reps": reps,
        "scenarios": {name: group_specs(name) for name in scenarios},
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "modes": modes,
        "cpus": cpus(),
        "kernel": os.uname().release,
        "node": os.uname().nodename,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--cooldown", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20261024)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--out", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--parent-weight", type=int, default=300)
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
    if args.cooldown < 0:
        raise SystemExit("--cooldown must be >= 0")

    scenarios = [name for name in args.scenarios.split(",") if name]
    modes = [name for name in args.modes.split(",") if name]
    if not scenarios:
        raise SystemExit("at least one scenario is required")
    if not modes:
        raise SystemExit("at least one mode is required")
    for name in scenarios:
        if name not in SCENARIOS:
            raise SystemExit(f"unknown scenario: {name}")
    for mode in modes:
        if mode not in MODES:
            raise SystemExit(f"unknown mode: {mode}")
    if not args.run_id:
        args.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    if args.plan:
        print(json.dumps(plan(args, scenarios, modes), indent=2, sort_keys=True))
        return 0

    if os.geteuid() != 0:
        raise SystemExit("run_matrix.py must run as root on the worker")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    environment = check_environment()
    if args.check:
        print(json.dumps(environment, indent=2, sort_keys=True))
        return 0

    cpu_burn, wakeup = compile_workloads(Path(__file__).resolve().parent)
    node = str(environment["node"])
    kernel = str(environment["kernel"])
    cpu_count = int(environment["cpus"])
    original_mode = str(environment["mode"])
    started = datetime.now(UTC).isoformat()
    all_rows: list[dict[str, object]] = []
    log(
        f"run {args.run_id} node={node} kernel={kernel} cpus={cpu_count} "
        f"modes={modes} scenarios={scenarios}"
    )
    ensure_parent(args.parent_weight, f"0-{cpu_count - 1}")

    restored_mode = ""
    try:
        for rep in range(1, args.reps + 1):
            order = list(modes)
            random.Random(args.seed + rep).shuffle(order)
            log(f"rep {rep}/{args.reps} mode order: {' '.join(order)}")
            for mode in order:
                set_mode(mode)
                log(f"  mode={mode} readback={read_mode()}")
                for index, scenario_name in enumerate(scenarios):
                    log(f"    scenario={scenario_name}")
                    rows = run_scenario(
                        scenario_name=scenario_name,
                        mode=mode,
                        rep=rep,
                        duration=args.duration,
                        warmup=args.warmup,
                        run_id=args.run_id,
                        node=node,
                        kernel=kernel,
                        cpu_count=cpu_count,
                        cpu_burn=cpu_burn,
                        wakeup=wakeup,
                        mode_order=order,
                    )
                    all_rows.extend(rows)
                    if index + 1 < len(scenarios):
                        time.sleep(args.cooldown)
            set_mode(original_mode)
            restored_mode = original_mode
    finally:
        try:
            set_mode(original_mode)
            restored_mode = original_mode
        except Exception as exc:  # noqa: BLE001
            log(f"warning: could not restore mode {original_mode}: {exc}")
        try:
            if CGROUP_ROOT.exists():
                clean_cgroup(CGROUP_ROOT)
        except OSError as exc:
            log(f"warning: could not remove {CGROUP_ROOT}: {exc}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in all_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    temporary.replace(out)

    meta = {
        "run_id": args.run_id,
        "node": node,
        "kernel": kernel,
        "cpus": cpu_count,
        "boot_id": environment["boot_id"],
        "reps": args.reps,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "seed": args.seed,
        "modes": modes,
        "scenarios": scenarios,
        "parent_weight": args.parent_weight,
        "original_mode": original_mode,
        "restored_mode": restored_mode,
        "started": started,
        "finished": datetime.now(UTC).isoformat(),
        "rows": len(all_rows),
    }
    Path(args.meta).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log(f"wrote {len(all_rows)} rows to {out}")
    return 0


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    raise SystemExit(main())
