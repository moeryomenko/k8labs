"""Expose selected CPU-limits cgroups in Prometheus text format.

The collector reads its cgroup root only. It emits metrics only for cgroups
listed by its fixture or Kubernetes inventory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

METRICS: tuple[tuple[str, str, str], ...] = (
    (
        "container_cpu_usage_seconds_total",
        "Cumulative CPU time used by the target cgroup.",
        "counter",
    ),
    (
        "container_cpu_cfs_periods_total",
        "Cumulative CPU quota periods for the target cgroup.",
        "counter",
    ),
    (
        "container_cpu_cfs_throttled_periods_total",
        "Cumulative throttled CPU quota periods for the target cgroup.",
        "counter",
    ),
    (
        "container_cpu_cfs_throttled_seconds_total",
        "Cumulative CPU throttled time for the target cgroup.",
        "counter",
    ),
    (
        "container_memory_working_set_bytes",
        "Memory current minus inactive file cache for the target cgroup.",
        "gauge",
    ),
    (
        "container_pressure_cpu_waiting_seconds_total",
        "Cumulative CPU pressure waiting time for the target cgroup.",
        "counter",
    ),
    (
        "k8labs_cgroup_cpu_weight",
        "Exact cgroup v2 cpu.weight value.",
        "gauge",
    ),
    (
        "k8labs_cgroup_cpu_max_quota",
        "Exact numeric cgroup v2 cpu.max quota in microseconds.",
        "gauge",
    ),
    (
        "k8labs_cgroup_cpu_max_period",
        "Exact cgroup v2 cpu.max period in microseconds.",
        "gauge",
    ),
    (
        "k8labs_cgroup_cpu_usage_seconds_total",
        "Exact cgroup v2 cpu.stat usage time in seconds.",
        "counter",
    ),
)

UID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")


@dataclass(frozen=True)
class CgroupTarget:
    """A selected cgroup and the Kubernetes metadata that describes it."""

    uid: str
    namespace: str
    pod: str
    container: str
    qos: str
    level: str
    cell_id: str
    cgroup_path: str

    def labels(self, node: str) -> dict[str, str]:
        """Return the fixed labels for target metrics."""

        return {
            "node": node,
            "namespace": self.namespace,
            "pod": self.pod,
            "pod_uid": self.uid,
            "container": self.container,
            "qos": self.qos,
            "level": self.level,
            "cell_id": self.cell_id,
        }


class InventoryFormatError(TypeError):
    """An inventory has an unsupported JSON shape."""


class FixtureInventory:
    """Target cgroups supplied by an offline JSON fixture or Kubernetes API."""

    def __init__(self, targets: Iterable[CgroupTarget]) -> None:
        self.targets = tuple(targets)
        self._by_uid: dict[str, tuple[CgroupTarget, ...]] = {}
        for target in self.targets:
            self._by_uid[target.uid] = (*self._by_uid.get(target.uid, ()), target)

    @classmethod
    def from_file(cls, path: Path) -> FixtureInventory:
        """Load an offline inventory fixture from JSON."""

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise InventoryFormatError("inventory fixture must contain a JSON object")
        return cls.from_data(data)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> FixtureInventory:
        """Build targets from the documented offline fixture shape."""

        pods = data.get("pods", data.get("items", ()))
        if not isinstance(pods, list):
            raise InventoryFormatError("inventory pods must be a JSON list")

        targets: list[CgroupTarget] = []
        for pod_data in pods:
            if not isinstance(pod_data, Mapping):
                raise InventoryFormatError("inventory pod entries must be JSON objects")
            uid = _required_text(pod_data, "uid")
            namespace = _optional_text(pod_data, "namespace")
            pod = _optional_text(pod_data, "pod", "name")
            qos = _optional_text(pod_data, "qos", "qos_class")
            cell_id = _optional_text(pod_data, "cell_id")
            containers = pod_data.get("containers", ())
            if not isinstance(containers, list):
                raise InventoryFormatError(
                    f"inventory pod {uid}: containers must be a JSON list"
                )
            for container_data in containers:
                if not isinstance(container_data, Mapping):
                    raise InventoryFormatError(
                        f"inventory pod {uid}: container must be a JSON object"
                    )
                container = _optional_text(container_data, "name", "container")
                cgroups = container_data.get("cgroups", ())
                if isinstance(cgroups, Mapping):
                    cgroup_items = cgroups.items()
                elif isinstance(cgroups, list):
                    cgroup_items = _list_cgroup_items(cgroups, uid)
                else:
                    raise InventoryFormatError(
                        f"inventory pod {uid}: cgroups must be an object or list"
                    )
                for level, cgroup_path in cgroup_items:
                    if not isinstance(level, str) or not isinstance(cgroup_path, str):
                        raise InventoryFormatError(
                            f"inventory pod {uid}: invalid cgroup entry"
                        )
                    targets.append(
                        CgroupTarget(
                            uid=uid,
                            namespace=namespace,
                            pod=pod,
                            container=container,
                            qos=qos,
                            level=level,
                            cell_id=cell_id,
                            cgroup_path=cgroup_path,
                        )
                    )
        return cls(targets)

    def targets_for_uid(self, uid: str) -> tuple[CgroupTarget, ...]:
        """Return only the targets registered to one pod UID."""

        return self._by_uid.get(uid, ())


class KubernetesInventory(FixtureInventory):
    """Target cgroups discovered for Pods returned by the Kubernetes API."""

    @classmethod
    def from_service_account(cls, cgroup_root: Path) -> KubernetesInventory:
        """Load pod metadata with the mounted service-account token."""

        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("mounted Kubernetes service-account token is empty")

        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host or not port.isdecimal():
            raise ValueError("Kubernetes service host or HTTPS port is invalid")
        context = _kubernetes_ssl_context()
        request = Request(
            f"https://{host}:{port}/api/v1/pods",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urlopen(request, context=context, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise InventoryFormatError(
                "Kubernetes API returned an invalid Pod inventory"
            )

        targets: list[CgroupTarget] = []
        items = payload.get("items", ())
        if not isinstance(items, list):
            raise InventoryFormatError("Kubernetes API Pod inventory has no items list")
        for item in items:
            if not isinstance(item, Mapping):
                continue
            targets.extend(_targets_from_kubernetes_pod(item, cgroup_root))
        return cls(targets)


def _required_text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"inventory entry needs non-empty {key}")
    return value


def _optional_text(values: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str):
            return value
    return ""


def _list_cgroup_items(cgroups: list[Any], uid: str) -> Iterable[tuple[str, str]]:
    for entry in cgroups:
        if not isinstance(entry, Mapping):
            raise InventoryFormatError(
                f"inventory pod {uid}: cgroup must be a JSON object"
            )
        level = entry.get("level")
        path = entry.get("path")
        if not isinstance(level, str) or not isinstance(path, str):
            raise InventoryFormatError(
                f"inventory pod {uid}: cgroup needs level and path"
            )
        yield level, path


def _kubernetes_ssl_context() -> ssl.SSLContext:
    certificate = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if certificate.is_file():
        return ssl.create_default_context(cafile=certificate)
    return ssl.create_default_context()


def _targets_from_kubernetes_pod(
    pod_data: Mapping[str, Any], cgroup_root: Path
) -> list[CgroupTarget]:
    metadata = pod_data.get("metadata")
    status = pod_data.get("status")
    spec = pod_data.get("spec")
    if not all(isinstance(value, Mapping) for value in (metadata, status, spec)):
        return []
    assert isinstance(metadata, Mapping)
    assert isinstance(status, Mapping)
    assert isinstance(spec, Mapping)
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not UID_PATTERN.fullmatch(uid):
        return []

    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    label_values = labels if isinstance(labels, Mapping) else {}
    annotation_values = annotations if isinstance(annotations, Mapping) else {}
    cell_id = _metadata_cell_id(label_values, annotation_values)
    namespace = _optional_text(metadata, "namespace")
    pod = _optional_text(metadata, "name")
    qos = _optional_text(status, "qosClass")
    containers = spec.get("containers", ())
    if not isinstance(containers, list):
        return []

    container_names = [
        _optional_text(container, "name")
        for container in containers
        if isinstance(container, Mapping)
    ]
    return _discover_targets(
        cgroup_root,
        uid,
        namespace,
        pod,
        container_names,
        qos,
        cell_id,
    )


def _metadata_cell_id(labels: Mapping[Any, Any], annotations: Mapping[Any, Any]) -> str:
    for values in (labels, annotations):
        for key in ("cpu-limits.k8labs.io/cell-id", "k8labs.io/cell-id", "cell_id"):
            value = values.get(key)
            if isinstance(value, str):
                return value
    return ""


def _discover_targets(
    cgroup_root: Path,
    uid: str,
    namespace: str,
    pod: str,
    containers: list[str],
    qos: str,
    cell_id: str,
) -> list[CgroupTarget]:
    """Find cgroup paths that contain the selected Pod UID only."""

    root = cgroup_root.resolve()
    uid_forms = (uid, uid.replace("-", "_"))
    matches: list[Path] = []
    try:
        for candidate in root.rglob("*"):
            if candidate.is_dir() and any(
                uid_form in candidate.name for uid_form in uid_forms
            ):
                matches.append(candidate)
    except OSError:
        return []

    targets: list[CgroupTarget] = []
    for pod_path in matches:
        parts = pod_path.relative_to(root).parts
        qos_index = next(
            (
                index
                for index, part in enumerate(parts)
                if part.startswith("kubepods") and part.endswith(".slice")
            ),
            None,
        )
        if qos_index is not None:
            targets.append(
                _target_for_path(
                    root / Path(*parts[: qos_index + 1]),
                    root,
                    uid,
                    namespace,
                    pod,
                    "",
                    qos,
                    "qos_slice",
                    cell_id,
                )
            )
        targets.append(
            _target_for_path(
                pod_path,
                root,
                uid,
                namespace,
                pod,
                "",
                qos,
                "pod_slice",
                cell_id,
            )
        )
        for child in _safe_child_directories(pod_path):
            container = _container_for_path(child, containers)
            level = (
                "container_scope" if child.name.endswith(".scope") else "container_leaf"
            )
            targets.append(
                _target_for_path(
                    child,
                    root,
                    uid,
                    namespace,
                    pod,
                    container,
                    qos,
                    level,
                    cell_id,
                )
            )

    unique: dict[tuple[str, str], CgroupTarget] = {}
    for target in targets:
        unique[(target.level, target.cgroup_path)] = target
    return list(unique.values())


def _safe_child_directories(path: Path) -> list[Path]:
    try:
        return [child for child in path.iterdir() if child.is_dir()]
    except OSError:
        return []


def _container_for_path(path: Path, containers: list[str]) -> str:
    for container in containers:
        if container and container in path.name:
            return container
    return ""


def _target_for_path(
    path: Path,
    root: Path,
    uid: str,
    namespace: str,
    pod: str,
    container: str,
    qos: str,
    level: str,
    cell_id: str,
) -> CgroupTarget:
    return CgroupTarget(
        uid=uid,
        namespace=namespace,
        pod=pod,
        container=container,
        qos=qos,
        level=level,
        cell_id=cell_id,
        cgroup_path=str(path.relative_to(root)),
    )


class Collector:
    """Read registered cgroups and render their metrics without host leakage."""

    def __init__(
        self, cgroup_root: Path, inventory: FixtureInventory, node: str
    ) -> None:
        self.cgroup_root = cgroup_root.resolve()
        self.inventory = inventory
        self.node = node

    def cgroups_for_uid(self, uid: str) -> list[dict[str, str]]:
        """Return registered cgroups for one valid UID and no other UID."""

        if not UID_PATTERN.fullmatch(uid):
            return []
        cgroups: list[dict[str, str]] = []
        for target in self.inventory.targets_for_uid(uid):
            path = self._safe_cgroup_path(target.cgroup_path)
            if path is not None:
                cgroups.append(
                    {
                        "path": str(path.relative_to(self.cgroup_root)),
                        "level": target.level,
                        "container": target.container,
                        "namespace": target.namespace,
                        "pod": target.pod,
                        "qos": target.qos,
                        "cell_id": target.cell_id,
                    }
                )
        return cgroups

    def render_metrics(self) -> str:
        """Render all required metric families in Prometheus text format."""

        samples: dict[str, list[tuple[dict[str, str], int | float]]] = {
            name: [] for name, _, _ in METRICS
        }
        for target in self.inventory.targets:
            path = self._safe_cgroup_path(target.cgroup_path)
            if path is None:
                continue
            labels = target.labels(self.node)
            for metric, value in self._read_target_metrics(path).items():
                samples[metric].append((labels, value))

        lines: list[str] = []
        for name, help_text, metric_type in METRICS:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
            for labels, value in sorted(samples[name], key=_sample_sort_key):
                lines.append(f"{name}{_render_labels(labels)} {_render_value(value)}")
        return "\n".join(lines) + "\n"

    def _safe_cgroup_path(self, relative_path: str) -> Path | None:
        candidate = Path(relative_path)
        path = candidate if candidate.is_absolute() else self.cgroup_root / candidate
        try:
            resolved = path.resolve()
            resolved.relative_to(self.cgroup_root)
        except (OSError, ValueError):
            return None
        if resolved == self.cgroup_root:
            return None
        return resolved

    def _read_target_metrics(self, path: Path) -> dict[str, int | float]:
        cpu_stat = _read_stat_file(path / "cpu.stat")
        memory_stat = _read_stat_file(path / "memory.stat")
        result: dict[str, int | float] = {}

        usage_usec = _nonnegative_int(cpu_stat.get("usage_usec"))
        if usage_usec is not None:
            usage_seconds = usage_usec / 1_000_000
            result["container_cpu_usage_seconds_total"] = usage_seconds
            result["k8labs_cgroup_cpu_usage_seconds_total"] = usage_seconds
        for stat_name, metric_name, scale in (
            ("nr_periods", "container_cpu_cfs_periods_total", 1),
            ("nr_throttled", "container_cpu_cfs_throttled_periods_total", 1),
            ("throttled_usec", "container_cpu_cfs_throttled_seconds_total", 1_000_000),
        ):
            value = _nonnegative_int(cpu_stat.get(stat_name))
            if value is not None:
                result[metric_name] = value / scale

        weight = _read_integer(path / "cpu.weight")
        if weight is not None:
            result["k8labs_cgroup_cpu_weight"] = weight
        quota, period = _read_cpu_max(path / "cpu.max")
        if quota is not None:
            result["k8labs_cgroup_cpu_max_quota"] = quota
        if period is not None:
            result["k8labs_cgroup_cpu_max_period"] = period

        memory_current = _read_integer(path / "memory.current")
        if memory_current is not None:
            inactive_file = _nonnegative_int(memory_stat.get("inactive_file")) or 0
            result["container_memory_working_set_bytes"] = max(
                0, memory_current - inactive_file
            )

        pressure = _read_cpu_pressure(path / "cpu.pressure")
        if pressure is not None:
            result["container_pressure_cpu_waiting_seconds_total"] = (
                pressure / 1_000_000
            )
        return result


def _read_stat_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            values[parts[0]] = parts[1]
    return values


def _read_integer(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _nonnegative_int(value)


def _nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.isdecimal():
        return None
    return int(value)


def _read_cpu_max(path: Path) -> tuple[int | None, int | None]:
    try:
        parts = path.read_text(encoding="utf-8").split()
    except OSError:
        return None, None
    if len(parts) != 2:
        return None, None
    quota = None if parts[0] == "max" else _nonnegative_int(parts[0])
    return quota, _nonnegative_int(parts[1])


def _read_cpu_pressure(path: Path) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if not fields or fields[0] != "some":
            continue
        for field in fields[1:]:
            key, separator, value = field.partition("=")
            if key == "total" and separator:
                return _nonnegative_int(value)
    return None


def _sample_sort_key(sample: tuple[dict[str, str], int | float]) -> tuple[str, ...]:
    return tuple(sample[0][key] for key in sorted(sample[0]))


def _render_labels(labels: Mapping[str, str]) -> str:
    encoded = ",".join(
        f'{key}="{_escape_label(labels[key])}"' for key in sorted(labels)
    )
    return "{" + encoded + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_value(value: float) -> str:
    return format(value, ".15g")


def make_handler(collector: Collector) -> type[BaseHTTPRequestHandler]:
    """Return a request handler bound to one collector instance."""

    class CollectorHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path in {"/healthz", "/readyz"}:
                self._respond(200, "text/plain; charset=utf-8", b"ok\n")
                return
            if parsed.path == "/metrics":
                self._respond(
                    200,
                    "text/plain; version=0.0.4; charset=utf-8",
                    collector.render_metrics().encode("utf-8"),
                )
                return
            if parsed.path == "/cgroups":
                uid = parse_qs(parsed.query).get("uid", [""])[0]
                payload = json.dumps(
                    {"uid": uid, "cgroups": collector.cgroups_for_uid(uid)},
                    sort_keys=True,
                ).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", payload)
                return
            self._respond(404, "text/plain; charset=utf-8", b"not found\n")

        def _respond(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return CollectorHandler


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse collector service options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--fixture-inventory", type=Path)
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9100)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Start the collector on port 9100 unless another port is requested."""

    args = parse_args(argv)
    if args.fixture_inventory is not None:
        inventory = FixtureInventory.from_file(args.fixture_inventory)
    else:
        inventory = KubernetesInventory.from_service_account(args.cgroup_root)
    collector = Collector(args.cgroup_root, inventory, args.node)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(collector))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
