"""Kubernetes access for the CPU-limit campaign.

Only the operations a measurement needs exist here: render and apply one cell,
wait until it is actually serving, and read its cgroup counters. Everything is
plain HTTP against the API server so the harness needs no client library, no
kubeconfig rewriting, and no in-cluster agent.

The API server is reached through a local kubectl port-forward. That keeps the
harness identical however the cluster is exposed (VPN, bastion, or a local
node), and it avoids assuming a particular kubeconfig layout.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import ssl
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The renderer lives in `manifests/render-cell.py` because it also has a CLI
# entry point for reproducing a cell by hand. It is loaded here so the campaign
# and a human resolve cell resources through the same validated code path. The
# file name is not a valid module name, so it is loaded by path.
_RENDERER_PATH = Path(__file__).resolve().parent.parent / "manifests" / "render-cell.py"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("render_cell", _RENDERER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - missing file
        raise ImportError(f"cannot load the cell renderer from {_RENDERER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_cell = _load_renderer()
RenderError = render_cell.RenderError
render = render_cell.render

NAMESPACE = "cpu-limit-autoscale"


class KubernetesError(RuntimeError):
    """The cluster answered, but not with what the measurement requires."""


@dataclass(frozen=True)
class CellSpec:
    """Every input the renderer needs, with the measured fields explicit.

    `cpu_limit_millicores` and `cpu_request_millicores` are integers so the
    harness can compare them and derive brackets without re-parsing quantities.
    """

    cell: str
    mix: str
    cpu_limit_millicores: int
    cpu_request_millicores: int
    cpu_iterations: int = 20_000
    postgres_pool_size: int = 16
    redis_pool_size: int = 16
    trace_sample_ratio: float = 0.0
    sidecar_cpu_request_millicores: int = 50
    sidecar_cpu_limit_millicores: int = 100

    def render_values(self) -> dict[str, str]:
        return {
            "CELL": self.cell,
            "MIX": self.mix,
            "CPU_ITERATIONS": str(self.cpu_iterations),
            "POSTGRES_POOL_SIZE": str(self.postgres_pool_size),
            "REDIS_POOL_SIZE": str(self.redis_pool_size),
            "TRACE_SAMPLE_RATIO": f"{self.trace_sample_ratio:g}",
            "CPU_REQUEST": f"{self.cpu_request_millicores}m",
            "CPU_LIMIT": f"{self.cpu_limit_millicores}m",
            "SIDECAR_CPU_REQUEST": f"{self.sidecar_cpu_request_millicores}m",
            "SIDECAR_CPU_LIMIT": f"{self.sidecar_cpu_limit_millicores}m",
        }

    def manifest(self) -> str:
        return render(self.render_values())


class PortForward:
    """A `kubectl port-forward` for the life of a `with` block.

    The forward is treated as ready only once it answers a request; kubectl
    prints "Forwarding from" before the listener is bound in some versions, and
    a request raced against that message fails intermittently.
    """

    def __init__(
        self,
        kubectl: Path,
        kubeconfig: Path,
        target: str,
        remote_port: int,
        local_port: int,
        ready_path: str = "/",
        namespace: str = NAMESPACE,
    ) -> None:
        self._kubectl = kubectl
        self._kubeconfig = kubeconfig
        self._target = target
        self._remote_port = remote_port
        self._local_port = local_port
        self._ready_path = ready_path
        self._namespace = namespace
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> PortForward:
        self._process = subprocess.Popen(
            [
                str(self._kubectl),
                f"--kubeconfig={self._kubeconfig}",
                "-n",
                self._namespace,
                "port-forward",
                self._target,
                f"{self._local_port}:{self._remote_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                details = ""
                if self._process.stderr is not None:
                    details = (
                        self._process.stderr.read().decode(errors="replace").strip()
                    )
                raise KubernetesError(
                    f"port-forward to {self._target}:{self._remote_port} exited immediately"
                    + (f": {details[:500]}" if details else "")
                )
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self._local_port, timeout=1
                )
                connection.request("GET", self._ready_path)
                connection.getresponse()
                connection.close()
                return self
            except OSError:
                time.sleep(0.1)
        self.__exit__(None, None, None)
        raise KubernetesError(
            f"port-forward to {self._target}:{self._remote_port} did not become ready"
        )

    def __exit__(self, *_exc: object) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None


class Kubeconfig:
    """The parts of a kubeconfig a client needs: server, CA, and client identity.

    `kubectl` stores credentials either as file paths or as base64 data; both
    appear in practice. Encoded data is written to a private temporary directory
    whose lifetime is the process, because `ssl` takes paths, not bytes.
    """

    def __init__(self, server: str, cacert: Path, cert: Path, key: Path) -> None:
        self.server = server
        self.cacert = cacert
        self.cert = cert
        self.key = key

    @classmethod
    def read(cls, path: Path, context: str | None = None) -> Kubeconfig:
        import base64
        import tempfile

        import yaml

        document = yaml.safe_load(path.read_text())
        cluster_name = context
        user_name: str | None = None
        active = document.get("current-context")
        selected = active if context is None else context
        for entry in document.get("contexts") or []:
            if entry.get("name") == selected:
                cluster_name = entry["context"]["cluster"]
                user_name = entry["context"].get("user")
                break
        if cluster_name is None:
            raise KubernetesError(f"context {selected!r} not found in {path}")

        cluster = next(
            (
                c["cluster"]
                for c in document.get("clusters") or []
                if c.get("name") == cluster_name
            ),
            None,
        )
        if cluster is None:
            raise KubernetesError(f"cluster {cluster_name!r} not found in {path}")

        user = next(
            (
                u["user"]
                for u in document.get("users") or []
                if u.get("name") == user_name
            ),
            None,
        )
        if user is None:
            raise KubernetesError(f"user {user_name!r} not found in {path}")

        scratch = Path(tempfile.mkdtemp(prefix="cpu-limit-kubeconfig-"))

        def materialise(field: str, data_field: str) -> Path:
            if user.get(field):
                return Path(user[field])
            if user.get(data_field):
                target = scratch / field
                target.write_bytes(base64.b64decode(user[data_field]))
                target.chmod(0o600)
                return target
            raise KubernetesError(
                f"kubeconfig user has neither {field} nor {data_field}"
            )

        cacert = scratch / "ca.crt"
        if cluster.get("certificate-authority"):
            cacert = Path(cluster["certificate-authority"])
        elif cluster.get("certificate-authority-data"):
            cacert.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
        else:
            raise KubernetesError("kubeconfig cluster has no certificate authority")

        server = cluster.get("server")
        if not server:
            raise KubernetesError(f"cluster {cluster_name!r} has no server address")
        return cls(
            server=server,
            cacert=cacert,
            cert=materialise("client-certificate", "client-certificate-data"),
            key=materialise("client-key", "client-key-data"),
        )


class ApiClient:
    """Minimal JSON client for the Kubernetes API built from a kubeconfig.

    `kubectl get --raw` is deliberately not used: it re-executes kubectl for
    every call, and a campaign makes hundreds of calls per cell. The API server
    is contacted directly at the address the kubeconfig already names, with the
    kubeconfig's own certificate authority and client credentials, so the
    harness behaves the same whether that address is a tunnel, a bastion, or a
    loopback a proxy owns.

    Only the credential shapes `kubectl` writes for a client-certificate
    context are supported. Anything else raises rather than silently falling
    back to an unauthenticated request.
    """

    def __init__(self, kubeconfig: Path, context: str | None = None) -> None:
        settings = Kubeconfig.read(kubeconfig, context=context)
        self._context = ssl.create_default_context(cafile=str(settings.cacert))
        self._context.load_cert_chain(
            certfile=str(settings.cert), keyfile=str(settings.key)
        )

        parsed = urllib.parse.urlsplit(settings.server)
        if parsed.scheme != "https" or not parsed.hostname:
            raise KubernetesError(
                f"unsupported API server address: {settings.server!r}"
            )
        self._host = parsed.hostname
        self._port = parsed.port or 443
        self._base_path = parsed.path.rstrip("/")

    def _connection(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(
            self._host, self._port, timeout=30, context=self._context
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | str | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        """Send one request. `body` may be a dict (JSON) or a pre-encoded string."""
        if body is None:
            payload = None
        elif isinstance(body, str):
            payload = body.encode()
        else:
            payload = json.dumps(body).encode()

        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = content_type or "application/json"

        connection = self._connection()
        try:
            connection.request(
                method, f"{self._base_path}{path}", body=payload, headers=headers
            )
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()

        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            decoded = {"raw": raw.decode(errors="replace")}
        return status, decoded

    def expect(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, decoded = self.request(method, path, body)
        if status >= 300:
            message = decoded.get("message") if isinstance(decoded, dict) else None
            raise KubernetesError(f"{method} {path} -> {status}: {message or decoded}")
        return decoded


def apply_document(client: ApiClient, document: dict[str, Any]) -> None:
    """Server-side apply one cell document.

    Server-side apply is idempotent and does not require the object to exist, so
    a retried cell can never leave a half-created Deployment behind and a cell
    re-applied after an edit converges instead of failing on a conflict.
    """
    kind = document["kind"]
    name = document["metadata"]["name"]
    query = "?fieldManager=cpu-limit-pilot&force=true"
    if kind == "Deployment":
        path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}{query}"
    elif kind == "Service":
        path = f"/api/v1/namespaces/{NAMESPACE}/services/{name}{query}"
    else:
        raise KubernetesError(f"refusing to apply unsupported kind {kind!r}")

    status, decoded = client.request(
        "PATCH", path, body=document, content_type="application/apply-patch+yaml"
    )
    if status >= 300:
        message = decoded.get("message") if isinstance(decoded, dict) else decoded
        raise KubernetesError(f"applying {kind} {name} failed: {status}: {message}")


def documents(manifest: str) -> list[dict[str, Any]]:
    """Parse a rendered multi-document manifest."""
    import yaml

    return [document for document in yaml.safe_load_all(manifest) if document]


def delete_cell(kubectl: Path, kubeconfig: Path, manifest_path: Path) -> None:
    subprocess.run(
        [
            str(kubectl),
            f"--kubeconfig={kubeconfig}",
            "-n",
            NAMESPACE,
            "delete",
            "--ignore-not-found",
            "--wait=true",
            "-f",
            str(manifest_path),
        ],
        check=False,
        capture_output=True,
    )


def cell_pod(client: ApiClient, cell: str) -> dict[str, Any] | None:
    """The single pod belonging to `cell`, or None while it does not exist."""
    selector = f"cpu-limit-autoscale/cell={cell}"
    listing = client.expect(
        "GET", f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={selector}"
    )
    pods = listing.get("items", [])
    if not pods:
        return None
    if len(pods) > 1:
        names = ", ".join(pod["metadata"]["name"] for pod in pods)
        raise KubernetesError(f"cell {cell} has more than one pod: {names}")
    return pods[0]


def pod_ready(pod: dict[str, Any] | None) -> bool:
    if pod is None:
        return False
    if pod.get("status", {}).get("phase") != "Running":
        return False
    statuses = pod.get("status", {}).get("containerStatuses") or []
    return bool(statuses) and all(status.get("ready") for status in statuses)


def pod_node(pod: dict[str, Any]) -> str:
    return pod.get("spec", {}).get("nodeName", "")


def pod_uid(pod: dict[str, Any]) -> str:
    return pod["metadata"]["uid"]


def node_capacity_millicores(client: ApiClient, node: str) -> int:
    """Allocatable CPU of a node in millicores.

    Allocatable is the bound that matters: it is capacity minus the kubelet
    reservation, and it is what a CPU limit must never exceed.
    """
    node_object = client.expect("GET", f"/api/v1/nodes/{node}")
    quantity = node_object["status"]["allocatable"]["cpu"]
    return parse_cpu_quantity(quantity)


def parse_cpu_quantity(value: str) -> int:
    """Parse a Kubernetes CPU quantity into millicores, rejecting surprises.

    Kubernetes accepts several spellings of the same number. The harness only
    needs millicores, and a value it misreads would silently widen the pilot's
    bracket, so anything unexpected raises rather than falls back.
    """
    if value.endswith("m"):
        return int(value[:-1])
    if value.isdigit():
        return int(value) * 1000
    raise KubernetesError(f"unsupported CPU quantity from the API server: {value!r}")


def node_taints(client: ApiClient, node: str) -> list[dict[str, Any]]:
    node_object = client.expect("GET", f"/api/v1/nodes/{node}")
    return node_object.get("spec", {}).get("taints", []) or []


def is_request_only_node(taints: list[dict[str, Any]]) -> bool:
    """True for a control-plane node the campaign must not schedule on.

    Only an explicit control-plane role taint excludes a node. Other taints (for
    example `node.kubernetes.io/unschedulable`, or a custom workload taint) are
    scheduling policy that the scheduler already honours, and the pilot records
    them instead of duplicating the decision.
    """
    return any(
        taint.get("key", "").startswith("node-role.kubernetes.io/control-plane")
        for taint in taints
    )


__all__ = [
    "ApiClient",
    "CellSpec",
    "KubernetesError",
    "NAMESPACE",
    "PortForward",
    "RenderError",
    "apply_document",
    "cell_pod",
    "delete_cell",
    "documents",
    "is_request_only_node",
    "node_capacity_millicores",
    "node_taints",
    "parse_cpu_quantity",
    "pod_node",
    "pod_ready",
    "pod_uid",
]
