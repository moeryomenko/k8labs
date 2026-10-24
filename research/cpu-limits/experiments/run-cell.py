#!/usr/bin/env python3
"""Run one CPU-limits cell and record only observed values.

This command owns one target Pod and, for a contention cell, one noisy-neighbour
Pod. It removes every owned Pod before it returns. A required missing value marks
the cell as failed. It never fills a missing value with zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

SERIES_ROOT = Path(__file__).resolve().parents[1]
if str(SERIES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERIES_ROOT))

from experiments import cgroup_probe, design
from workloads.render import render

PROTOCOL = "cpu-limits-v1"
BLOCK_COLUMNS = (
    "protocol",
    "phase",
    "run_id",
    "block_id",
    "seed",
    "position",
    "cell_id",
    "workload",
    "policy",
    "condition",
    "node",
    "pod_uid",
    "status",
    "started_at",
    "finished_at",
)
CGROUP_COLUMNS = (
    "protocol",
    "run_id",
    "block_id",
    "cell_id",
    "captured_at",
    "window",
    "level",
    "path",
    "key",
    "value",
    "unit",
)
METRIC_COLUMNS = (
    "protocol",
    "run_id",
    "block_id",
    "cell_id",
    "timestamp",
    "source",
    "metric",
    "value",
    "unit",
    "labels_json",
)
REQUIRED_METRICS = frozenset(
    {
        "container_cpu_usage_seconds_total",
        "container_cpu_cfs_periods_total",
        "container_cpu_cfs_throttled_periods_total",
        "container_cpu_cfs_throttled_seconds_total",
        "container_memory_working_set_bytes",
        "container_pressure_cpu_waiting_seconds_total",
    }
)


class CellRunError(RuntimeError):
    """The cell cannot provide a required measurement."""


@dataclass(frozen=True)
class CellResult:
    """The terminal state that is stored for one scheduled cell."""

    status: str
    pod_uid: str
    started_at: str
    finished_at: str
    error: str | None
    teardown: str


def utc_now() -> str:
    """Return one RFC3339 UTC timestamp."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def cell_from_id(cell_id: str) -> design.Cell:
    """Return one frozen cell by its stable identifier."""

    for cell in design.CELLS:
        if cell.cell_id == cell_id:
            return cell
    raise ValueError(f"unknown cell id: {cell_id}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse one complete runner request."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("smoke", "pilot", "confirmatory"), required=True
    )
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--block-id", type=int, required=True)
    parser.add_argument("--position", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--collector-url", required=True)
    parser.add_argument("--collector-pod", required=True)
    parser.add_argument("--namespace", default="cpu-limits")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--ready-timeout", type=int, default=120)
    parser.add_argument("--warmup-seconds", type=int, default=5)
    parser.add_argument("--measure-seconds", type=int, default=15)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    if args.block_id < 1 or args.position < 1:
        parser.error("--block-id and --position must be positive")
    if args.ready_timeout < 1 or args.warmup_seconds < 0 or args.measure_seconds < 1:
        parser.error("timeouts and measurement duration must be positive")
    try:
        cell_from_id(args.cell_id)
    except ValueError as error:
        parser.error(str(error))
    return args


class Kubectl:
    """Run kubectl with the fixed namespace and optional kubeconfig."""

    def __init__(self, namespace: str, kubeconfig: Path | None) -> None:
        self.namespace = namespace
        self.kubeconfig = kubeconfig

    def run(self, *arguments: str, input_text: str | None = None) -> str:
        command = ["kubectl"]
        if self.kubeconfig is not None:
            command.extend(("--kubeconfig", str(self.kubeconfig)))
        command.extend(("-n", self.namespace, *arguments))
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                check=True,
            )
        except FileNotFoundError as error:
            raise CellRunError("kubectl is required") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or error.stdout.strip() or str(error)
            raise CellRunError(f"kubectl command failed: {detail}") from error
        return completed.stdout


def _target_name(cell: design.Cell, run_id: str) -> str:
    return f"cpu-limits-{cell.workload}-{cell.policy}-{run_id}".lower()


def _support_name(kind: str, cell: design.Cell, run_id: str) -> str:
    return f"cpu-limits-{kind}-{cell.cell_id}-{run_id}".lower()


def _pod_manifest(
    name: str, node: str, run_id: str, cell_id: str, command: list[str]
) -> str:
    """Build one fixed support Pod manifest on the selected node."""

    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/name": "cpu-limits-support",
                "cpu-limits.k8labs.io/run-id": run_id,
                "cpu-limits.k8labs.io/cell-id": cell_id,
            },
        },
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "stress-ng",
                    "image": "polinux/stress-ng@sha256:aa26e8ffd2c0a1192de691a22401a5fd008a940bb8f30dc2a4ddfe72e0dfdc10",
                    "args": command,
                    "resources": {"requests": {"cpu": "1000m"}},
                }
            ],
        },
    }
    return yaml.safe_dump(document, sort_keys=False)


def _target_manifest(cell: design.Cell, node: str, run_id: str) -> str:
    """Render one source manifest and add the cell label for the collector."""

    values = design.CPU_VALUES[cell.workload]
    text = render(
        workload=cell.workload,
        policy=cell.policy,
        node=node,
        run_id=run_id,
        fit_cpu=values["fit"],
        low_cpu=values["low"],
    )
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise CellRunError("rendered target manifest is not an object")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise CellRunError("rendered target manifest has no metadata")
    labels = metadata.setdefault("labels", {})
    if not isinstance(labels, dict):
        raise CellRunError("rendered target labels are invalid")
    labels["cpu-limits.k8labs.io/cell-id"] = cell.cell_id
    return yaml.safe_dump(document, sort_keys=False)


def _pod_json(kubectl: Kubectl, name: str) -> Mapping[str, object]:
    output = kubectl.run("get", "pod", name, "-o", "json")
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise CellRunError(f"Pod {name} returned invalid JSON") from error
    if not isinstance(value, Mapping):
        raise CellRunError(f"Pod {name} JSON is not an object")
    return value


def _pod_uid(pod: Mapping[str, object]) -> str:
    metadata = pod.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CellRunError("Pod has no metadata")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise CellRunError("Pod UID is missing")
    return uid


def wait_for_ready(kubectl: Kubectl, name: str, timeout: int) -> Mapping[str, object]:
    """Wait for Ready and stop early if Kubernetes reports a terminal failure."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = _pod_json(kubectl, name)
        status = pod.get("status")
        if not isinstance(status, Mapping):
            raise CellRunError(f"Pod {name} has no status")
        phase = status.get("phase")
        if phase in {"Failed", "Succeeded"}:
            raise CellRunError(f"Pod {name} reached terminal phase {phase}")
        conditions = status.get("conditions", ())
        if isinstance(conditions, list):
            for condition in conditions:
                if (
                    isinstance(condition, Mapping)
                    and condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                ):
                    return pod
        time.sleep(1)
    raise CellRunError(f"Pod {name} was not Ready within {timeout} seconds")


def snapshot_pod(
    kubectl: Kubectl,
    collector_pod: str,
    pod_uid: str,
    args: argparse.Namespace,
) -> Mapping[str, object]:
    """Run the read-only probe in the collector and validate its JSON record."""

    output = kubectl.run(
        "exec",
        collector_pod,
        "--",
        "python3",
        "/opt/cpu-limits/cgroup_probe.py",
        "/host/sys/fs/cgroup",
        pod_uid,
        "--run-id",
        args.run_id,
        "--block-id",
        str(args.block_id),
        "--cell-id",
        args.cell_id,
        "--node",
        args.node,
    )
    try:
        record = json.loads(output)
    except json.JSONDecodeError as error:
        raise CellRunError("collector cgroup snapshot is not JSON") from error
    if not isinstance(record, Mapping):
        raise CellRunError("collector cgroup snapshot is not an object")
    levels = record.get("levels")
    if not isinstance(levels, list) or not levels:
        raise CellRunError("collector cgroup snapshot has no levels")
    return record


def parse_metrics(text: str, cell_id: str, timestamp: str) -> list[dict[str, str]]:
    """Parse target samples and reject any required missing metric family."""

    rows: list[dict[str, str]] = []
    found: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#") or "{" not in line:
            continue
        metric, rest = line.split("{", 1)
        labels_text, separator, value = rest.partition("}")
        if not separator:
            continue
        labels: dict[str, str] = {}
        for item in labels_text.split(","):
            key, equal, encoded = item.partition("=")
            if (
                not equal
                or len(encoded) < 2
                or not encoded.startswith('"')
                or not encoded.endswith('"')
            ):
                raise CellRunError(f"invalid Prometheus label in {metric}")
            labels[key] = bytes(encoded[1:-1], "utf-8").decode("unicode_escape")
        if labels.get("cell_id") != cell_id:
            continue
        raw_value = value.strip()
        try:
            float(raw_value)
        except ValueError as error:
            raise CellRunError(f"metric {metric} has no numeric value") from error
        if metric in REQUIRED_METRICS:
            found.add(metric)
        rows.append(
            {
                "timestamp": timestamp,
                "source": "collector",
                "metric": metric,
                "value": raw_value,
                "unit": "seconds" if metric.endswith("_seconds_total") else "count",
                "labels_json": json.dumps(labels, sort_keys=True),
            }
        )
    missing = sorted(REQUIRED_METRICS - found)
    if missing:
        raise CellRunError(
            "collector is missing required metric(s): " + ", ".join(missing)
        )
    return rows


class MetricsScraper:
    """Fetch stored collector samples at one-second intervals."""

    def __init__(self, url: str, cell_id: str) -> None:
        self.url = url
        self.cell_id = cell_id
        self.samples: list[dict[str, str]] = []
        self.error: CellRunError | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list[dict[str, str]]:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise CellRunError("metrics scraper did not stop")
        if self.error is not None:
            raise self.error
        if not self.samples:
            raise CellRunError("metrics scraper recorded no samples")
        return self.samples

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with urlopen(self.url, timeout=5) as response:
                    payload = response.read().decode("utf-8")
                self.samples.extend(parse_metrics(payload, self.cell_id, utc_now()))
            except (OSError, UnicodeError, URLError, CellRunError) as error:
                self.error = CellRunError(f"metrics scrape failed: {error}")
                self._stop.set()
                return
            self._stop.wait(1)


def _cgroup_rows(record: Mapping[str, object], window: str) -> Iterable[dict[str, str]]:
    captured_at = record.get("captured_at")
    levels = record.get("levels")
    if not isinstance(captured_at, str) or not isinstance(levels, list):
        raise CellRunError("cgroup snapshot is incomplete")
    for level in levels:
        if not isinstance(level, Mapping):
            raise CellRunError("cgroup snapshot has an invalid level")
        kind = level.get("kind")
        path = level.get("path")
        if not isinstance(kind, str) or not isinstance(path, str):
            raise CellRunError("cgroup snapshot level is missing kind or path")
        quota = level.get("cpu_max_quota_us")
        if quota is None:
            quota = "max"
        values: list[tuple[str, object, str]] = [
            ("cpu.weight", level.get("cpu_weight"), "weight"),
            ("cpu.max.quota", quota, "microseconds"),
            ("cpu.max.period", level.get("cpu_max_period_us"), "microseconds"),
        ]
        cpu_stat = level.get("cpu_stat")
        pressure = level.get("cpu_pressure")
        memory = level.get("memory")
        if not all(
            isinstance(value, Mapping) for value in (cpu_stat, pressure, memory)
        ):
            raise CellRunError("cgroup snapshot is missing required counters")
        for key in cgroup_probe.CPU_STAT_KEYS:
            values.append(
                (
                    f"cpu.stat.{key}",
                    cpu_stat.get(key),
                    "microseconds" if key.endswith("usec") else "count",
                )
            )
        for key in ("some_total_usec", "full_total_usec"):
            values.append((f"cpu.pressure.{key}", pressure.get(key), "microseconds"))
        for key in ("current_bytes", "peak_bytes"):
            values.append((f"memory.{key}", memory.get(key), "bytes"))
        for key, value, unit in values:
            if value is None:
                raise CellRunError(f"cgroup snapshot is missing {key}")
            yield {
                "captured_at": captured_at,
                "window": window,
                "level": kind,
                "path": path,
                "key": key,
                "value": str(value),
                "unit": unit,
            }


def _delta_rows(
    before: Mapping[str, object], after: Mapping[str, object]
) -> Iterable[dict[str, str]]:
    captured_at = after.get("captured_at")
    if not isinstance(captured_at, str):
        raise CellRunError("end cgroup snapshot has no timestamp")
    try:
        deltas = cgroup_probe.cpu_stat_deltas(before, after)
    except (TypeError, ValueError) as error:
        raise CellRunError(f"cannot calculate cgroup deltas: {error}") from error
    for delta in deltas:
        path = delta.get("path")
        kind = delta.get("kind")
        stats = delta.get("cpu_stat")
        if (
            not isinstance(path, str)
            or not isinstance(kind, str)
            or not isinstance(stats, Mapping)
        ):
            raise CellRunError("cgroup delta is incomplete")
        for key in cgroup_probe.CPU_STAT_KEYS:
            value = stats.get(key)
            if not isinstance(value, int):
                raise CellRunError(f"cgroup delta is missing {key}")
            yield {
                "captured_at": captured_at,
                "window": "measurement_delta",
                "level": kind,
                "path": path,
                "key": f"cpu.stat.{key}",
                "value": str(value),
                "unit": "microseconds" if key.endswith("usec") else "count",
            }


def append_csv(
    path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]
) -> None:
    """Append complete rows to a CSV file and write its header once."""

    materialized = list(rows)
    if not materialized:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerows(materialized)


def _delete_pod(kubectl: Kubectl, name: str) -> bool:
    try:
        kubectl.run("delete", "pod", name, "--ignore-not-found", "--wait=true")
    except CellRunError:
        return False
    return True


def run_cell(args: argparse.Namespace) -> CellResult:
    """Run one cell, write complete observations, and always remove owned Pods."""

    cell = cell_from_id(args.cell_id)
    root = args.data_root / args.phase
    cell_dir = root / "cells" / f"block-{args.block_id:03d}" / cell.cell_id
    cell_dir.mkdir(parents=True, exist_ok=True)
    kubectl = Kubectl(args.namespace, args.kubeconfig)
    target_name = _target_name(cell, args.run_id)
    owned_names = [target_name]
    if cell.condition == "contention":
        owned_names.append(_support_name("noise", cell, args.run_id))
    started_at = utc_now()
    pod_uid = ""
    status = "failed"
    error_text: str | None = None
    scraper: MetricsScraper | None = None
    trace_process: subprocess.Popen[str] | None = None
    teardown = "complete"
    try:
        kubectl.run(
            "apply",
            "-f",
            "-",
            input_text=_target_manifest(cell, args.node, args.run_id),
        )
        if cell.condition == "contention":
            noise_name = owned_names[1]
            kubectl.run(
                "apply",
                "-f",
                "-",
                input_text=_pod_manifest(
                    noise_name,
                    args.node,
                    args.run_id,
                    cell.cell_id,
                    ["--cpu", "1", "--cpu-method", "matrixprod", "--timeout", "25s"],
                ),
            )
            wait_for_ready(kubectl, noise_name, args.ready_timeout)
        target = wait_for_ready(kubectl, target_name, args.ready_timeout)
        pod_uid = _pod_uid(target)
        inventory = {
            "protocol": PROTOCOL,
            "pod_uid": pod_uid,
            "pod": target_name,
            "node": args.node,
            "cell_id": cell.cell_id,
        }
        (cell_dir / "pods.json").write_text(
            json.dumps(inventory, indent=2) + "\n", encoding="utf-8"
        )
        before = snapshot_pod(kubectl, args.collector_pod, pod_uid, args)
        (cell_dir / "cgroup-start.json").write_text(
            json.dumps(before, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        scraper = MetricsScraper(args.collector_url, cell.cell_id)
        scraper.start()
        if args.trace:
            trace_dir = cell_dir / "trace"
            trace_command = [
                str(SERIES_ROOT / "capture" / "capture-cell.sh"),
                "--collector-pod",
                args.collector_pod,
                "--namespace",
                args.namespace,
                "--duration",
                str(min(5, args.measure_seconds)),
                "--out-dir",
                str(trace_dir),
            ]
            if args.kubeconfig is not None:
                trace_command.extend(("--kubeconfig", str(args.kubeconfig)))
            trace_process = subprocess.Popen(
                trace_command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        time.sleep(args.warmup_seconds)
        time.sleep(args.measure_seconds)
        metrics = scraper.stop()
        scraper = None
        if trace_process is not None:
            _, trace_error = trace_process.communicate(timeout=30)
            if trace_process.returncode != 0:
                raise CellRunError("trace capture failed: " + trace_error.strip())
            trace_process = None
        after = snapshot_pod(kubectl, args.collector_pod, pod_uid, args)
        (cell_dir / "cgroup-end.json").write_text(
            json.dumps(after, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        cgroup_rows = [
            *_cgroup_rows(before, "start"),
            *_cgroup_rows(after, "end"),
            *_delta_rows(before, after),
        ]
        append_csv(
            root / "cgroups.csv",
            CGROUP_COLUMNS,
            [
                {
                    "protocol": PROTOCOL,
                    "run_id": args.run_id,
                    "block_id": str(args.block_id),
                    "cell_id": cell.cell_id,
                    **row,
                }
                for row in cgroup_rows
            ],
        )
        append_csv(
            root / "metrics.csv",
            METRIC_COLUMNS,
            [
                {
                    "protocol": PROTOCOL,
                    "run_id": args.run_id,
                    "block_id": str(args.block_id),
                    "cell_id": cell.cell_id,
                    **row,
                }
                for row in metrics
            ],
        )
        status = "complete"
    except (CellRunError, OSError, ValueError, yaml.YAMLError) as error:
        error_text = str(error)
        if scraper is not None:
            try:
                scraper.stop()
            except CellRunError as scraper_error:
                error_text = f"{error_text}; {scraper_error}"
    finally:
        if trace_process is not None and trace_process.poll() is None:
            trace_process.terminate()
            try:
                trace_process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                trace_process.kill()
                trace_process.communicate()
        failed_deletes = [
            name for name in owned_names if not _delete_pod(kubectl, name)
        ]
        if failed_deletes:
            teardown = "failed"
            if error_text is None:
                error_text = "could not delete owned Pod(s): " + ", ".join(
                    failed_deletes
                )
            status = "failed"
        finished_at = utc_now()
        result = CellResult(
            status, pod_uid, started_at, finished_at, error_text, teardown
        )
        (cell_dir / "status.json").write_text(
            json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
        )
        append_csv(
            root / "blocks.csv",
            BLOCK_COLUMNS,
            [
                {
                    "protocol": PROTOCOL,
                    "phase": args.phase,
                    "run_id": args.run_id,
                    "block_id": str(args.block_id),
                    "seed": str(args.seed),
                    "position": str(args.position),
                    "cell_id": cell.cell_id,
                    "workload": cell.workload,
                    "policy": cell.policy,
                    "condition": cell.condition,
                    "node": args.node,
                    "pod_uid": pod_uid,
                    "status": status,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            ],
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run one cell and return non-zero for a failed or incomplete cell."""

    args = parse_args(argv)
    result = run_cell(args)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
