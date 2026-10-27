#!/usr/bin/env python3
"""
Phase 7 Topic 7.6: kernel scheduler comparison

Compares the schedulers a lab node can expose, with identical workloads:

  cfs       the stock CFS group scheduler, in all five CFS group-weight modes
            (`up`, `smp`, `concur`, `max`, `tasks`) that the custom kernel
            exposes through /sys/kernel/debug/sched/cgroup_mode
  scx_lavd  the sched_ext scheduler scx_lavd in `--performance` mode, over the
            same CFS modes

Why both axes: the CFS modes and sched_ext answer different questions. The
modes only change how a cgroup's fair-share weight is computed, so they move
CPU splits between groups but cannot change wakeup latency or quota
enforcement. sched_ext replaces the scheduling class entirely, so it can.

The two are not mutually exclusive in the kernel; sched_ext simply takes every
task away from CFS while it is enabled. Running the matrix under each arm
shows which of the two changes actually moves the numbers.

The workload matrix, the cgroup tree and cpu_burn/wakeup_latency come from
research/sched-core-modes/experiments. The only change is that the runner
takes the cgroup mount and the debugfs root from the environment, because the
matrix executes inside a node-access pod whose /sys is its own namespace. The
pod first enters the node's scheduler view with phase7/lib/node_view.sh.

Evidence:
    evidence/phase7/kernel-comparison/*.jsonl   one row per measured cgroup
    evidence/phase7/kernel-comparison/*.json    summary and environment

Usage:
    python3 test-kernel-comparison.py [--duration 8] [--reps 2]
                                      [--scenarios weighted_threads,...]
                                      [--skip-scx]

Environment:
    PHASE7_NAMESPACE  (default: cpu-limits-research)
    PHASE7_NODE       (default: the node already running 7.3.0-k8labs)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import node_access as na  # noqa: E402

RESEARCH = Path(__file__).parent.parent.parent
EVIDENCE_DIR = RESEARCH / "evidence" / "phase7" / "kernel-comparison"
MATRIX_SRC = RESEARCH.parent / "sched-core-modes" / "experiments"
NODE_VIEW = RESEARCH / "phase7" / "lib" / "node_view.sh"

NAMESPACE = "cpu-limits-research"
PROBE_POD = "node-probe"
# The pod needs Python for the matrix and nothing else: the two workload
# helpers are built as static binaries on the workstation and copied in, so
# no compiler is installed on the node. The node has about 1 GiB free and a
# gcc install takes 600 MiB of it.
PROBE_IMAGE = "docker.io/library/fedora:44"
PROBE_NODE = "k8labs-md-custom-4vcpu-qq498-zlz6p-6n655"
WORK_DIR = "/var/tmp/sched-modes"
# The host cgroup2 mount inside the node-access pod. The pod's own
# /sys/fs/cgroup is a private, read-only cgroup namespace, so the runner is
# told explicitly which tree to create its groups in.
HOST_CGROUP = "/host/sys/fs/cgroup"
# Scheduler state and the CFS group mode are always readable at these paths:
# node_view.sh only creates a private copy at the standard locations for the
# benefit of libbpf and the matrix. Polling them needs no namespace.
HOST_SCHED_EXT = "/host/sys/kernel/sched_ext"
HOST_DEBUGFS = "/host/sys/kernel/debug"
# The preemption model is switchable at runtime because the kernel is built
# with PREEMPT_DYNAMIC and PREEMPT_LAZY=y. Writing "full" or "lazy" flips the
# static key that decides whether a wakeup may preempt a task in a lazy
# preemption section; the file reports the active mode in parentheses.
PREEMPT_FILE = f"{HOST_DEBUGFS}/sched/preempt"

MATRIX_FILES = ("run_matrix.py", "scenarios.py", "cpu_burn.c", "wakeup_latency.c")
WORKLOADS = ("cpu_burn", "wakeup_latency")
MODES = ("up", "smp", "concur", "max", "tasks")
SCENARIOS = ("weighted_threads", "qos_nested", "quota", "latency")

# The scheduler is not part of this repository: it is built from the scx tree
# at ../scx/.worktrees/k8labs-7.3.0-k8labs. The harness copies it into the pod
# when the pod does not already hold that exact build, so a fresh pod does not
# turn into a silent "not staged" failure.
SCX_BUILD = Path(
    os.environ.get(
        "PHASE7_SCX_BUILD",
        "/home/eryoma/workspace/blog/scx/.worktrees/k8labs-7.3.0-k8labs/target/release",
    )
)

# Each scheduler is built from the scx tree and installed under /var/tmp in
# the pod. The cell schedulers resolve their parent themselves: mitosis
# prepends /sys/fs/cgroup to whatever is given, so the argument is the
# directory name relative to that mount. node_view.sh binds the host tree
# there for exactly this reason.
CELL_PARENT = "/sched-modes"
CELL_PARENT_PATH = f"{HOST_CGROUP}{CELL_PARENT}"

SCHEDULERS = {
    "lavd": {
        "name": "scx_lavd-performance",
        "build": "scx_lavd",
        "args": ["--performance"],
        "cpu_max": False,
    },
    # scx_lavd replaces the fair-class scheduler, and with it the CFS
    # bandwidth control that turns cpu.max into throttling: without this flag
    # a cgroup's quota is simply not enforced. Upstream calls the flag highly
    # experimental, so the difference is measured, not assumed.
    "lavd-bw": {
        "name": "scx_lavd-performance-bw",
        "build": "scx_lavd",
        "args": ["--performance", "--enable-cpu-bw"],
        "cpu_max": True,
    },
    "bpfland": {
        "name": "scx_bpfland",
        "build": "scx_bpfland",
        "args": [],
        "cpu_max": False,
    },
    "mitosis": {
        "name": "scx_mitosis",
        "build": "scx_mitosis",
        "args": ["--cell-parent-cgroup", CELL_PARENT],
        "cpu_max": False,
    },
    "mavd": {
        "name": "scx_mavd",
        "build": "scx_mavd",
        "args": ["--performance"],
        "cpu_max": False,
    },
    "nitosis": {
        "name": "scx_nitosis",
        "build": "scx_nitosis",
        "args": ["--cell-parent-cgroup", CELL_PARENT],
        "cpu_max": False,
    },
}

def kubectl(*args, stdin=None, timeout=300):
    return subprocess.run(
        ["kubectl", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def exec_probe(command, timeout=300):
    return kubectl(
        "exec", "-n", NAMESPACE, PROBE_POD, "--", "sh", "-c", command, timeout=timeout
    )


def apply(manifest):
    return kubectl("apply", "-f", "-", stdin=json.dumps(manifest))


def probe_manifest(node):
    """Node-access pod: host paths mounted, exactly the capabilities needed.

    Privileged pods are rejected by this cluster (mknodat /dev/loop0 fails), so
    the pod asks for the individual capabilities instead: SYS_ADMIN for the
    bind mounts of debugfs/BTF/sched_ext, BPF and PERFMON for sched_ext,
    IPC_LOCK for the scheduler's locked memory, SYS_RESOURCE for RLIMIT.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": PROBE_POD,
            "namespace": NAMESPACE,
            "labels": {"phase7": "7.6"},
        },
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "hostPID": True,
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "probe",
                    "image": PROBE_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["sleep", "7200"],
                    "securityContext": {
                        "capabilities": {
                            "add": [
                                "SYS_ADMIN",
                                "SYS_RESOURCE",
                                "SYS_PTRACE",
                                "BPF",
                                "PERFMON",
                                "IPC_LOCK",
                            ]
                        },
                        "seccompProfile": {"type": "Unconfined"},
                    },
                    "volumeMounts": [
                        {"name": "host", "mountPath": "/host"},
                        {"name": "debugfs", "mountPath": "/host-dbg"},
                        {"name": "hostcgroup", "mountPath": HOST_CGROUP},
                        {"name": "modules", "mountPath": "/lib/modules"},
                    ],
                    # A limit here would make the pod itself a throttled
                    # cgroup, which biases every measurement it collects.
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "4000m", "memory": "1Gi"},
                    },
                }
            ],
            "volumes": [
                {"name": "host", "hostPath": {"path": "/"}},
                {"name": "debugfs", "hostPath": {"path": "/sys/kernel/debug"}},
                {"name": "hostcgroup", "hostPath": {"path": "/sys/fs/cgroup"}},
                {"name": "modules", "hostPath": {"path": "/lib/modules"}},
            ],
        },
    }


def probe_ready():
    phase = kubectl(
        "get", "pod", PROBE_POD, "-n", NAMESPACE, "-o", "jsonpath={.status.phase}"
    ).stdout.strip()
    ready = kubectl(
        "get",
        "pod",
        PROBE_POD,
        "-n",
        NAMESPACE,
        "-o",
        "jsonpath={.status.containerStatuses[0].ready}",
    ).stdout.strip()
    return ready == "true", phase


def ensure_probe(node):
    """Return the pod the matrix runs in, creating it when needed."""
    ready, phase = probe_ready()
    if ready:
        owner = kubectl(
            "get",
            "pod",
            PROBE_POD,
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.spec.nodeName}",
        ).stdout.strip()
        if owner != node:
            return None, f"{PROBE_POD} runs on {owner}, not {node}"
        return PROBE_POD, None

    print(f"  {PROBE_POD} is {phase or 'absent'}; creating it on {node}")
    kubectl(
        "delete",
        "pod",
        PROBE_POD,
        "-n",
        NAMESPACE,
        "--ignore-not-found",
        "--force",
        "--grace-period=0",
    )
    applied = apply(probe_manifest(node))
    if applied.returncode != 0:
        return None, f"pod rejected: {applied.stderr.strip()[:300]}"
    deadline = time.time() + 180
    while time.time() < deadline:
        ready, phase = probe_ready()
        if ready:
            return PROBE_POD, None
        if phase in ("Failed", "Succeeded"):
            reason = kubectl(
                "get",
                "events",
                "-n",
                NAMESPACE,
                "--field-selector",
                f"involvedObject.name={PROBE_POD}",
                "-o",
                "jsonpath={.items[-1].message}",
            ).stdout.strip()
            return None, f"pod {phase}: {reason[:300]}"
        time.sleep(3)
    return None, "pod not ready in 180s"


def copy_into_pod(local, remote):
    """kubectl cp, extracting to a temp name first.

    The pod may already hold an older binary; copying straight over a file
    that is currently executable fails with ETXTBSY, and tar refuses to
    overwrite without --overwrite, so the target is removed first.
    """
    exec_probe(f"rm -f {remote}")
    result = kubectl("cp", str(local), f"{NAMESPACE}/{PROBE_POD}:{remote}")
    if result.returncode != 0:
        return f"kubectl cp {local.name}: {result.stderr.strip()[:200]}"
    return None


def build_workloads(destination):
    """Build cpu_burn and wakeup_latency as static binaries.

    Static linking is what lets the pod run them without a compiler: the
    container image has no gcc, and installing one costs the node 600 MiB of
    a disk with about 1 GiB free. The two programs use only libc and pthreads,
    so a static build from the workstation runs unchanged on the node.
    """
    for name in WORKLOADS:
        source = MATRIX_SRC / f"{name}.c"
        if not source.is_file():
            return f"missing workload source {source}"
        result = subprocess.run(
            [
                "gcc",
                "-O2",
                "-static",
                "-pthread",
                "-o",
                str(destination / name),
                str(source),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"static build of {name} failed: {result.stderr.strip()[:300]}"
    return None


def arm_build(arms, binary):
    for arm in arms:
        if arm.get("binary") == binary:
            return arm["build"]
    return Path(binary).name


def stage_scx(binary, build):
    """Copy the scheduler into the pod when the pod holds a different build."""
    local = SCX_BUILD / build
    remote = exec_probe(f"stat -c '%Y %s' {binary} 2>/dev/null").stdout.strip()
    if local.is_file():
        stat = local.stat()
        expected = f"{int(stat.st_mtime)} {stat.st_size}"
        if remote == expected:
            return None
        error = copy_into_pod(local, binary)
        if error:
            return error
        exec_probe(f"chmod 0755 {binary}")
        return None
    if remote:
        # A build is already in the pod but this workstation does not have the
        # tree; that is fine, and saying so is better than failing.
        print(f"  using the scheduler already in the pod at {binary}")
        return None
    return (
        f"{local} is missing: build scx_lavd first or set PHASE7_SCX_BUILD "
        "to the directory that holds it"
    )


def stage(pod, arms):
    """Enter the node view and put the matrix into the pod."""
    has_python = exec_probe("command -v python3").returncode == 0
    if not has_python:
        # Only Python is installed: the image ships without it, and the
        # matrix cannot run under anything else. This is the one package the
        # node's free space can afford.
        print("  installing python3 in the pod")
        install = exec_probe(
            "dnf install -y -q python3 >/dev/null 2>&1 && command -v python3",
            timeout=900,
        )
        if install.returncode != 0:
            return f"python3 is missing and dnf could not install it: {install.stderr.strip()[:300]}"

    ready = exec_probe(f"mkdir -p {WORK_DIR}")
    if ready.returncode != 0:
        return f"cannot create {WORK_DIR}: {ready.stderr.strip()[:200]}"

    view = copy_into_pod(NODE_VIEW, f"{WORK_DIR}/node_view.sh")
    if view:
        return view

    for name in MATRIX_FILES:
        local = MATRIX_SRC / name
        if not local.is_file():
            return f"missing matrix source {local}"
        error = copy_into_pod(local, f"{WORK_DIR}/{name}")
        if error:
            return error

    for binary in sorted({arm["binary"] for arm in arms if arm["binary"]}):
        error = stage_scx(binary, arm_build(arms, binary))
        if error:
            return error

    with tempfile.TemporaryDirectory(prefix="phase7-7.6-") as tmp:
        built = build_workloads(Path(tmp))
        if built:
            return built
        for name in WORKLOADS:
            error = copy_into_pod(Path(tmp) / name, f"{WORK_DIR}/{name}")
            if error:
                return error
    # kubectl cp preserves the workstation mtimes. The binaries must look
    # newer than their sources or the matrix tries to recompile them.
    exec_probe(f"touch {WORK_DIR}/{' {WORK_DIR}/'.join(WORKLOADS)}")

    check = exec_probe(
        f"cd {WORK_DIR} && CGROUP_MOUNT={HOST_CGROUP} "
        f"sh node_view.sh python3 run_matrix.py "
        f"--check --out /tmp/check.jsonl --meta /tmp/check.json"
    )
    if check.returncode != 0:
        return (
            "run_matrix.py --check failed: "
            f"{check.stdout.strip()[:300]} {check.stderr.strip()[:300]}"
        )
    try:
        return json.loads(check.stdout[check.stdout.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return f"unparsable --check output: {check.stdout[:300]}"


def sched_ext_state():
    out = exec_probe(f"cat {HOST_SCHED_EXT}/state 2>/dev/null").stdout.strip()
    return out or "missing"


def wait_state(expected, timeout=20):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = sched_ext_state()
        if last == expected:
            return last
        time.sleep(1)
    return last


def scheduler_pid(binary):
    """PID of the running scheduler, found by the start of its cmdline.

    node_view.sh launches the scheduler through unshare(1), which normally
    keeps the PID of the shell that started it, but that is an implementation
    detail. Matching the exact binary path at the start of /proc/PID/cmdline
    cannot match the scanning shell, whose cmdline starts with "sh".
    """
    out = exec_probe(
        "for p in /proc/[0-9]*; do "
        "c=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null); "
        f'case "$c" in "{binary}"*) echo "${{p#/proc/}}"; break;; esac; '
        "done 2>/dev/null"
    )
    return out.stdout.strip()


CELL_ROOT = f"{HOST_CGROUP}/sched-modes-cells"


def cell_setup():
    """Two sibling cells with deliberately unequal weights.

    The matrix creates its groups after the scheduler starts, so a cell
    scheduler never sees them: it scans its parent once, at init. These two
    cells exist first. Under CFS the 400/100 weights would split the CPUs
    80/20; a cell scheduler is supposed to give each cell its own CPUs
    instead, so the same workloads should come out near 50/50.
    """
    exec_probe(f"rmdir {CELL_ROOT}/cell-a {CELL_ROOT}/cell-b 2>/dev/null; "
               f"mkdir -p {CELL_ROOT}/cell-a {CELL_ROOT}/cell-b")
    exec_probe(f"echo '+cpu' > {CELL_ROOT}/cgroup.subtree_control 2>/dev/null; true")
    for cell, weight in (("cell-a", 400), ("cell-b", 100)):
        exec_probe(f"echo {weight} > {CELL_ROOT}/{cell}/cpu.weight")


def cell_burners(duration):
    """One shell that starts both burners and waits for them.

    Each burner writes its own pid into its cell before exec, which keeps the
    pid through the exec, so the threads land in the right cgroup. Starting
    them from one foreground command avoids the container runtime waiting on
    a background child.
    """
    cmds = []
    for cell in ("cell-a", "cell-b"):
        cmds.append(
            f"sh -c 'echo $$ > {CELL_ROOT}/{cell}/cgroup.procs; "
            f"exec {WORK_DIR}/cpu_burn --threads 4 --duration {duration} "
            f"--wait-file {WORK_DIR}/scenarios.py --output /tmp/{cell}.json' &"
        )
    return " ".join(cmds) + " wait"


def cell_usage():
    out = {}
    for cell in ("cell-a", "cell-b"):
        text = exec_probe(f"cat {CELL_ROOT}/{cell}/cpu.stat 2>/dev/null").stdout
        stats = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                stats[parts[0]] = int(parts[1])
        out[cell] = stats
    return out


def cell_test(schedulers, duration=8):
    """CPU seconds per cell under CFS and under each requested cell scheduler."""
    results = []
    for arm in ["cfs"] + [key for key in schedulers if key in ("mitosis", "nitosis")]:
        spec = None if arm == "cfs" else SCHEDULERS[arm]
        info = None
        if spec:
            info, error = start_scx({"binary": f"/var/tmp/{spec['build']}",
                                     "args": spec["args"]})
            if not info:
                results.append({"arm": spec["name"], "status": "failed", "error": error})
                continue
        cell_setup()
        before = cell_usage()
        burn = exec_probe(cell_burners(duration), timeout=duration + 120)
        after = cell_usage()
        if info:
            stop_scx({"pid": info["pid"], "supervisor": info["supervisor"]})
        usage = {}
        for cell in ("cell-a", "cell-b"):
            b, a = before.get(cell, {}), after.get(cell, {})
            usage[cell] = round(
                ((a.get("usage_usec", 0) - b.get("usage_usec", 0))) / 1e6, 3
            )
        total = sum(usage.values()) or 1
        results.append(
            {
                "arm": spec["name"] if spec else "cfs",
                "status": "ok" if burn.returncode == 0 else "failed",
                "weights": {"cell-a": 400, "cell-b": 100},
                "cpu_seconds": usage,
                "share": {cell: round(value / total, 3) for cell, value in usage.items()},
                "error": None if burn.returncode == 0 else burn.stderr.strip()[:300],
            }
        )
    exec_probe(f"rmdir {CELL_ROOT}/cell-a {CELL_ROOT}/cell-b {CELL_ROOT} 2>/dev/null; true")
    return results


def preempt_read():
    return exec_probe(f"cat {PREEMPT_FILE} 2>/dev/null").stdout.strip()


def preempt_active(raw):
    """The active model from "(full) lazy" or "full (lazy)"."""
    match = re.search(r"\((\w+)\)", raw)
    return match.group(1) if match else ""


def preempt_set(mode):
    exec_probe(f"echo {mode} > {PREEMPT_FILE}", timeout=30)
    return preempt_read()


def start_scx(arm):
    """Attach the scheduler for the whole arm and wait until it enables.

    `kubectl exec` does not return while anything the command started is
    still alive: the container runtime waits on the orphan even after the
    command itself exits, so `&`, setsid, a double fork and a fork-and-exit
    launcher all keep the exec open until the scheduler stops. `nsenter -t 1`
    and `systemd-run` are unavailable (the first is blocked by the pod's
    capabilities, the second needs the node's D-Bus), so there is no way to
    hand the scheduler to the node's init from here.

    The remaining option is to keep the long-lived process in the foreground
    of an exec the harness itself holds open for the duration of the arm: the
    Popen never blocks the harness, the state polls and the stop signal run in
    separate short execs, and killing the scheduler makes this exec return
    normally. Nothing is leaked if the arm is stopped.
    """
    exec_probe("rm -f /var/tmp/scx.log /var/tmp/scx.pid")
    # A cell scheduler scans the children of its parent once, at start, so the
    # parent must exist before the scheduler does. Creating it does not create
    # a cell: the matrix's groups do that when they run.
    exec_probe(f"mkdir -p {CELL_PARENT_PATH}")
    # scx_stats hard-codes /var/run/scx/<sched>/stats with no override, and a
    # previous sched_ext experiment (running as another pod user) may own the
    # stats socket there, so removing the directory fails. /var/run is
    # pod-local tmpfs: renaming it aside needs no write permission on the
    # socket itself and touches nothing on the node.
    exec_probe(
        "mv /var/run/scx /var/run/scx.stale-$$ 2>/dev/null; mkdir -p /var/run/scx",
        timeout=60,
    )
    # `echo $$` runs in the same shell that then exec()s the scheduler, and
    # exec keeps the PID, so the pidfile names the scheduler itself.
    inner = (
        "echo $$ > /var/tmp/scx.pid; "
        f"exec sh {WORK_DIR}/node_view.sh {arm['binary']} {' '.join(arm['args'])}"
        " > /var/tmp/scx.log 2>&1"
    )
    supervisor = subprocess.Popen(
        ["kubectl", "exec", "-n", NAMESPACE, PROBE_POD, "--", "sh", "-c", inner],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = ""
    for _ in range(50):
        pid = scheduler_pid(arm["binary"])
        if pid:
            break
        time.sleep(0.2)

    state = wait_state("enabled", 30)
    if state != "enabled":
        tail = exec_probe("tail -5 /var/tmp/scx.log").stdout.strip()
        stop_scx({"pid": pid, "supervisor": supervisor})
        return None, f"state={state} pid={pid} log={tail[:400]}"
    seq = exec_probe(f"cat {HOST_SCHED_EXT}/enable_seq").stdout.strip()
    return {
        "pid": pid,
        "enable_seq": seq,
        "state": state,
        "supervisor": supervisor,
    }, None


def stop_scx(record):
    """Detach the scheduler and confirm the kernel took the CPUs back."""
    pid = record.get("pid") or scheduler_pid(record.get("binary") or "")
    if pid:
        # TERM first, so the scheduler unregisters its ops cleanly; KILL only
        # if it is still there. The pidfile names the scheduler, never the
        # shell that started it.
        exec_probe(
            f"kill -TERM {pid} 2>/dev/null; sleep 2; "
            f"kill -0 {pid} 2>/dev/null && kill -KILL {pid} 2>/dev/null; true",
            timeout=60,
        )
    state = wait_state("disabled", 20)
    supervisor = record.get("supervisor")
    if supervisor is not None:
        try:
            supervisor.wait(timeout=30)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait(timeout=10)
    rejected = exec_probe(f"cat {HOST_SCHED_EXT}/nr_rejected").stdout.strip()
    return {"state": state, "nr_rejected": rejected}


def run_matrix(label, reps, duration, scenarios, modes):
    """Run one arm and return (rows, meta, error)."""
    tag = f"{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    remote_out = f"/var/tmp/{tag}.jsonl"
    remote_meta = f"/var/tmp/{tag}.json"
    command = (
        f"cd {WORK_DIR} && CGROUP_MOUNT={HOST_CGROUP} "
        f"sh node_view.sh python3 run_matrix.py --reps {reps} --duration {duration} "
        f"--scenarios {','.join(scenarios)} --modes {','.join(modes)} "
        f"--run-id {tag} --out {remote_out} --meta {remote_meta}"
    )
    # The matrix sleeps for warmup + duration + cooldown per group set, four
    # times per rep, once per mode. Give the exec a generous ceiling; the
    # scheduler-side loop is the only slow part and it is counted.
    budget = int(reps * len(modes) * len(scenarios) * (duration + 4) * 2.5 + 180)
    result = exec_probe(command, timeout=budget)
    if result.returncode != 0:
        return (
            [],
            None,
            (
                f"matrix exited {result.returncode}: "
                f"{result.stdout.strip()[-400:]} {result.stderr.strip()[-400:]}"
            ),
        )

    rows = []
    for line in exec_probe(f"cat {remote_out}").stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    meta_text = exec_probe(f"cat {remote_meta}").stdout
    try:
        meta = json.loads(meta_text[meta_text.index("{") :])
    except (ValueError, json.JSONDecodeError):
        meta = {"unparsable": meta_text[:400]}
    return rows, meta, None


# The groups that compete for CPU in each scenario, i.e. the denominator of
# a share. The nested scenario counts at the QoS parent, because that is the
# level at which a QoS class competes; its pod children are reported too, but
# their usage is already part of the parent's total.
UNIT_NAMES = {
    "weighted_threads": ["w400-t1", "w200-t2", "w100-t4"],
    "qos_nested": ["qos-guaranteed", "qos-burstable"],
    "quota": ["limited", "open"],
    "latency": ["load", "probe"],
}


def aggregate(rows):
    """Per (arm, scenario, mode, group): mean CPU share and CPU seconds.

    A share is computed inside one repetition, so a repetition that ran
    slower as a whole does not look like a group losing its share.
    """
    per_rep_total = {}
    for row in rows:
        if row["group"] not in UNIT_NAMES.get(row["scenario"], []):
            continue
        key = (row["arm_taken"], row["scenario"], row["mode"], row["rep"])
        per_rep_total[key] = per_rep_total.get(key, 0) + row["usage_usec"]

    buckets = {}
    for row in rows:
        key = (row["arm_taken"], row["scenario"], row["mode"], row["group"])
        buckets.setdefault(key, []).append(row)

    summary = []
    for (arm, scenario, mode, group), group_rows in buckets.items():
        first = group_rows[0]
        shares = []
        total = 0
        for row in group_rows:
            total += row["usage_usec"]
            if row["group"] not in UNIT_NAMES.get(scenario, []):
                continue
            denominator = per_rep_total[(arm, scenario, mode, row["rep"])]
            if denominator:
                shares.append(row["usage_usec"] / denominator)
        summary.append(
            {
                "arm": arm,
                "scenario": scenario,
                "mode": mode,
                "group": group,
                "kind": first["kind"],
                "weight": first["weight"],
                "threads": first["threads"],
                "cpu_max": first["cpu_max"],
                "reps": len(group_rows),
                "share_mean": round(sum(shares) / len(shares), 4) if shares else None,
                "share_min": round(min(shares), 4) if shares else None,
                "share_max": round(max(shares), 4) if shares else None,
                "cpu_seconds_mean": round(total / len(group_rows) / 1e6, 4),
                "throttled_total": sum(row["nr_throttled"] for row in group_rows),
            }
        )
    summary.sort(
        key=lambda item: (
            item["scenario"],
            item["mode"],
            item["arm"],
            item["kind"],
            item["group"],
        )
    )
    return summary


def latency_row(rows, arm, scenario, mode):
    """Mean wakeup latency of the probe group in the latency scenario."""
    probes = [
        row
        for row in rows
        if row["arm_taken"] == arm
        and row["scenario"] == scenario
        and row["mode"] == mode
        and row["group"] == "probe"
    ]
    if not probes:
        return None
    fields = ("p50_us", "p99_us", "max_us")
    values = {}
    for field in fields:
        present = [
            row[field] for row in probes if isinstance(row.get(field), (int, float))
        ]
        values[field] = round(sum(present) / len(present), 1) if present else None
    values["reps"] = len(probes)
    return values


def preempt_comparison(args, scenarios):
    """Measure the same latency workload under a lazy and a full preemption.

    PREEMPT_LAZY lets a wakeup wait for a reschedule point instead of
    preempting immediately, which trades wakeup latency for fewer
    context switches. PREEMPT_DYNAMIC makes the choice a static key, so both
    models can be measured in one boot: the boot parameter only picks the
    default. PREEMPT_RT cannot be combined with sched_ext at all and is out
    of scope here.
    """
    if "latency" not in scenarios:
        return {
            "status": "skipped",
            "reason": "the latency scenario is not in --scenarios",
        }

    original = preempt_read()
    if not original:
        return {"status": "skipped", "reason": f"{PREEMPT_FILE} is not present"}

    results = []
    for mode in ("lazy", "full"):
        raw = preempt_set(mode)
        active = preempt_active(raw)
        print()
        print(f"{'-' * 78}")
        print(f"preempt arm: requested={mode} active={active} ({raw})")
        print(f"{'-' * 78}")
        rows, meta, error = run_matrix(
            f"preempt-{mode}", args.reps, args.duration, ["latency"], ["concur"]
        )
        probe = None
        if error:
            rows = []
        else:
            for row in rows:
                row["arm_taken"] = f"preempt-{mode}"
                row["sched_ext"] = "cfs"
            probe = latency_row(rows, f"preempt-{mode}", "latency", "concur")
        if probe:
            print(
                f"  probe p50 {probe['p50_us']} us p99 {probe['p99_us']} us "
                f"max {probe['max_us']} us over {probe['reps']} reps"
            )
        results.append(
            {
                "requested": mode,
                "active": active,
                "raw": raw,
                "meta": meta,
                "error": error,
                "probe": probe,
                "rows": rows,
            }
        )

    restored = preempt_set(preempt_active(original) or "lazy")
    rows = []
    for entry in results:
        rows.extend(entry.pop("rows", []))
    return {
        "status": "ok"
        if all(entry["error"] is None for entry in results)
        else "partial",
        "file": PREEMPT_FILE,
        "original": original,
        "restored": restored,
        "arms": results,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--skip-scx", action="store_true")
    parser.add_argument("--skip-preempt", action="store_true")
    parser.add_argument(
        "--schedulers",
        default="lavd",
        help="comma-separated sched_ext schedulers to run: " + ", ".join(SCHEDULERS),
    )
    parser.add_argument(
        "--only-preempt", action="store_true", help="run only the preemption comparison"
    )
    parser.add_argument(
        "--cells",
        action="store_true",
        help="also run the two-cell isolation test for the cell schedulers",
    )
    parser.add_argument("--label", default="kernel-comparison")
    args = parser.parse_args()

    scenarios = [name for name in args.scenarios.split(",") if name]
    modes = [name for name in args.modes.split(",") if name]
    for name in scenarios:
        if name not in SCENARIOS:
            raise SystemExit(f"unknown scenario: {name}")
    for mode in modes:
        if mode not in MODES:
            raise SystemExit(f"unknown mode: {mode}")

    schedulers = [name for name in args.schedulers.split(",") if name]
    for name in schedulers:
        if name not in SCHEDULERS:
            raise SystemExit(
                f"unknown scheduler: {name} (known: {', '.join(SCHEDULERS)})"
            )

    print("=" * 78)
    print("Phase 7 Topic 7.6: kernel scheduler comparison")
    print("=" * 78)

    node = os.environ.get("PHASE7_NODE") or PROBE_NODE
    kubectl("create", "namespace", NAMESPACE)

    pod, error = ensure_probe(node)
    if not pod:
        print(f"FAILED: {error}")
        return 1
    print(f"pod: {NAMESPACE}/{pod} on {node}")

    arms = [] if args.only_preempt else [{"name": "cfs", "binary": None, "args": []}]
    if not args.skip_scx and not args.only_preempt:
        for key in schedulers:
            spec = SCHEDULERS[key]
            arms.append(
                {
                    "name": spec["name"],
                    "binary": f"/var/tmp/{spec['build']}",
                    "build": spec["build"],
                    "args": list(spec["args"]),
                    "cpu_max": spec["cpu_max"],
                }
            )

    environment = stage(pod, arms)
    if isinstance(environment, str):
        print(f"FAILED: {environment}")
        return 1
    print(
        f"node view: kernel={environment['kernel']} cpus={environment['cpus']} "
        f"cgroup_mode={environment['mode']}"
    )
    if environment["kernel"] != "7.3.0-k8labs":
        # The CFS modes only exist on the custom kernel, and sched_ext needs
        # its config; recording the mismatch is better than failing obscurely.
        print(f"WARNING: expected 7.3.0-k8labs, got {environment['kernel']}")


    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    all_rows = []
    arm_records = []
    failures = []

    for arm in arms:
        print()
        print(f"{'-' * 78}")
        print(f"arm: {arm['name']}")
        print(f"{'-' * 78}")
        record = {"arm": arm["name"], "binary": arm["binary"], "args": arm["args"]}
        supervisor = None

        started_ok = True
        if arm["binary"]:
            started_ok = exec_probe(f"test -x {arm['binary']}").returncode == 0
            if not started_ok:
                failures.append(f"{arm['name']}: {arm['binary']} is not staged")
                record["status"] = "failed"
                record["error"] = f"{arm['binary']} is not staged in the pod"
                arm_records.append(record)
                continue
            info, error = start_scx(arm)
            if not info:
                failures.append(f"{arm['name']}: {error}")
                record["status"] = "failed"
                record["error"] = error
                arm_records.append(record)
                continue
            supervisor = info["supervisor"]
            record.update(
                {
                    "pid": info["pid"],
                    "state": info["state"],
                    "enable_seq": info["enable_seq"],
                }
            )
            print(f"  attached: state={info['state']} enable_seq={info['enable_seq']}")

        try:
            rows, meta, error = run_matrix(
                arm["name"], args.reps, args.duration, scenarios, modes
            )
        finally:
            detach = {"state": sched_ext_state()}
            if supervisor is not None:
                detach = stop_scx({**record, "supervisor": supervisor})
                print(
                    f"  detached: state={detach['state']} "
                    f"nr_rejected={detach['nr_rejected']}"
                )
            if detach["state"] != "disabled":
                failures.append(f"{arm['name']}: sched_ext still {detach['state']}")
                record["status"] = "failed"
                record["error"] = f"scheduler did not detach: {detach}"
        record["detach"] = detach

        if error:
            failures.append(f"{arm['name']}: {error}")
            record["status"] = "failed"
            record["error"] = error
            arm_records.append(record)
            continue

        for row in rows:
            row["arm_taken"] = arm["name"]
            row["sched_ext"] = arm["binary"] or "cfs"
        all_rows.extend(rows)
        print(f"  rows: {len(rows)}")

        raw_path = EVIDENCE_DIR / f"{args.label}-{arm['name']}-rows-{started}.jsonl"
        raw_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
        )
        record.update(
            {
                "status": "ok",
                "rows": len(rows),
                "raw": raw_path.name,
                "matrix_meta": meta,
            }
        )
        arm_records.append(record)

    cells = cell_test(schedulers, args.duration) if args.cells else None
    if cells:
        print()
        print("cells (weights 400 vs 100, four threads each):")
        for entry in cells:
            print(
                f"  {entry['arm']:20} {entry.get('share')} "
                f"cpu_s {entry.get('cpu_seconds')}"
            )

    preempt = None
    if not args.skip_preempt:
        preempt = preempt_comparison(args, scenarios)
        if preempt.get("rows"):
            all_rows.extend(preempt.pop("rows"))

    summary = aggregate(all_rows) if all_rows else []

    # Write the summary next to the raw rows so both can be cross-checked.
    summary_path = EVIDENCE_DIR / f"{args.label}-{started}.json"
    payload = {
        "test": "kernel-comparison",
        "label": args.label,
        "topic": "7.6",
        "timestamp": started,
        "node": node,
        "pod": pod,
        "environment": environment,
        "duration_sec": args.duration,
        "reps": args.reps,
        "scenarios": scenarios,
        "modes": modes,
        "arms": arm_records,
        "preempt": preempt,
        "cells": cells,
        "summary": summary,
        "latency": [
            {
                "arm": arm,
                "scenario": "latency",
                "mode": mode,
                "probe": latency_row(all_rows, arm, "latency", mode),
            }
            for arm in [record["arm"] for record in arm_records]
            for mode in modes
        ]
        if "latency" in scenarios
        else [],
        "failures": failures,
    }
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    print()
    print(
        f"{'scenario':18} {'mode':7} {'arm':24} {'group':18} {'share':>7} {'cpu_s':>8}"
    )
    for item in summary:
        share = item["share_mean"]
        print(
            f"{item['scenario']:18} {item['mode']:7} {item['arm']:24} "
            f"{item['group']:18} {share if share is not None else '-':>7} "
            f"{item['cpu_seconds_mean']:>8}"
        )

    lat = [entry for entry in payload["latency"] if entry["probe"]]
    if lat:
        print()
        print("wakeup latency (probe group):")
        for entry in lat:
            probe = entry["probe"]
            print(
                f"  {entry['mode']:7} {entry['arm']:24} p50 {probe['p50_us']:>9} us "
                f"p99 {probe['p99_us']:>9} us max {probe['max_us']:>9} us"
            )

    print()
    print(f"Evidence: {summary_path}")
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        return 1
    print("Result: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
