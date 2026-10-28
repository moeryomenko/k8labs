#!/usr/bin/env python3
"""Pilot a CPU-limit bracket on the live cluster before committing campaign budget.

The pilot exists to answer one question cheaply: between which two CPU limits
does this workload cross its SLO? It is not a fit, and it deliberately makes no
model. Its output is a bracket plus the evidence behind it, which the campaign
then spends its budget inside.

Each cell:

1. Renders the workload template at one CPU limit and applies it.
2. Waits until the pod is ready **and** the service reports the intended mix, so
   a cell is never measured against a stale pod.
3. Runs the load generator in open-loop mode. Open loop is required: the pilot
   asks how the service behaves at a declared offered load, and a closed loop
   would let the service dictate its own request rate.
4. Samples the cell's cgroup counters for the measurement window and computes
   throttled time as a derivative, which is what the telemetry contract calls
   `resource.cpu.throttling`.
5. Judges the result against the SLO with `slo.verdict`.

Limit progression is decided by `bracket.plan_limits`. The pilot stops at the
first limit that meets the SLO, at the largest node the cell can run on, or when
the planned cells are exhausted.

Usage:

    uv run python campaign/pilot.py \
        --kubeconfig ../../build/kubeconfig \
        --mix balanced --rate 50 \
        --slo-p99-ms 100 --slo-min-rps 45 \
        --out results/pilot-balanced-50rps.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


from bracket import (
    Bracket,
    Outcome,
    PilotError,
    Trial,
    evaluate_trials,
    plan_limits,
)
from k8s import (
    ApiClient,
    CellSpec,
    KubernetesError,
    NAMESPACE,
    PortForward,
    apply_document,
    cell_pod,
    delete_cell,
    documents,
    is_request_only_node,
    node_capacity_millicores,
    parse_cpu_quantity,
    pod_node,
    pod_ready,
)
from slo import Observation, Slo, verdict

HERE = Path(__file__).resolve().parent
PHASE8 = HERE.parent

# Cgroup accounting is in microseconds of CPU time; a wall-clock ratio needs
# the same unit for both numerator and denominator.
USEC_PER_SECOND = 1_000_000


@dataclass
class CgroupSample:
    """One reading of the container's cgroup CPU accounting."""

    at_seconds: float
    nr_periods: int
    nr_throttled: int
    throttled_usec: int
    usage_usec: int


@dataclass
class CellResult:
    cell: str
    limit_millicores: int
    request_millicores: int
    node: str
    node_capacity_millicores: int
    outcome: str
    reason: str
    loadgen: dict[str, Any] = field(default_factory=dict)
    cgroup: dict[str, Any] = field(default_factory=dict)
    wall_seconds: float = 0.0


def log(message: str) -> None:
    print(f"[pilot] {message}", file=sys.stderr, flush=True)


def kubectl(kubeconfig: Path) -> Path:
    binary = subprocess.run(
        ["bash", "-lc", "command -v kubectl"], capture_output=True, text=True
    ).stdout.strip()
    if not binary:
        raise SystemExit("kubectl was not found on PATH")
    return Path(binary)


def wait_for_endpoints(
    client: ApiClient, service: str, *, timeout_seconds: float
) -> None:
    """Wait until a Service has at least one ready endpoint.

    `kubectl port-forward svc/...` resolves endpoints once, at start-up, and
    fails immediately when the set is empty. A pod can be Ready slightly before
    the EndpointSlice controller has published its address, so the forward is
    only attempted once an endpoint exists.
    """
    deadline = time.monotonic() + timeout_seconds
    path = f"/api/v1/namespaces/{NAMESPACE}/endpoints/{service}"
    while time.monotonic() < deadline:
        endpoints = client.expect("GET", path)
        for subset in endpoints.get("subsets") or []:
            if subset.get("addresses"):
                return
        time.sleep(0.5)
    raise KubernetesError(
        f"service {service} has no ready endpoints after {timeout_seconds:.0f}s"
    )


def wait_for_cell(
    client: ApiClient, cell: str, *, timeout_seconds: float
) -> dict[str, Any]:
    """Wait until the cell's single pod is Running and all containers report ready."""
    deadline = time.monotonic() + timeout_seconds
    last_state = "not created"
    while time.monotonic() < deadline:
        pod = cell_pod(client, cell)
        if pod is None:
            last_state = "not created"
        elif pod_ready(pod):
            return pod
        else:
            last_state = describe_pod_state(pod)
        time.sleep(1.0)
    raise KubernetesError(
        f"cell {cell} not ready after {timeout_seconds:.0f}s: {last_state}"
    )


def describe_pod_state(pod: dict[str, Any] | None) -> str:
    if pod is None:
        return "pod absent"
    status = pod.get("status", {})
    phase = status.get("phase", "unknown")
    statuses = status.get("containerStatuses") or []
    waits = [
        f"{s.get('name')}={(s.get('state', {}).get('waiting', {}) or {}).get('reason', 'not-ready')}"
        for s in statuses
    ]
    return f"phase={phase} containers={','.join(waits) or 'none'}"


def warm_up(url: str, *, samples: int = 20) -> None:
    """Prime the connection pools and fail early if the handler is broken.

    Without this the first measured samples include connection establishment,
    which is an artefact of when the load generator started rather than of the
    CPU limit.
    """
    for _ in range(samples):
        with urllib.request.urlopen(f"{url}/work", timeout=10) as response:
            if response.status != 200:
                raise KubernetesError(
                    f"warm-up request returned {response.status}; the cell is not serving"
                )


def fetch_meta(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/meta", timeout=10) as response:
        return json.loads(response.read())


def run_loadgen(
    binary: Path,
    *,
    url: str,
    mix: str,
    rate: float,
    duration_seconds: int,
    warmup_seconds: int,
    timeout_ms: int,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the load generator and return its JSON report.

    Exit code 3 means the generator declared its own run invalid. That is a
    recorded result, not a crash: the report is still returned so the cell is
    excluded with its reason preserved.
    """
    process_timeout = duration_seconds + warmup_seconds + 120
    if timeout_seconds is not None:
        process_timeout = min(process_timeout, max(0.1, timeout_seconds))
    completed = subprocess.run(
        [
            str(binary),
            "--url",
            url,
            "--mode",
            "open",
            "--rate",
            f"{rate:g}",
            "--duration-secs",
            str(duration_seconds),
            "--warmup-secs",
            str(warmup_seconds),
            "--mix",
            mix,
            "--timeout-ms",
            str(timeout_ms),
        ],
        capture_output=True,
        text=True,
        timeout=process_timeout,
    )
    if completed.returncode not in (0, 3):
        raise KubernetesError(
            "load generator failed: "
            f"exit {completed.returncode}: {completed.stderr.strip()[:2000]}"
        )
    if not completed.stdout.strip():
        raise KubernetesError(
            f"load generator produced no report: {completed.stderr.strip()[:2000]}"
        )
    return json.loads(completed.stdout)


def exec_in_container(
    kube_bin: Path,
    kubeconfig: Path,
    pod_name: str,
    container: str,
    command: list[str],
) -> str:
    completed = subprocess.run(
        [
            str(kube_bin),
            f"--kubeconfig={kubeconfig}",
            "-n",
            NAMESPACE,
            "exec",
            pod_name,
            "-c",
            container,
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise KubernetesError(
            f"exec {command} in {pod_name}/{container} failed: {completed.stderr.strip()[:500]}"
        )
    return completed.stdout


def parse_cpu_stat(raw: str) -> dict[str, int]:
    """Parse `/sys/fs/cgroup/cpu.stat` into `flat key -> value`.

    The kernel adds keys over time (`nice_usec`, `core_sched.force_idle_usec`),
    so unknown keys are kept rather than rejected, and required keys are checked
    by the caller.
    """
    values: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        key, value = parts
        try:
            values[key] = int(value)
        except ValueError:
            continue
    return values


def sample_cgroup(kube_bin: Path, kubeconfig: Path, pod_name: str) -> CgroupSample:
    raw = exec_in_container(
        kube_bin, kubeconfig, pod_name, "app", ["cat", "/sys/fs/cgroup/cpu.stat"]
    )
    values = parse_cpu_stat(raw)
    for required in ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec"):
        if required not in values:
            raise KubernetesError(
                f"cpu.stat is missing {required}: {raw.strip()[:200]}"
            )
    return CgroupSample(
        at_seconds=time.monotonic(),
        nr_periods=values["nr_periods"],
        nr_throttled=values["nr_throttled"],
        throttled_usec=values["throttled_usec"],
        usage_usec=values["usage_usec"],
    )


def derive_cpu_metrics(
    before: CgroupSample, after: CgroupSample, limit_millicores: int
) -> dict[str, float]:
    """Convert cumulative cgroup counters into the contract's rate gauges.

    `resource.cpu.throttling` is defined as throttled time per wall-clock time,
    and `resource.cpu.usage` as a millicore rate. Both are derivatives, so both
    need two samples; a single reading cannot produce either and is rejected.
    """
    elapsed = after.at_seconds - before.at_seconds
    if elapsed <= 0:
        raise KubernetesError("cgroup samples are not ordered in time")

    throttled_delta = after.throttled_usec - before.throttled_usec
    usage_delta = after.usage_usec - before.usage_usec
    periods_delta = after.nr_periods - before.nr_periods

    if throttled_delta < 0 or usage_delta < 0 or periods_delta < 0:
        raise KubernetesError("cgroup counters decreased; the container was replaced")

    return {
        "throttle_ratio": min(1.0, throttled_delta / (elapsed * USEC_PER_SECOND)),
        "throttled_ms_per_second": throttled_delta / elapsed / 1000.0,
        "cpu_usage_millicores": usage_delta / elapsed / 1000.0,
        "cpu_limit_millicores": float(limit_millicores),
        "periods": float(periods_delta),
        "periods_throttled": float(after.nr_throttled - before.nr_throttled),
        "elapsed_seconds": elapsed,
    }


def observation_from(
    loadgen: dict[str, Any],
    cpu: dict[str, float],
    *,
    target_reached: bool | None = None,
) -> Observation:
    latency = loadgen.get("latency_ms") or {}
    completed = int(loadgen.get("completed", 0))
    errors = int(loadgen.get("errors", 0))

    # Availability is the share of issued requests that succeeded. The report's
    # error ratio is over completed requests, so it cannot stand in for it.
    availability: float | None
    if completed > 0:
        availability = 1.0 - (errors / completed)

    return Observation(
        p50_latency_ms=float(latency["p50"]) if "p50" in latency else None,
        p99_latency_ms=float(latency["p99"]) if "p99" in latency else None,
        throughput_rps=float(loadgen["achieved_rate_rps"])
        if "achieved_rate_rps" in loadgen
        else None,
        error_ratio=float(loadgen["error_ratio"]) if "error_ratio" in loadgen else None,
        availability_ratio=availability,
        cpu_usage_millicores=cpu.get("cpu_usage_millicores"),
        throttled_ms_per_second=cpu.get("throttled_ms_per_second"),
        completed_requests=completed,
        loadgen_valid=bool(loadgen.get("valid", False)),
        loadgen_invalid_reason=str(loadgen.get("invalid_reason") or ""),
    )


def measure_cell(
    *,
    kube_bin: Path,
    kubeconfig: Path,
    loadgen: Path,
    cell: CellSpec,
    rate_rps: float,
    slo: Slo,
    duration_seconds: int,
    warmup_seconds: int,
    local_port: int,
    timeout_ms: int,
    workdir: Path,
    deadline: float | None = None,
) -> CellResult:
    """Deploy, warm, measure, and judge one cell.

    The cell's objects are removed in a `finally` block on every path, including
    measurement failures. A leaked cell keeps consuming a node and, because the
    template requires anti-affinity between cells, would block the next cell
    from scheduling.
    """
    started = time.monotonic()
    manifest_path = workdir / f"{cell.cell}.yaml"
    manifest_path.write_text(cell.manifest())

    client = ApiClient(kubeconfig)
    try:
        for document in documents(manifest_path.read_text()):
            apply_document(client, document)

        readiness_timeout = 240.0
        if deadline is not None:
            readiness_timeout = min(
                readiness_timeout, max(0.1, deadline - time.monotonic())
            )
        pod = wait_for_cell(client, cell.cell, timeout_seconds=readiness_timeout)
        node = pod_node(pod)
        node_capacity = node_capacity_millicores(client, node)
        # The renderer names the Service after the cell with a `bench-` prefix.
        endpoint_timeout = 60.0
        if deadline is not None:
            endpoint_timeout = min(
                endpoint_timeout, max(0.1, deadline - time.monotonic())
            )
        wait_for_endpoints(
            client, f"bench-{cell.cell}", timeout_seconds=endpoint_timeout
        )

        with PortForward(
            kube_bin,
            kubeconfig,
            target=f"svc/bench-{cell.cell}",
            remote_port=8080,
            local_port=local_port,
            ready_path="/healthz",
        ):
            url = f"http://127.0.0.1:{local_port}"
            run_ready_deadline = time.monotonic() + 60
            if deadline is not None:
                run_ready_deadline = min(run_ready_deadline, deadline)
            while True:
                try:
                    meta = fetch_meta(url)
                    break
                except OSError:
                    if time.monotonic() > run_ready_deadline:
                        raise KubernetesError("service /meta did not answer") from None
                    time.sleep(0.5)

            if meta.get("mix") != cell.mix:
                raise KubernetesError(
                    f"service reports mix {meta.get('mix')!r} but the cell declares {cell.mix!r}"
                )

            warm_up(url, samples=30)

            pod_name = pod["metadata"]["name"]
            before = sample_cgroup(kube_bin, kubeconfig, pod_name)
            loadgen_report = run_loadgen(
                loadgen,
                url=url,
                mix=cell.mix,
                rate=rate_rps,
                duration_seconds=duration_seconds,
                warmup_seconds=warmup_seconds,
                timeout_ms=timeout_ms,
                timeout_seconds=(
                    max(0.1, deadline - time.monotonic())
                    if deadline is not None
                    else None
                ),
            )
            after = sample_cgroup(kube_bin, kubeconfig, pod_name)
    finally:
        delete_cell(kube_bin, kubeconfig, manifest_path)

    cpu = derive_cpu_metrics(before, after, cell.cpu_limit_millicores)
    # Warm-up only returns once the service answered, so the target was reachable
    # when the measurement window opened.
    observation = observation_from(loadgen_report, cpu, target_reached=True)
    outcome, reason = verdict(observation, slo)

    return CellResult(
        cell=cell.cell,
        limit_millicores=cell.cpu_limit_millicores,
        request_millicores=cell.cpu_request_millicores,
        node=node,
        node_capacity_millicores=node_capacity,
        outcome=outcome.value,
        reason=reason,
        loadgen=loadgen_report,
        cgroup=cpu,
        wall_seconds=time.monotonic() - started,
    )


def cluster_capacity(client: ApiClient) -> list[tuple[str, int]]:
    """Schedulable worker nodes and their allocatable CPU, largest first."""
    listing = client.expect("GET", "/api/v1/nodes")
    workers: list[tuple[str, int]] = []
    for node in listing["items"]:
        name = node["metadata"]["name"]
        taints = node.get("spec", {}).get("taints", []) or []
        if is_request_only_node(taints):
            log(f"skipping control-plane node {name}")
            continue
        workers.append((name, parse_cpu_quantity(node["status"]["allocatable"]["cpu"])))
    workers.sort(key=lambda item: item[1], reverse=True)
    return workers


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument(
        "--loadgen",
        type=Path,
        default=PHASE8 / "loadgen" / "target" / "debug" / "bench-loadgen",
    )
    parser.add_argument("--mix", default="balanced")
    parser.add_argument(
        "--rate", type=float, default=50.0, help="Offered load in requests/second"
    )
    parser.add_argument("--duration-seconds", type=int, default=10)
    parser.add_argument("--warmup-seconds", type=int, default=5)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--slo-p99-ms", type=float, required=True)
    parser.add_argument(
        "--slo-min-rps",
        type=float,
        default=None,
        help="Defaults to 95%% of --rate, a 5%% throughput tolerance",
    )
    parser.add_argument("--slo-max-error-ratio", type=float, default=0.0)
    parser.add_argument("--slo-min-availability", type=float, default=0.99)
    parser.add_argument("--min-limit-millicores", type=int, default=50)
    parser.add_argument(
        "--max-limit-millicores",
        type=int,
        default=250,
        help="Maximum application CPU limit for the bounded pilot",
    )
    parser.add_argument("--growth", type=float, default=2.0)
    parser.add_argument("--max-cells", type=int, default=2)
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=60.0,
        help="End-to-end wall-clock budget for the pilot (maximum 60 seconds)",
    )
    parser.add_argument(
        "--cpu-request-millicores",
        type=int,
        default=None,
        help="Defaults to the smallest planned limit",
    )
    parser.add_argument("--cpu-iterations", type=int, default=20_000)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/cpu-limit-pilot"))
    args = parser.parse_args(argv)

    if not 0 < args.deadline_seconds <= 60:
        raise SystemExit("--deadline-seconds must be greater than 0 and at most 60")
    if args.max_cells < 1 or args.max_cells > 2:
        raise SystemExit("--max-cells must be between 1 and 2 for the bounded pilot")
    if not 0 < args.max_limit_millicores <= 250:
        raise SystemExit("--max-limit-millicores must be between 1m and 250m")
    deadline = time.monotonic() + args.deadline_seconds

    kube_bin = kubectl(args.kubeconfig)
    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)

    min_rps = args.slo_min_rps if args.slo_min_rps is not None else args.rate * 0.95
    slo = Slo(
        max_p99_latency_ms=args.slo_p99_ms,
        min_throughput_rps=min_rps,
        max_error_ratio=args.slo_max_error_ratio,
        min_availability_ratio=args.slo_min_availability,
    )

    workers = cluster_capacity(ApiClient(args.kubeconfig))
    if not workers:
        raise SystemExit("no schedulable worker nodes found")

    log("worker nodes (allocatable CPU): " + ", ".join(f"{n}={c}m" for n, c in workers))
    node_capacity = workers[0][1]
    limits = plan_limits(
        min_limit_millicores=args.min_limit_millicores,
        max_limit_millicores=min(node_capacity, args.max_limit_millicores),
        growth=args.growth,
        max_cells=args.max_cells,
    )
    log(f"planned limits: {', '.join(f'{limit}m' for limit in limits)}")

    request_millicores = args.cpu_request_millicores or limits[0]
    if request_millicores > 300:
        raise SystemExit(
            "--cpu-request-millicores exceeds the 300m active pilot budget"
        )
    if request_millicores > limits[0]:
        raise SystemExit(
            f"--cpu-request-millicores ({request_millicores}m) exceeds the smallest "
            f"planned limit ({limits[0]}m); the API server would reject every cell"
        )

    # The renderer caps a cell name at 41 characters, so the run id is short on
    # purpose: a timestamp to the day plus random bits, not to the second.
    run_id = f"p{dt.datetime.now(dt.timezone.utc):%m%d%H%M%S}{secrets.token_hex(2)}"
    results: list[CellResult] = []
    bracket: Bracket | None = None
    pilot_error = ""

    for index, limit in enumerate(limits):
        if time.monotonic() >= deadline:
            pilot_error = "pilot end-to-end deadline reached before starting next cell"
            break
        cell = CellSpec(
            cell=f"{run_id}-{limit}m",
            mix=args.mix,
            cpu_limit_millicores=limit,
            cpu_request_millicores=min(request_millicores, limit),
            cpu_iterations=args.cpu_iterations,
        )
        log(
            f"cell {index + 1}/{len(limits)}: limit={limit}m request={cell.cpu_request_millicores}m"
        )

        try:
            result = measure_cell(
                kube_bin=kube_bin,
                kubeconfig=args.kubeconfig,
                loadgen=args.loadgen,
                cell=cell,
                rate_rps=args.rate,
                slo=slo,
                duration_seconds=args.duration_seconds,
                warmup_seconds=args.warmup_seconds,
                local_port=18080,
                timeout_ms=args.timeout_ms,
                workdir=workdir,
                deadline=deadline,
            )
        except (KubernetesError, OSError, RuntimeError) as error:
            log(f"cell {limit}m failed to measure: {error}")
            results.append(
                CellResult(
                    cell=cell.cell,
                    limit_millicores=limit,
                    request_millicores=cell.cpu_request_millicores,
                    node="",
                    node_capacity_millicores=node_capacity,
                    outcome=Outcome.INVALID.value,
                    reason=f"measurement failed: {error}",
                )
            )
            continue

        results.append(result)
        log(
            f"  -> {result.outcome}: p99={result.loadgen.get('latency_ms', {}).get('p99')}ms "
            f"rps={result.loadgen.get('achieved_rate_rps', 0):.1f} "
            f"throttled={result.cgroup.get('throttled_ms_per_second', 0):.0f}ms/s [{result.reason}]"
        )

        if result.outcome == Outcome.MEETS_SLO.value:
            trials = [
                Trial(
                    limit_millicores=item.limit_millicores,
                    outcome=Outcome(item.outcome),
                    reason=item.reason,
                )
                for item in results
            ]
            try:
                bracket = evaluate_trials(trials, node_capacity)
            except PilotError as error:
                pilot_error = str(error)
                log(f"bracket unavailable: {error}")
            break

    if bracket is None and not pilot_error:
        trials = [
            Trial(
                limit_millicores=item.limit_millicores,
                outcome=Outcome(item.outcome),
                reason=item.reason,
            )
            for item in results
        ]
        try:
            bracket = evaluate_trials(trials, node_capacity)
        except PilotError as error:
            pilot_error = str(error)

    report: dict[str, Any] = {
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mixed": {
            "slo": asdict(slo),
            "rate_rps": args.rate,
            "duration_seconds": args.duration_seconds,
            "warmup_seconds": args.warmup_seconds,
            "cpu_iterations": args.cpu_iterations,
            "planned_limits_millicores": list(limits),
            "deadline_seconds": args.deadline_seconds,
            "max_active_request_millicores": request_millicores,
            "node_capacity_millicores": node_capacity,
        },
        "workers": [
            {"name": name, "allocatable_millicores": capacity}
            for name, capacity in workers
        ],
        "cells": [asdict(result) for result in results],
        "bracket": None,
        "error": pilot_error or None,
    }

    if bracket is not None:
        report["bracket"] = {
            "passing_millicores": bracket.passing_millicores,
            "failing_below_millicores": bracket.failing_below_millicores,
            "stop": bracket.stop.value,
            "node_capacity_millicores": bracket.node_capacity_millicores,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    log(f"wrote {args.out}")

    if bracket is None:
        log(f"pilot produced no bracket: {pilot_error}")
        return 4
    failing = (
        f"{bracket.failing_below_millicores}m"
        if bracket.failing_below_millicores is not None
        else "not measured"
    )
    log(
        f"bracket: passing {bracket.passing_millicores}m, "
        f"failing below {failing}, stop={bracket.stop.value}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
