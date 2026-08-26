"""Tests for the ``make multi-cluster-test`` target (TASK-018, test-first).

REQ-013 / VC-05: one make target must prove TWO concurrent workload clusters
on one host. The contract pinned here, stage by stage:

1. ``make multi-cluster-test`` runs ALL stages in order:
   server-side apply of ``capi/cluster.yaml`` AND ``capi/cluster-lab2.yaml``
   -> readiness wait for BOTH Clusters (k8labs, k8labs-2) -> fetch TWO
   kubeconfig Secrets into two DISTINCT paths carrying DISTINCT server ports
   -> smoke-test Job against BOTH kubeconfigs -> delete BOTH Clusters ->
   reclamation wait for BOTH.
2. Collision detector: when the two fetched kubeconfigs carry IDENTICAL
   server URLs, the target must FAIL with a message naming the URL(s). With
   identical URLs the two names are the same string, so naming the shared
   URL is what the test can observe; the fake serves the SAME payload for
   both Secrets in this mode.
3. Failure propagation: a failing stage (the second cluster never becomes
   Ready) aborts nonzero with an actionable message naming the stage and the
   cluster, and later stages (smoke) never run.
4. Teardown completeness: when one Cluster delete fails, the target must
   STILL attempt deleting the other (best-effort both) and then exit
   nonzero.
5. Resource floor: the target's header comment documents a CPU floor and a
   RAM floor with concrete numbers (asserted as a Makefile text check, the
   same style as the committed-manifest text pins in test_capi_assets.py).

Second-cluster facts (P4): topology-only manifest ``capi/cluster-lab2.yaml``,
Cluster name ``k8labs-2``, same ClusterClass as ``capi/cluster.yaml``. The
manifest itself is the IMPLEMENTER'S deliverable; these tests never read it
(the fake kubectl intercepts every apply), so they stay red for the right
reason (missing target behavior, not a missing file).

Kubeconfig endpoints use ports 20001/20002 -- inside the documented default
PublishPort allocator range 20000-21000 (REQ-010), which is where real
allocations will land.

Which PATHS the two fetched kubeconfigs are written to is an implementation
choice; the contract pins DISTINCTION (two different paths) plus CONTENT
(one carries the :20001 server URL, the other :20002). The tests therefore
derive the two paths from the smoke-test applications' ``--kubeconfig``
arguments instead of hardcoding names.

Whether the implementer wires prereq/mgmt-up into the composite is also free
(the listed stages do not include them, but delegating to existing targets
is reasonable). Following the TASK-017 composite-test convention, the fake
environment seeds EVERYTHING such delegation may touch -- podman, kvm-group
identity, baked-artifact placeholders, and a healthy provider readyz
endpoint on the contractual 127.0.0.1:9440 -- so the tests judge the
multi-cluster stages, not environment gaps ("environment seeding only").

Everything runs WITHOUT a live KVM host: ``systemctl``, ``kubectl`` and
``sudo`` are PATH-injected fakes that log their argv; the make subprocess
gets an isolated HOME plus a restricted PATH. The whole ``build/`` directory
is renamed aside for each test and restored afterwards, so the two fetched
kubeconfigs can never leak into (or collide with) real build state.

RED PHASE: the ``multi-cluster-test`` target does not exist yet, so every
behavioral test below fails on missing evidence (``No rule to make target
'multi-cluster-test'``) and the documentation test fails on the missing
target header. None of them can pass vacuously: each negative-contract test
additionally requires logged proof that the target attempted its work.
"""

from __future__ import annotations

import base64
import http.server
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = REPO_ROOT / "build"

# Locked contract constants (P4: second cluster is topology-only, k8labs-2).
CLUSTER_NAME = "k8labs"
CLUSTER2_NAME = "k8labs-2"

# Server URLs the two fake Secrets decode to. Ports sit inside the default
# PublishPort allocator range 20000-21000 (REQ-010).
PORT_CLUSTER1 = 20001
PORT_CLUSTER2 = 20002
SERVER_URL_CLUSTER1 = f"https://127.0.0.1:{PORT_CLUSTER1}"
SERVER_URL_CLUSTER2 = f"https://127.0.0.1:{PORT_CLUSTER2}"

CAPISHIM_KUBECONFIG_SUFFIX = ".kube/capishim.kubeconfig"
QUADLET_DIR_REL = Path(".config") / "containers" / "systemd"
QUADLET_UNITS = (
    ("capishim.pod", "capishim"),
    ("k8netd.container", "k8netd"),
    ("cluster-api-hypervisor.container", "cluster-api-hypervisor"),
)

BASE_IMAGE_RELPATH = Path("build") / "k8labs-base.qcow2"
FIRMWARE_RELPATH = Path("build") / "CLOUDHV.fd"

_IDENTITY_GROUPS_READY = ("kvm", "wheel", "users")

# Tools the composite may legitimately reach through prereq/mgmt-up
# delegation (TASK-017 environment-seeding convention). curl is required on
# the host because the readyz gate probes with it.
BASE_TOOLS = (
    "cloud-hypervisor",
    "openssl",
    "clusterctl",
    "jq",
    "python3",
    "systemctl",
    "kubectl",
    "sudo",
    "podman",
)
FETCH_TOOLS_REQUIRED = ("curl",)

MAKE_TIMEOUT = 120
SMOKE_TIMEOUT = 300

MAKE_BIN = shutil.which("make")
assert MAKE_BIN is not None, "GNU make must be on the test runner's PATH"

COREUTILS = (
    "sh",
    "bash",
    "env",
    "cat",
    "sed",
    "grep",
    "egrep",
    "awk",
    "gawk",
    "base64",
    "chmod",
    "mkdir",
    "rmdir",
    "rm",
    "cp",
    "mv",
    "ln",
    "sleep",
    "date",
    "seq",
    "find",
    "dirname",
    "basename",
    "tr",
    "cut",
    "paste",
    "sort",
    "head",
    "tail",
    "tee",
    "touch",
    "uname",
    "id",
    "readlink",
    "realpath",
    "mktemp",
    "od",
    "printf",
    "test",
    "[",
    "true",
    "false",
    "echo",
    "ls",
    "pwd",
    "xargs",
    "stat",
    "wc",
    "comm",
    "uniq",
    "pgrep",
    "ps",
    "kill",
    "install",
    "sha256sum",
    "which",
    "make",
)

FAKE_SYSTEMCTL = """\
#!/bin/sh
# Fake systemctl: logs argv, succeeds unconditionally (TASK-018 needs no
# systemd failure modes; mgmt-up delegation only has to get past startup).
printf 'systemctl %s\\n' "$*" >> "$KFAKE_LOG"
# capishim-setup oneshot Result probe (mgmt-up setup gate): report the
# converged oneshot these tests assume (environment seeding only).
case "$*" in
  *Result*capishim-setup.service*)
    printf 'success\\n'
    ;;
esac
exit 0
"""

FAKE_SUDO = """\
#!/bin/sh
# Fake sudo: NEVER escalates; logs and succeeds so no test can touch the
# real system even if a recipe ever invokes it.
printf 'sudo %s\\n' "$*" >> "$KFAKE_LOG"
exit 0
"""

FAKE_KUBECTL = """\
#!/bin/sh
# Fake kubectl (TASK-018 multicluster): logs argv, behavior driven by
# KFAKE_KUBECTL_MODE. Serves PER-CLUSTER kubeconfig Secrets so the two
# clusters can be told apart, and simulates three failure modes:
#   identical-server-urls : BOTH Secrets decode to the SAME server URL
#                           (multi-cluster port-collision detector input)
#   second-never-ready    : readiness wait mentioning the second cluster
#                           times out (mid-stage abort input)
#   delete-lab2-fails     : deleting the SECOND Cluster errors (best-effort
#                           teardown input)
printf 'kubectl %s\\n' "$*" >> "$KFAKE_LOG"
args="$*"
lower=$(printf '%s' "$args" | tr '[:upper:]' '[:lower:]')
mode="${KFAKE_KUBECTL_MODE:-ok}"

# Management-plane readiness probe (get namespaces against capishim kc).
case "$lower" in
  *get*namespaces*)
    exit 0
    ;;
esac

# Workload kubeconfig Secret fetch: payload selected by Secret name.
case "$lower" in
  *secret*)
    payload=""
    name=""
    case "$args" in
      *"${KFAKE_CLUSTER2_NAME}"-kubeconfig*)
        payload="${KFAKE_SECRET2_B64}"
        name="${KFAKE_CLUSTER2_NAME}-kubeconfig"
        ;;
      *"${KFAKE_CLUSTER_NAME}"-kubeconfig*)
        payload="${KFAKE_SECRET1_B64}"
        name="${KFAKE_CLUSTER_NAME}-kubeconfig"
        ;;
    esac
    case "$args" in
      *jsonpath*)
        printf '%s' "$payload"
        ;;
      *)
        printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"%s","namespace":"default"},"data":{"value":"%s"}}\\n' "$name" "$payload"
        ;;
    esac
    exit 0
    ;;
esac

# Smoke-test Job apply and completion wait (against either kubeconfig).
case "$lower" in
  *smoke-test/job.yaml*)
    exit 0
    ;;
esac

# Cluster manifest applies (capi/cluster.yaml AND capi/cluster-lab2.yaml).
case "$lower" in
  *apply*cluster*.yaml*)
    exit 0
    ;;
esac

# Cluster waits. Reclamation waits (--for=delete) always succeed; readiness
# waits fail for the second cluster in second-never-ready mode.
case "$lower" in
  *wait*cluster*)
    case "$lower" in
      *delete*)
        exit 0
        ;;
    esac
    case "$mode" in
      second-never-ready)
        case "$args" in
          *"-${KFAKE_CLUSTER2_NAME}"*)
            echo "error: timed out waiting for the condition on cluster.cluster.x-k8s.io/${KFAKE_CLUSTER2_NAME}" >&2
            exit 1
            ;;
        esac
        ;;
    esac
    exit 0
    ;;
esac

# Cluster deletes (cluster-down style).
case "$lower" in
  *delete*cluster*)
    case "$mode" in
      delete-lab2-fails)
        case "$args" in
          *"-${KFAKE_CLUSTER2_NAME}"*)
            echo 'Error: Unable to connect to the server: connection refused' >&2
            exit 1
            ;;
        esac
        ;;
    esac
    exit 0
    ;;
esac

exit 0
"""


def _workload_kubeconfig_payload(server_url: str, cluster: str) -> str:
    """A minimal but realistic kubeconfig carrying one server URL."""
    return (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        f"- name: {cluster}\n"
        "  cluster:\n"
        f"    server: {server_url}\n"
        "contexts:\n"
        f"- name: {cluster}\n"
        "  context:\n"
        f"    cluster: {cluster}\n"
        f"current-context: {cluster}\n"
        "users:\n"
        "- name: admin\n"
    )


PAYLOAD_CLUSTER1 = _workload_kubeconfig_payload(SERVER_URL_CLUSTER1, CLUSTER_NAME)
PAYLOAD_CLUSTER2 = _workload_kubeconfig_payload(SERVER_URL_CLUSTER2, CLUSTER2_NAME)
SECRET1_B64 = base64.b64encode(PAYLOAD_CLUSTER1.encode()).decode()
SECRET2_B64 = base64.b64encode(PAYLOAD_CLUSTER2.encode()).decode()


def _write_executable(path: Path, content: str) -> None:
    # Replace, never write through: path may be a symlink to a real host
    # binary picked up by the tools loop, and the fake must overwrite the
    # link itself, not its target.
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@contextmanager
def _preserve(path: Path) -> Iterator[None]:
    """Move ``path`` aside for the test, then restore it exactly."""
    backup = path.with_name(f"{path.name}.pytest-backup")
    moved = False
    if path.exists():
        if backup.exists():
            backup.unlink()
        path.rename(backup)
        moved = True
    try:
        yield
    finally:
        if path.exists():
            path.unlink()
        if moved:
            backup.rename(path)


@contextmanager
def _preserve_build_dir() -> Iterator[None]:
    """Hide the ENTIRE build/ tree so fetched kubeconfigs stay hermetic.

    Rename-based: O(1) regardless of artifact size (base images are large).
    The make recipes recreate build/ themselves via mkdir -p.
    """
    backup = REPO_ROOT / "build.task018-backup"
    existed = BUILD_DIR.exists()
    if existed:
        if backup.exists():
            shutil.rmtree(backup)
        BUILD_DIR.rename(backup)
    try:
        yield
    finally:
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
        if existed:
            backup.rename(BUILD_DIR)


@pytest.fixture(autouse=True)
def _hermetic_build_dir() -> Iterator[None]:
    """Never let multicluster runs read or leak real build/ state."""
    with _preserve_build_dir():
        yield


@contextmanager
def _placeholder(path: Path) -> Iterator[None]:
    """Ensure ``path`` exists during the block; restore prior state after."""
    with _preserve(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"k8labs test placeholder\n")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


@dataclass
class FakeEnv:
    """Isolated HOME + fake-bin environment for one make invocation family."""

    home: Path
    bin: Path
    coreutils: Path
    log: Path
    state: Path
    quadlet_dir: Path = field(init=False)
    capishim_kubeconfig: Path = field(init=False)

    def __post_init__(self) -> None:
        self.quadlet_dir = self.home / QUADLET_DIR_REL
        self.capishim_kubeconfig = self.home / CAPISHIM_KUBECONFIG_SUFFIX

    def install_quadlets(self) -> None:
        self.quadlet_dir.mkdir(parents=True, exist_ok=True)
        for fname, _fragment in QUADLET_UNITS:
            (self.quadlet_dir / fname).write_text(
                f"# {fname}\n[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )

    def install_capishim_kubeconfig(self) -> None:
        self.capishim_kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        self.capishim_kubeconfig.write_text(
            "apiVersion: v1\nkind: Config\nclusters:\n- capishim-mgmt\n",
            encoding="utf-8",
        )

    def env(
        self,
        *,
        kubectl_mode: str = "ok",
        systemctl_mode: str = "ok",
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{self.coreutils}"
        env["HOME"] = str(self.home)
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        env["XDG_DATA_HOME"] = str(self.home / ".local" / "share")
        env["KFAKE_LOG"] = str(self.log)
        env["KFAKE_STATE_DIR"] = str(self.state)
        env["KFAKE_KUBECTL_MODE"] = kubectl_mode
        env["KFAKE_SYSTEMCTL_MODE"] = systemctl_mode
        env["KFAKE_CLUSTER_NAME"] = CLUSTER_NAME
        env["KFAKE_CLUSTER2_NAME"] = CLUSTER2_NAME
        env["KFAKE_SECRET1_B64"] = SECRET1_B64
        # Collision mode: BOTH Secrets serve cluster 1's payload, so the two
        # fetched kubeconfigs carry identical server URLs.
        if kubectl_mode == "identical-server-urls":
            env["KFAKE_SECRET2_B64"] = SECRET1_B64
        else:
            env["KFAKE_SECRET2_B64"] = SECRET2_B64
        env["CLUSTER_NAME"] = CLUSTER_NAME
        for var in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKE_TERMOUT", "MAKE_TERMERR"):
            env.pop(var, None)
        env.pop("KUBECONFIG", None)
        return env

    def run_make(
        self,
        *targets: str,
        kubectl_mode: str = "ok",
        systemctl_mode: str = "ok",
        timeout: int = MAKE_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        make_bin = MAKE_BIN
        assert make_bin is not None
        return subprocess.run(
            [make_bin, *targets],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=self.env(kubectl_mode=kubectl_mode, systemctl_mode=systemctl_mode),
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def calls_of(self, tool: str) -> list[str]:
        return [line for line in self.calls() if line.startswith(f"{tool} ")]

    @staticmethod
    def kubeconfig_arg(line: str) -> str | None:
        match = re.search(r"--kubeconfig(?:=|\s+)(\S+)", line)
        return match.group(1) if match else None


def _build_fake_env(tmp_path: Path, *, tools: tuple[str, ...] = BASE_TOOLS) -> FakeEnv:
    """Create the fake bin dir, coreutils whitelist, and isolated HOME."""
    bin_dir = tmp_path / "bin"
    coreutils_dir = tmp_path / "coreutils"
    bin_dir.mkdir()
    coreutils_dir.mkdir()

    for name in COREUTILS:
        src = shutil.which(name)
        if src is not None:
            (coreutils_dir / name).symlink_to(src)

    for name in tools:
        dest = bin_dir / name
        src = shutil.which(name)
        if src is not None:
            dest.symlink_to(src)
        else:
            _write_executable(dest, "#!/bin/sh\nexit 0\n")

    _write_executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    _write_executable(bin_dir / "sudo", FAKE_SUDO)

    env = FakeEnv(
        home=tmp_path / "home",
        bin=bin_dir,
        coreutils=coreutils_dir,
        log=tmp_path / "calls.log",
        state=tmp_path / "state",
    )
    env.log.touch()
    env.state.mkdir()
    return env


def _fake_identity_script(groups: tuple[str, ...]) -> str:
    joined = " ".join(groups)
    return f"""#!/bin/sh
# Fake id/groups: deterministic group membership for prereq checks.
for arg in "$@"; do
  case "$arg" in
    *G*) printf '%s\\n' "{joined}"; exit 0 ;;
    *u*) printf '%s\\n' "labuser"; exit 0 ;;
  esac
done
printf 'uid=1000(labuser) gid=1000(labuser) groups=1000(labuser),{joined}\\n'
"""


def _install_identity(env: FakeEnv, groups: tuple[str, ...]) -> None:
    script = _fake_identity_script(groups)
    _write_executable(env.bin / "id", script)
    _write_executable(
        env.bin / "groups", f"#!/bin/sh\nprintf '%s\\n' \"{' '.join(groups)}\"\n"
    )


def _mc_env(tmp_path: Path) -> FakeEnv:
    """Fully seeded environment for multi-cluster-test runs.

    TASK-018 (environment seeding only, TASK-017 convention): everything the
    composite may delegate to (prereq's podman/kvm-group/artifact gates,
    mgmt-up's readyz probe) is satisfied, so the tests judge ONLY the
    multi-cluster stage contract.
    """
    missing = [tool for tool in FETCH_TOOLS_REQUIRED if shutil.which(tool) is None]
    assert not missing, (
        f"host must provide {FETCH_TOOLS_REQUIRED} for the readyz gate "
        f"delegation path; missing: {missing}"
    )
    tools = BASE_TOOLS + tuple(FETCH_TOOLS_REQUIRED)
    if shutil.which("wget") is not None:
        tools += ("wget",)
    env = _build_fake_env(tmp_path, tools=tools)
    _install_identity(env, _IDENTITY_GROUPS_READY)
    env.install_quadlets()
    env.install_capishim_kubeconfig()
    return env


@contextmanager
def _mc_runtime() -> Iterator[None]:
    """Seed baked-artifact placeholders for prereq delegation."""
    with (
        _placeholder(REPO_ROOT / BASE_IMAGE_RELPATH),
        _placeholder(REPO_ROOT / FIRMWARE_RELPATH),
    ):
        yield


# --- Provider readyz endpoint (TASK-017 convention, environment seeding) -----

READYZ_HOST = "127.0.0.1"
READYZ_PORT = 9440


class _ReadyzHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class _ReadyzHandler(http.server.BaseHTTPRequestHandler):
    """Answers GET /readyz with 200 ok; 404 for anything else."""

    def do_GET(self) -> None:
        body = b"ok\n" if self.path == "/readyz" else b"not found\n"
        status = 200 if self.path == "/readyz" else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _readyz_server() -> Iterator[None]:
    """Own a healthy readyz endpoint at the contractual 9440 address."""
    try:
        httpd = _ReadyzHTTPServer((READYZ_HOST, READYZ_PORT), _ReadyzHandler)
    except OSError as exc:
        raise AssertionError(
            f"test could not bind {READYZ_HOST}:{READYZ_PORT} for the readyz "
            f"endpoint ({exc}); the port must be free to exercise the gate"
        ) from exc
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --- Log-classification helpers ------------------------------------------------


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _resolve_repo_path(arg: str) -> Path:
    path = Path(arg)
    return path if path.is_absolute() else REPO_ROOT / path


def _uses_capishim_kubeconfig(env: FakeEnv, line: str) -> bool:
    kc = env.kubeconfig_arg(line)
    return kc is not None and kc.endswith(CAPISHIM_KUBECONFIG_SUFFIX)


def _is_cluster_apply(line: str, manifest_token: str) -> bool:
    return line.startswith("kubectl") and "apply" in line and manifest_token in line


def _is_secret_fetch(line: str, cluster: str) -> bool:
    return (
        line.startswith("kubectl")
        and "secret" in line
        and f"{cluster}-kubeconfig" in line
    )


def _is_ready_wait(line: str, cluster: str) -> bool:
    """A Cluster-readiness wait/poll line naming ``cluster`` (not delete)."""
    lowered = line.lower()
    if cluster not in lowered or "delete" in lowered:
        return False
    if "wait" in lowered and "cluster" in lowered:
        return True
    return re.search(r"\bget\s+cluster\b", lowered) is not None


def _is_delete_call(line: str, cluster: str) -> bool:
    # (^| )delete( |$) refuses '--for=delete', separating real delete calls
    # from reclamation waits.
    return (
        line.startswith("kubectl")
        and re.search(r"(^| )delete( |$)", line) is not None
        and cluster in line
    )


def _is_reclaim_wait(line: str, cluster: str) -> bool:
    lowered = line.lower()
    return "wait" in lowered and "delete" in lowered and cluster in lowered


def _is_smoke_apply(line: str) -> bool:
    return (
        line.startswith("kubectl") and "apply" in line and "smoke-test/job.yaml" in line
    )


def _is_smoke_apply_via(line: str, path: Path) -> bool:
    if not _is_smoke_apply(line):
        return False
    kc = FakeEnv.kubeconfig_arg(line)
    return kc is not None and _resolve_repo_path(kc) == path


def _scan_ordered(lines: list[str], groups: list[tuple[str, list]]) -> dict[str, int]:
    """Walk ``groups`` monotonically through ``lines``.

    The cursor is NON-DECREASING (starts at the previous match, not after
    it): one log line may legitimately satisfy several groups, e.g. a single
    `kubectl apply -f capi/cluster.yaml -f capi/cluster-lab2.yaml` or a
    single wait naming both Clusters.
    """
    cursor = 0
    spans: dict[str, int] = {}
    for label, preds in groups:
        idx = -1
        for i in range(cursor, len(lines)):
            if any(pred(lines[i]) for pred in preds):
                idx = i
                break
        if idx == -1:
            raise AssertionError(
                f"stage '{label}' not found at/after log line {cursor} "
                f"(multi-cluster-test stages must run in contract order); "
                f"kubectl call log:\n" + "\n".join(lines)
            )
        spans[label] = idx
        cursor = idx
    return spans


def _run_multicluster(
    env: FakeEnv, *, kubectl_mode: str = "ok"
) -> subprocess.CompletedProcess[str]:
    with _mc_runtime(), _readyz_server():
        return env.run_make(
            "multi-cluster-test", kubectl_mode=kubectl_mode, timeout=SMOKE_TIMEOUT
        )


# --- REQ-013 / VC-05: full two-cluster lifecycle in order ---------------------


def test_multi_cluster_test_runs_all_stages_in_order(tmp_path: Path) -> None:
    """VC-05 happy path: apply both -> wait both -> fetch two -> smoke both
    -> delete both -> reclaim both, strictly in that order, exit 0.

    The two fetched kubeconfigs must land on two DISTINCT paths and carry
    DISTINCT server ports (:20001 vs :20002 here). Paths are derived from
    the smoke applications' --kubeconfig arguments, so the implementation
    stays free to name them.
    """
    env = _mc_env(tmp_path)

    result = _run_multicluster(env)

    assert result.returncode == 0, f"multi-cluster-test failed:\n{_combined(result)}"
    lines = env.calls()

    # Exactly two DISTINCT workload kubeconfig paths reached the smoke stage.
    smoke_lines = [line for line in lines if _is_smoke_apply(line)]
    smoke_paths: list[Path] = []
    for line in smoke_lines:
        kc = env.kubeconfig_arg(line)
        assert kc is not None, f"smoke apply without --kubeconfig: {line}"
        resolved = _resolve_repo_path(kc)
        if resolved not in smoke_paths:
            smoke_paths.append(resolved)
    assert len(smoke_paths) == 2, (
        "smoke must run against exactly two DISTINCT fetched kubeconfig "
        f"paths; got {[str(p) for p in smoke_paths]} from:\n" + "\n".join(smoke_lines)
    )

    # Full ordered stage walk (non-decreasing cursor tolerates one log line
    # serving two groups, e.g. a dual-manifest apply or both-name wait).
    _scan_ordered(
        lines,
        [
            (
                "server-side apply of capi/cluster.yaml",
                [lambda l: _is_cluster_apply(l, "capi/cluster.yaml")],
            ),
            (
                "server-side apply of capi/cluster-lab2.yaml",
                [lambda l: _is_cluster_apply(l, "capi/cluster-lab2.yaml")],
            ),
            (
                f"readiness wait for Cluster {CLUSTER_NAME}",
                [lambda l: _is_ready_wait(l, CLUSTER_NAME)],
            ),
            (
                f"readiness wait for Cluster {CLUSTER2_NAME}",
                [lambda l: _is_ready_wait(l, CLUSTER2_NAME)],
            ),
            (
                f"Secret fetch {CLUSTER_NAME}-kubeconfig",
                [lambda l: _is_secret_fetch(l, CLUSTER_NAME)],
            ),
            (
                f"Secret fetch {CLUSTER2_NAME}-kubeconfig",
                [lambda l: _is_secret_fetch(l, CLUSTER2_NAME)],
            ),
            (
                f"smoke-test via {smoke_paths[0].name}",
                [lambda l, p=smoke_paths[0]: _is_smoke_apply_via(l, p)],
            ),
            (
                f"smoke-test via {smoke_paths[1].name}",
                [lambda l, p=smoke_paths[1]: _is_smoke_apply_via(l, p)],
            ),
            (
                f"delete Cluster {CLUSTER_NAME}",
                [lambda l: _is_delete_call(l, CLUSTER_NAME)],
            ),
            (
                f"delete Cluster {CLUSTER2_NAME}",
                [lambda l: _is_delete_call(l, CLUSTER2_NAME)],
            ),
            (
                f"reclamation wait for {CLUSTER_NAME}",
                [lambda l: _is_reclaim_wait(l, CLUSTER_NAME)],
            ),
            (
                f"reclamation wait for {CLUSTER2_NAME}",
                [lambda l: _is_reclaim_wait(l, CLUSTER2_NAME)],
            ),
        ],
    )

    # Applies: server-side, against the capishim management kubeconfig.
    for manifest in ("capi/cluster.yaml", "capi/cluster-lab2.yaml"):
        applies = [line for line in lines if _is_cluster_apply(line, manifest)]
        assert any("--server-side" in line for line in applies), (
            f"{manifest} must be applied server-side:\n" + "\n".join(applies)
        )
        for line in applies:
            assert _uses_capishim_kubeconfig(env, line), (
                f"{manifest} apply must target the capishim kubeconfig: {line}"
            )

    # Secret fetches and deletes go against the capishim kubeconfig too.
    for cluster in (CLUSTER_NAME, CLUSTER2_NAME):
        for line in [l for l in lines if _is_secret_fetch(l, cluster)]:
            assert _uses_capishim_kubeconfig(env, line), (
                f"{cluster}-kubeconfig fetch must target the capishim "
                f"kubeconfig: {line}"
            )
        for line in [l for l in lines if _is_delete_call(l, cluster)]:
            assert _uses_capishim_kubeconfig(env, line), (
                f"{cluster} delete must target the capishim kubeconfig: {line}"
            )

    # The two written kubeconfigs carry the two DISTINCT server URLs.
    servers: set[str] = set()
    for path in smoke_paths:
        assert path.is_file(), f"fetched workload kubeconfig missing at {path}"
        text = path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^\s*server:\s*(\S+)", text)
        assert match is not None, f"{path}: no 'server:' entry found in:\n{text[:400]}"
        servers.add(match.group(1))
    assert servers == {SERVER_URL_CLUSTER1, SERVER_URL_CLUSTER2}, (
        f"the two fetched kubeconfigs must carry DISTINCT server URLs "
        f"{sorted({SERVER_URL_CLUSTER1, SERVER_URL_CLUSTER2})}; got {sorted(servers)}"
    )


# --- REQ-013 / VC-05: collision detector --------------------------------------


def test_multi_cluster_test_fails_when_both_kubeconfigs_share_one_server_url(
    tmp_path: Path,
) -> None:
    """Identical server URLs in the two fetched kubeconfigs -> nonzero FAIL
    with a message naming the URL(s).

    Collision-mode semantics: both Secrets serve cluster 1's payload, so the
    two URLs are the SAME string; naming "both" is observable as the shared
    URL appearing in the failure output. Evidence guards keep this from
    passing vacuously: BOTH Secrets must have been fetched before the
    failure.
    """
    env = _mc_env(tmp_path)

    result = _run_multicluster(env, kubectl_mode="identical-server-urls")

    combined = _combined(result)
    assert result.returncode != 0, (
        "multi-cluster-test must FAIL when the two fetched kubeconfigs "
        f"carry identical server URLs (port collision); output:\n{combined}"
    )
    lines = env.calls()
    for cluster in (CLUSTER_NAME, CLUSTER2_NAME):
        assert any(_is_secret_fetch(line, cluster) for line in lines), (
            f"collision detection requires fetching BOTH Secrets; no fetch "
            f"for {cluster}-kubeconfig:\n{lines}"
        )
    assert SERVER_URL_CLUSTER1 in combined, (
        f"failure message must name the colliding server URL "
        f"({SERVER_URL_CLUSTER1}):\n{combined}"
    )


# --- REQ-013 / VC-05: mid-stage failure propagation ----------------------------


def test_multi_cluster_test_aborts_when_second_cluster_never_ready(
    tmp_path: Path,
) -> None:
    """Second Cluster never Ready -> nonzero abort naming stage and cluster;
    later stages (smoke) never run."""
    env = _mc_env(tmp_path)

    result = _run_multicluster(env, kubectl_mode="second-never-ready")

    combined = _combined(result)
    lines = env.calls()
    assert result.returncode != 0, (
        "multi-cluster-test must abort when the second Cluster never "
        f"becomes Ready; output:\n{combined}"
    )
    assert any(_is_ready_wait(line, CLUSTER2_NAME) for line in lines), (
        f"the failure must come from an attempted readiness wait for "
        f"{CLUSTER2_NAME}, not an unrelated error; calls were:\n{lines}"
    )
    assert CLUSTER2_NAME in combined, (
        f"failure output must name the stuck cluster ({CLUSTER2_NAME}):\n" + combined
    )
    assert re.search(r"ready|wait", combined, re.IGNORECASE) is not None, (
        f"failure output must identify the readiness stage:\n{combined}"
    )
    assert not any(_is_smoke_apply(line) for line in lines), (
        "stages after the failed readiness gate (smoke-test) must not run; "
        f"calls were:\n{lines}"
    )


# --- REQ-013 / VC-05: best-effort teardown completeness ------------------------


def test_multi_cluster_test_deletes_both_clusters_when_one_delete_fails(
    tmp_path: Path,
) -> None:
    """One Cluster delete failing must not skip the sibling delete:
    best-effort BOTH, then exit nonzero naming the failure."""
    env = _mc_env(tmp_path)

    result = _run_multicluster(env, kubectl_mode="delete-lab2-fails")

    combined = _combined(result)
    lines = env.calls()
    assert result.returncode != 0, (
        "multi-cluster-test must exit nonzero when a Cluster delete fails; "
        f"output:\n{combined}"
    )
    for cluster in (CLUSTER_NAME, CLUSTER2_NAME):
        assert any(_is_delete_call(line, cluster) for line in lines), (
            f"teardown must be best-effort: a failing delete of one cluster "
            f"must not skip deleting {cluster}; calls were:\n{lines}"
        )
    assert CLUSTER2_NAME in combined, (
        f"failure output must name the cluster whose delete failed "
        f"({CLUSTER2_NAME}):\n{combined}"
    )


# --- REQ-013: resource-floor documentation (Makefile text pin) -----------------


def test_multi_cluster_test_header_documents_resource_floor() -> None:
    """The target's header comment documents a CPU floor and a RAM floor.

    Text-check style (same approach as the committed-manifest text pins in
    test_capi_assets.py): read the Makefile, isolate the contiguous comment
    block above the multi-cluster-test rule (.PHONY lines and blank lines
    are skipped, anything else ends the header), and require CPU and RAM
    floor documentation WITH concrete numbers -- a floor without a number
    is not a floor.
    """
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"(?m)^multi-cluster-test\s*:", text)
    assert match is not None, (
        "Makefile must define the multi-cluster-test target "
        "(REQ-013 two-cluster automated verification)"
    )

    header_lines: list[str] = []
    for line in reversed(text[: match.start()].splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            header_lines.append(stripped)
        elif stripped.startswith(".PHONY") or stripped == "":
            continue
        else:
            break
    header = "\n".join(header_lines)
    assert header.strip(), (
        "multi-cluster-test must carry a header comment documenting the "
        "resource floor (REQ-013)"
    )

    assert re.search(r"(?i)\bcpu", header) is not None, (
        "target header must document a CPU floor; header was:\n" + header
    )
    assert re.search(r"(?i)(\bram\b|memory|\bgib\b|\bgb\b)", header) is not None, (
        f"target header must document a RAM/memory floor; header was:\n{header}"
    )
    assert re.search(r"\d", header) is not None, (
        "resource floor must carry concrete numbers (e.g. '8 CPUs, 16 GiB "
        f"RAM'); header was:\n{header}"
    )
