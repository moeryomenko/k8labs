"""Tests for the CAPI-migration Makefile surface (TASK-001, test-first).

The terraform/network pipeline is being replaced by a Cluster API pipeline
driven by a rootless capishim management plane. This module pins the NEW
target contracts before implementation (red phase): every test here must
fail against the current Makefile, because the targets either do not exist
yet (mgmt-up, mgmt-down, cluster-up, cluster-down) or do not implement the
CAPI behavior (prereq, kubeconfig, smoke-test, cluster).

Contracts under test (Verification Contract summary):

- ``make prereq``      extended checks: quadlet units for capishim-pod,
                       k8netd and the provider manager under
                       ``~/.config/containers/systemd/``, ``clusterctl`` and
                       ``kubectl`` on PATH, and the capishim kubeconfig at
                       ``~/.kube/capishim.kubeconfig``; keeps the surviving
                       legacy tool checks (cloud-hypervisor, jq, python3
                       venv). Failures must name what is missing.
- ``make mgmt-up``     ``systemctl --user start`` of the three units, then
                       wait until ``kubectl --kubeconfig <capishim kc> get
                       namespaces`` responds. Idempotent on re-run.
- ``make mgmt-down``   ``systemctl --user stop`` of the same units; never
                       deletes management-plane state.
- ``make cluster-up``  server-side apply of ``capi/cluster.yaml`` against the
                       capishim kubeconfig; re-apply is idempotent.
- ``make kubeconfig``  fetch Secret ``<cluster>-kubeconfig``, decode the
                       ``value`` data key, write ``build/kubeconfig``; clear
                       failure when the Secret is absent.
- ``make smoke-test``  apply ``capi/smoke-test/job.yaml`` against the
                       workload kubeconfig (``build/kubeconfig``) and wait
                       for Job completion; Job failure must fail the target.
- ``make cluster-down`` delete the Cluster against the capishim kubeconfig
                       and wait for reclamation.
- ``make cluster``     composite ordering: prereq -> mgmt-up -> cluster-up ->
                       wait Cluster ready -> kubeconfig -> smoke-test.

Cluster name: no cluster name exists in terraform/ today, so the locked
contract constant is ``CLUSTER_NAME ?= k8labs``. Tests pass
``CLUSTER_NAME=k8labs`` explicitly so they hold whether the Makefile makes
it overridable or hardcodes it.

Everything runs WITHOUT a live KVM host: ``systemctl``, ``kubectl`` and
``sudo`` are PATH-injected fakes that log their argv, and the make
subprocess gets an isolated ``HOME`` plus a restricted PATH (fake bin dir +
a whitelisted coreutils dir). The restricted PATH deliberately omits tofu,
uv, packer and friends, so red-phase runs of today's heavyweight targets
abort within milliseconds instead of reaching real infrastructure, and the
faked ``sudo`` makes privilege escalation impossible even if a recipe ever
gets that far.

Two prereq regression guards (missing cloud-hypervisor, missing jq) pin
behavior the CURRENT prereq already implements; they legitimately pass
today and are documented as such. Every other test fails today, including
the negative-contract tests, which additionally require logged evidence
that the target attempted its work (so a missing target cannot satisfy
them vacuously).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = REPO_ROOT / "build"
WORKLOAD_KUBECONFIG = BUILD_DIR / "kubeconfig"

# Locked contract constants (see module docstring).
CLUSTER_NAME = "k8labs"
CAPISHIM_KUBECONFIG_SUFFIX = ".kube/capishim.kubeconfig"
QUADLET_DIR_REL = Path(".config") / "containers" / "systemd"
# (quadlet file name, distinctive fragment the failure message must contain)
QUADLET_UNITS = (
    ("capishim.pod", "capishim"),
    ("k8netd.container", "k8netd"),
    ("cluster-api-hypervisor.container", "cluster-api-hypervisor"),
)
MGMT_UNIT_FRAGMENTS = ("capishim-pod", "k8netd", "cluster-api-hypervisor")

# Tools whose presence varies per test; everything else in the fake
# environment is fixed. cloud-hypervisor/jq/python3 are the surviving legacy
# checks; openssl stays because cert material handling survives the
# migration; clusterctl/kubectl are the new CAPI tools. systemctl/kubectl/
# sudo request the PATH-injected fakes (see _build_fake_env): a test that
# leaves one out genuinely runs without it.
DEFAULT_TOOLS = (
    "cloud-hypervisor",
    "openssl",
    "clusterctl",
    "jq",
    "python3",
    # Fakes every non-prereq target relies on.
    "systemctl",
    "kubectl",
    "sudo",
)

# Minimal workload kubeconfig payload the fake Secret decodes to.
WORKLOAD_PAYLOAD = "apiVersion: v1\nkind: Config\nclusters:\n- k8labs-workload\n"
SECRET_B64 = base64.b64encode(WORKLOAD_PAYLOAD.encode()).decode()

MAKE_TIMEOUT = 120
SMOKE_TIMEOUT = 300

MAKE_BIN = shutil.which("make")
assert MAKE_BIN is not None, "GNU make must be on the test runner's PATH"

# Coreutils the Makefile recipes may legitimately need under the restricted
# PATH. Anything not listed here (tofu, uv, packer, nft, real systemctl,
# real kubectl, ...) is invisible to the make subprocess, which both isolates
# the fakes and makes red-phase runs of today's targets fail fast and safe.
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
# Fake systemctl: logs argv, behavior driven by KFAKE_SYSTEMCTL_MODE.
printf 'systemctl %s\\n' "$*" >> "$KFAKE_LOG"
case "${KFAKE_SYSTEMCTL_MODE:-ok}" in
  fail-start)
    case "$*" in
      *start*)
        echo "Failed to start units: Unit not found: $*" >&2
        exit 1
        ;;
    esac
    ;;
  fail-stop)
    case "$*" in
      *stop*)
        echo "Job for unit failed: stop dep failed" >&2
        exit 1
        ;;
    esac
    ;;
esac
exit 0
"""

FAKE_SUDO = """\
#!/bin/sh
# Fake sudo: NEVER escalates; logs and succeeds so no test can touch the
# real system even if a (current or future) recipe invokes it.
printf 'sudo %s\\n' "$*" >> "$KFAKE_LOG"
exit 0
"""

FAKE_KUBECTL = """\
#!/bin/sh
# Fake kubectl: logs argv, behavior driven by KFAKE_KUBECTL_MODE.
printf 'kubectl %s\\n' "$*" >> "$KFAKE_LOG"
args="$*"
mode="${KFAKE_KUBECTL_MODE:-ok}"

# Management-plane readiness probe (get namespaces against capishim kc).
if printf '%s' "$args" | grep -q ' namespaces'; then
  case "$mode" in
    ready-after-2)
      f="${KFAKE_STATE_DIR}/namespaces-probe-count"
      n=$(cat "$f" 2>/dev/null || echo 0)
      n=$((n + 1))
      echo "$n" > "$f"
      if [ "$n" -le 2 ]; then
        echo 'The connection to the server 127.0.0.1:6443 was refused' >&2
        exit 1
      fi
      ;;
    never-ready)
      echo 'The connection to the server 127.0.0.1:6443 was refused' >&2
      exit 1
      ;;
  esac
  exit 0
fi

# Workload kubeconfig Secret fetch.
if printf '%s' "$args" | grep -q 'secret'; then
  case "$mode" in
    secret-absent)
      echo "Error from server (NotFound): secrets \\"${CLUSTER_NAME}-kubeconfig\\" not found" >&2
      exit 1
      ;;
  esac
  case "$args" in
    *jsonpath*)
      printf '%s' "${KFAKE_SECRET_B64}"
      ;;
    *)
      printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"'${CLUSTER_NAME}'-kubeconfig"},"data":{"value":"%s"}}\\n' "${KFAKE_SECRET_B64}"
      ;;
  esac
  exit 0
fi

# Smoke-test Job apply.
if printf '%s' "$args" | grep -q 'smoke-test/job.yaml'; then
  case "$mode" in
    apply-job-fails)
      echo 'error: unable to recognize "capi/smoke-test/job.yaml"' >&2
      exit 1
      ;;
  esac
  exit 0
fi

# Smoke-test Job completion wait.
if printf '%s' "$args" | grep -Eq 'wait .*(complete|success)'; then
  case "$mode" in
    job-wait-fails)
      echo 'error: timed out waiting for the condition on jobs/smoke-test' >&2
      exit 1
      ;;
  esac
  exit 0
fi

# Cluster manifest apply (server-side).
if printf '%s' "$args" | grep -q 'cluster.yaml'; then
  case "$mode" in
    apply-cluster-fails)
      echo 'Error: Unable to connect to the server: connection refused' >&2
      exit 1
      ;;
  esac
  exit 0
fi

# Cluster delete (cluster-down).
if printf '%s' "$args" | grep -Eq '(^| )delete( |$)' && \\
   printf '%s' "$args" | grep -q 'cluster'; then
  case "$mode" in
    delete-cluster-fails)
      echo 'Error: Unable to connect to the server: connection refused' >&2
      exit 1
      ;;
  esac
  exit 0
fi

exit 0
"""


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


@pytest.fixture(autouse=True)
def _preserve_workload_kubeconfig() -> Iterator[None]:
    """Never let the tests leak a workload kubeconfig into the real build/."""
    with _preserve(WORKLOAD_KUBECONFIG):
        yield


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

    # -- environment construction ------------------------------------------

    def install_quadlets(self) -> None:
        self.quadlet_dir.mkdir(parents=True, exist_ok=True)
        for fname, _fragment in QUADLET_UNITS:
            (self.quadlet_dir / fname).write_text(
                f"# {fname}\n[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )

    def remove_quadlet(self, fname: str) -> None:
        (self.quadlet_dir / fname).unlink()

    def install_capishim_kubeconfig(self) -> None:
        self.capishim_kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        self.capishim_kubeconfig.write_text(
            "apiVersion: v1\nkind: Config\nclusters:\n- capishim-mgmt\n",
            encoding="utf-8",
        )

    def install_mgmt_state(self) -> Path:
        """Create management-plane state that mgmt-down must never delete."""
        state_dir = self.home / ".local" / "share" / "capishim"
        (state_dir / "etcd").mkdir(parents=True, exist_ok=True)
        (state_dir / "kubeconfigs").mkdir(parents=True, exist_ok=True)
        (state_dir / "etcd" / "member.dat").write_text("state", encoding="utf-8")
        (state_dir / "kubeconfigs" / "admin.kubeconfig").write_text(
            "apiVersion: v1\nkind: Config\n", encoding="utf-8"
        )
        return state_dir

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
        env["KFAKE_SECRET_B64"] = SECRET_B64
        env["CLUSTER_NAME"] = CLUSTER_NAME
        # Do not leak an outer make's jobserver/flags into the inner make.
        for var in (
            "MAKEFLAGS",
            "MFLAGS",
            "MAKELEVEL",
            "MAKE_TERMOUT",
            "MAKE_TERMERR",
        ):
            env.pop(var, None)
        # A host-exported KUBECONFIG could redirect kubectl; the fakes do not
        # care, but drop it so the contract stays about explicit flags.
        env.pop("KUBECONFIG", None)
        return env

    # -- invocation ---------------------------------------------------------

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

    # -- log inspection ------------------------------------------------------

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def calls_of(self, tool: str) -> list[str]:
        return [line for line in self.calls() if line.startswith(f"{tool} ")]

    @staticmethod
    def kubeconfig_arg(line: str) -> str | None:
        match = re.search(r"--kubeconfig(?:=|\s+)(\S+)", line)
        return match.group(1) if match else None


def _build_fake_env(
    tmp_path: Path, *, tools: tuple[str, ...] = DEFAULT_TOOLS
) -> FakeEnv:
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
            # Tool not installed on the host: a silent success shim satisfies
            # `command -v` style checks without exercising the real binary.
            _write_executable(dest, "#!/bin/sh\nexit 0\n")

    # Fakes win over the tool symlinks/shims (written last). Each fake is
    # installed only when its tool is part of the requested set, so a test
    # that omits e.g. kubectl genuinely omits it from the fake PATH.
    if "systemctl" in tools:
        _write_executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    if "kubectl" in tools:
        _write_executable(bin_dir / "kubectl", FAKE_KUBECTL)
    if "sudo" in tools:
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


@pytest.fixture
def fake_env(tmp_path: Path) -> FakeEnv:
    """Fully provisioned CAPI environment: quadlets, kubeconfig, tools."""
    env = _build_fake_env(tmp_path)
    env.install_quadlets()
    env.install_capishim_kubeconfig()
    return env


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _first_index(lines: list[str], predicate) -> int:
    for idx, line in enumerate(lines):
        if predicate(line):
            return idx
    return -1


def _is_readiness_wait(line: str) -> bool:
    """A Cluster-ready wait/poll line (between apply and Secret fetch)."""
    lowered = line.lower()
    if "wait" in lowered and "cluster" in lowered:
        return True
    return re.search(r"\bget\s+cluster\b", lowered) is not None


def _is_reclamation_wait(line: str) -> bool:
    """A post-delete reclamation wait/poll line."""
    lowered = line.lower()
    if "wait" in lowered and "delete" in lowered:
        return True
    if "wait" in lowered and "cluster" in lowered:
        return True
    return re.search(r"\bget\s+cluster\b", lowered) is not None


# --- VC-prereq: extended checks with actionable messages --------------------


def test_prereq_passes_with_capi_tooling_present(fake_env: FakeEnv) -> None:
    """VC-prereq: full tooling + quadlets + capishim kubeconfig -> exit 0.

    The fake PATH intentionally omits tofu/nft: after the migration they
    must no longer be required.
    """
    result = fake_env.run_make("prereq")

    assert result.returncode == 0, (
        "prereq must pass when every CAPI-era requirement is satisfied "
        f"(without tofu/nft); stderr:\n{_combined(result)}"
    )


def test_prereq_fails_naming_missing_clusterctl(tmp_path: Path) -> None:
    """VC-prereq: clusterctl absent -> nonzero + message naming clusterctl."""
    env = _build_fake_env(
        tmp_path,
        tools=(
            "cloud-hypervisor",
            "openssl",
            "jq",
            "python3",
            "systemctl",
            "kubectl",
            "sudo",
        ),
    )
    env.install_quadlets()
    env.install_capishim_kubeconfig()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, "prereq must fail without clusterctl"
    assert "clusterctl" in combined, (
        f"failure output must name the missing tool 'clusterctl':\n{combined}"
    )


def test_prereq_fails_naming_missing_kubectl(tmp_path: Path) -> None:
    """VC-prereq: kubectl absent -> nonzero + message naming kubectl."""
    env = _build_fake_env(
        tmp_path, tools=("cloud-hypervisor", "openssl", "clusterctl", "jq", "python3")
    )
    env.install_quadlets()
    env.install_capishim_kubeconfig()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, "prereq must fail without kubectl"
    assert "kubectl" in combined, (
        f"failure output must name the missing tool 'kubectl':\n{combined}"
    )


@pytest.mark.parametrize(
    ("filename", "fragment"),
    QUADLET_UNITS,
    ids=[fname for fname, _ in QUADLET_UNITS],
)
def test_prereq_fails_naming_missing_quadlet_unit(
    tmp_path: Path, filename: str, fragment: str
) -> None:
    """VC-prereq: each missing quadlet unit file -> failure naming that unit."""
    env = _build_fake_env(tmp_path)
    env.install_quadlets()
    env.remove_quadlet(filename)
    env.install_capishim_kubeconfig()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, (
        f"prereq must fail when {filename} is missing from ~/{QUADLET_DIR_REL}"
    )
    assert fragment in combined, (
        f"failure output must name the missing unit ({fragment}) when "
        f"{filename} is absent:\n{combined}"
    )


def test_prereq_fails_naming_missing_capishim_kubeconfig(tmp_path: Path) -> None:
    """VC-prereq: no ~/.kube/capishim.kubeconfig -> failure naming the path."""
    env = _build_fake_env(tmp_path)
    env.install_quadlets()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, "prereq must fail without the capishim kubeconfig"
    assert "capishim.kubeconfig" in combined, (
        "failure output must name the missing capishim kubeconfig path:\n" + combined
    )


def test_prereq_surviving_check_cloud_hypervisor(tmp_path: Path) -> None:
    """VC-prereq: surviving legacy check - cloud-hypervisor still required.

    Documented exception: this negative-contract test legitimately PASSES
    against the current Makefile (the existing prereq already checks
    cloud-hypervisor); it guards that the migration keeps the check.
    """
    env = _build_fake_env(
        tmp_path,
        tools=(
            "openssl",
            "clusterctl",
            "jq",
            "python3",
            "systemctl",
            "kubectl",
            "sudo",
        ),
    )
    env.install_quadlets()
    env.install_capishim_kubeconfig()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, "prereq must fail without cloud-hypervisor"
    assert "cloud-hypervisor" in combined, (
        "failure output must name the missing tool 'cloud-hypervisor':\n" + combined
    )


def test_prereq_surviving_check_jq(tmp_path: Path) -> None:
    """VC-prereq: surviving legacy check - jq still required.

    Documented exception: legitimately PASSES today for the same reason as
    the cloud-hypervisor guard above.
    """
    env = _build_fake_env(
        tmp_path,
        tools=(
            "cloud-hypervisor",
            "openssl",
            "clusterctl",
            "python3",
            "systemctl",
            "kubectl",
            "sudo",
        ),
    )
    env.install_quadlets()
    env.install_capishim_kubeconfig()

    result = env.run_make("prereq")

    combined = _combined(result)
    assert result.returncode != 0, "prereq must fail without jq"
    assert "jq" in combined, (
        f"failure output must name the missing tool 'jq':\n{combined}"
    )


# --- VC-mgmt-up: start units, wait for plane readiness, idempotent ----------


def test_mgmt_up_starts_all_three_units(fake_env: FakeEnv) -> None:
    """VC-mgmt-up: systemctl --user start covers capishim-pod/k8netd/provider."""
    result = fake_env.run_make("mgmt-up")

    assert result.returncode == 0, f"mgmt-up failed:\n{_combined(result)}"
    starts = [
        line
        for line in fake_env.calls_of("systemctl")
        if "--user" in line and "start" in line
    ]
    assert starts, "mgmt-up must start units via `systemctl --user start`"
    joined = "\n".join(starts)
    for fragment in MGMT_UNIT_FRAGMENTS:
        assert fragment in joined, (
            f"mgmt-up must start the '{fragment}' unit; starts were:\n{joined}"
        )


def test_mgmt_up_is_idempotent_on_rerun(fake_env: FakeEnv) -> None:
    """VC-mgmt-up: a second mgmt-up over running units still exits 0."""
    first = fake_env.run_make("mgmt-up")
    assert first.returncode == 0, f"first mgmt-up failed:\n{_combined(first)}"

    second = fake_env.run_make("mgmt-up")

    assert second.returncode == 0, (
        f"mgmt-up must be idempotent on re-run; stderr:\n{_combined(second)}"
    )


def test_mgmt_up_waits_for_management_plane_readiness(fake_env: FakeEnv) -> None:
    """VC-mgmt-up: transient `get namespaces` failures are retried to success."""
    result = fake_env.run_make("mgmt-up", kubectl_mode="ready-after-2")

    assert result.returncode == 0, (
        "mgmt-up must retry until the management plane responds; stderr:\n"
        + _combined(result)
    )
    probes = [line for line in fake_env.calls_of("kubectl") if " namespaces" in line]
    assert len(probes) >= 3, (
        "expected at least three readiness probes (two failures + one "
        f"success); observed:\n{probes}"
    )
    for probe in probes:
        kc = fake_env.kubeconfig_arg(probe)
        assert kc is not None and kc.endswith(CAPISHIM_KUBECONFIG_SUFFIX), (
            f"readiness probes must use the capishim kubeconfig: {probe}"
        )


def test_mgmt_up_fails_when_units_missing(fake_env: FakeEnv) -> None:
    """VC-mgmt-up: systemctl start failure -> nonzero + actionable message."""
    result = fake_env.run_make("mgmt-up", systemctl_mode="fail-start")

    combined = _combined(result)
    assert result.returncode != 0, (
        "mgmt-up must fail when the systemd units cannot be started"
    )
    named = any(fragment in combined for fragment in MGMT_UNIT_FRAGMENTS) or (
        "systemctl" in combined
    )
    assert named, (
        "failure output must identify the failing unit or systemctl "
        f"invocation:\n{combined}"
    )


def test_mgmt_up_fails_when_plane_never_ready(fake_env: FakeEnv) -> None:
    """VC-mgmt-up: management plane never responds -> bounded failure."""
    result = fake_env.run_make("mgmt-up", kubectl_mode="never-ready")

    assert result.returncode != 0, (
        "mgmt-up must fail (after a bounded wait) when the management "
        "plane never becomes ready"
    )
    probes = [line for line in fake_env.calls_of("kubectl") if " namespaces" in line]
    assert len(probes) >= 2, (
        "mgmt-up must actually poll management-plane readiness (repeated "
        f"`get namespaces`) before giving up; observed {len(probes)} "
        f"probe(s):\n{fake_env.calls()}"
    )


# --- VC-mgmt-down: stop units, never wipe state ------------------------------


def test_mgmt_down_stops_all_three_units(fake_env: FakeEnv) -> None:
    """VC-mgmt-down: systemctl --user stop covers all three units."""
    result = fake_env.run_make("mgmt-down")

    assert result.returncode == 0, f"mgmt-down failed:\n{_combined(result)}"
    stops = [
        line
        for line in fake_env.calls_of("systemctl")
        if "--user" in line and "stop" in line
    ]
    assert stops, "mgmt-down must stop units via `systemctl --user stop`"
    joined = "\n".join(stops)
    for fragment in MGMT_UNIT_FRAGMENTS:
        assert fragment in joined, (
            f"mgmt-down must stop the '{fragment}' unit; stops were:\n{joined}"
        )


def test_mgmt_down_preserves_state_files(fake_env: FakeEnv) -> None:
    """VC-mgmt-down: stopping the plane leaves all management state intact."""
    state_dir = fake_env.install_mgmt_state()

    result = fake_env.run_make("mgmt-down")

    assert result.returncode == 0, f"mgmt-down failed:\n{_combined(result)}"
    assert state_dir.is_dir(), "mgmt-down deleted the capishim state directory"
    assert (state_dir / "etcd" / "member.dat").is_file(), "mgmt-down deleted etcd state"
    assert (state_dir / "kubeconfigs" / "admin.kubeconfig").is_file(), (
        "mgmt-down deleted stored kubeconfigs"
    )


def test_mgmt_down_reports_failure_and_preserves_state(fake_env: FakeEnv) -> None:
    """VC-mgmt-down: stop failure -> nonzero exit AND state still intact."""
    state_dir = fake_env.install_mgmt_state()

    result = fake_env.run_make("mgmt-down", systemctl_mode="fail-stop")

    combined = _combined(result)
    assert result.returncode != 0, "mgmt-down must surface systemctl stop failures"
    stops = [
        line
        for line in fake_env.calls_of("systemctl")
        if "--user" in line and "stop" in line
    ]
    assert stops, (
        "the failure must come from an attempted `systemctl --user stop`, "
        f"not an unrelated error; calls were:\n{fake_env.calls()}"
    )
    assert state_dir.is_dir() and (state_dir / "etcd" / "member.dat").is_file(), (
        "even on failure, mgmt-down must never delete management state:\n" + combined
    )


# --- VC-cluster-up: server-side apply, idempotent ----------------------------


def test_cluster_up_server_side_applies_cluster_manifest(fake_env: FakeEnv) -> None:
    """VC-cluster-up: server-side apply of capi/cluster.yaml vs capishim kc."""
    result = fake_env.run_make("cluster-up")

    assert result.returncode == 0, f"cluster-up failed:\n{_combined(result)}"
    applies = [
        line
        for line in fake_env.calls_of("kubectl")
        if "apply" in line and "cluster.yaml" in line
    ]
    assert applies, "cluster-up must kubectl apply capi/cluster.yaml"
    assert any("--server-side" in line for line in applies), (
        "cluster-up must use server-side apply:\n" + "\n".join(applies)
    )
    for line in applies:
        kc = fake_env.kubeconfig_arg(line)
        assert kc is not None and kc.endswith(CAPISHIM_KUBECONFIG_SUFFIX), (
            f"cluster-up must apply against the capishim kubeconfig: {line}"
        )


def test_cluster_up_reapply_is_idempotent(fake_env: FakeEnv) -> None:
    """VC-cluster-up: applying the same cluster manifest twice succeeds."""
    first = fake_env.run_make("cluster-up")
    assert first.returncode == 0, f"first cluster-up failed:\n{_combined(first)}"

    second = fake_env.run_make("cluster-up")

    assert second.returncode == 0, (
        f"re-apply must be idempotent; stderr:\n{_combined(second)}"
    )


def test_cluster_up_fails_when_management_plane_unreachable(fake_env: FakeEnv) -> None:
    """VC-cluster-up: apiserver unreachable -> nonzero + actionable message."""
    result = fake_env.run_make("cluster-up", kubectl_mode="apply-cluster-fails")

    combined = _combined(result)
    assert result.returncode != 0, (
        "cluster-up must fail when the management plane is unreachable"
    )
    assert (
        "refused" in combined.lower()
        or "unreachable" in combined.lower()
        or ("kubectl" in combined.lower())
    ), f"failure output must explain the connectivity problem:\n{combined}"


# --- VC-kubeconfig: fetch Secret, decode value, write build/kubeconfig -------


def test_kubeconfig_writes_decoded_workload_kubeconfig(fake_env: FakeEnv) -> None:
    """VC-kubeconfig: Secret data.value is base64-decoded into build/kubeconfig."""
    result = fake_env.run_make("kubeconfig")

    assert result.returncode == 0, f"kubeconfig failed:\n{_combined(result)}"
    fetches = [line for line in fake_env.calls_of("kubectl") if "secret" in line]
    assert fetches, "kubeconfig must fetch the workload Secret via kubectl"
    for line in fetches:
        kc = fake_env.kubeconfig_arg(line)
        assert kc is not None and kc.endswith(CAPISHIM_KUBECONFIG_SUFFIX), (
            f"Secret fetch must go against the capishim kubeconfig: {line}"
        )
    assert WORKLOAD_KUBECONFIG.is_file(), "kubeconfig must write build/kubeconfig"
    written = WORKLOAD_KUBECONFIG.read_text(encoding="utf-8")
    assert written.strip() == WORKLOAD_PAYLOAD.strip(), (
        "build/kubeconfig must contain the DECODED Secret data.value "
        f"payload; got:\n{written[:200]}"
    )


def test_kubeconfig_fails_when_secret_absent(fake_env: FakeEnv) -> None:
    """VC-kubeconfig: missing Secret -> clear failure naming the Secret."""
    result = fake_env.run_make("kubeconfig", kubectl_mode="secret-absent")

    combined = _combined(result)
    assert result.returncode != 0, "kubeconfig must fail when the Secret is absent"
    assert (
        f"{CLUSTER_NAME}-kubeconfig" in combined or "not found" in combined.lower()
    ), f"failure output must name the missing Secret clearly:\n{combined}"
    assert not WORKLOAD_KUBECONFIG.exists() or (
        WORKLOAD_KUBECONFIG.read_text(encoding="utf-8").strip() == ""
    ), "on failure, kubeconfig must not leave a bogus build/kubeconfig behind"


# --- VC-smoke-test: Job apply + completion wait + failure propagation -------


def test_smoke_test_applies_job_and_waits_for_completion(fake_env: FakeEnv) -> None:
    """VC-smoke-test: apply capi/smoke-test/job.yaml vs build/kubeconfig, wait."""
    result = fake_env.run_make("smoke-test", timeout=SMOKE_TIMEOUT)

    assert result.returncode == 0, f"smoke-test failed:\n{_combined(result)}"
    applies = [
        line
        for line in fake_env.calls_of("kubectl")
        if "apply" in line and "smoke-test/job.yaml" in line
    ]
    assert applies, "smoke-test must apply capi/smoke-test/job.yaml"
    for line in applies:
        kc = fake_env.kubeconfig_arg(line)
        assert kc is not None and kc.replace(REPO_ROOT.as_posix() + "/", "").lstrip(
            "./"
        ).endswith("build/kubeconfig"), (
            f"smoke-test must use the workload kubeconfig (build/kubeconfig): {line}"
        )
    waits = [
        line
        for line in fake_env.calls_of("kubectl")
        if "wait" in line and ("complete" in line or "success" in line)
    ]
    assert waits, "smoke-test must wait for the Job to complete"


def test_smoke_test_propagates_job_failure(fake_env: FakeEnv) -> None:
    """VC-smoke-test: a failing Job must fail the target."""
    result = fake_env.run_make(
        "smoke-test", kubectl_mode="job-wait-fails", timeout=SMOKE_TIMEOUT
    )

    assert result.returncode != 0, (
        "smoke-test must propagate Job failure as a nonzero exit"
    )
    applies = [
        line
        for line in fake_env.calls_of("kubectl")
        if "apply" in line and "smoke-test/job.yaml" in line
    ]
    assert applies, (
        "the Job failure must come from the CAPI smoke-test flow (apply "
        f"capi/smoke-test/job.yaml), not an unrelated error; calls were:\n"
        f"{fake_env.calls()}"
    )


# --- VC-cluster-down: delete + wait for reclamation --------------------------


def test_cluster_down_deletes_cluster_and_waits_for_reclamation(
    fake_env: FakeEnv,
) -> None:
    """VC-cluster-down: delete Cluster via capishim kc, then wait/poll."""
    result = fake_env.run_make("cluster-down")

    assert result.returncode == 0, f"cluster-down failed:\n{_combined(result)}"
    deletes = [
        line
        for line in fake_env.calls_of("kubectl")
        if "delete" in line and "cluster" in line
    ]
    assert deletes, "cluster-down must delete the Cluster object"
    for line in deletes:
        kc = fake_env.kubeconfig_arg(line)
        assert kc is not None and kc.endswith(CAPISHIM_KUBECONFIG_SUFFIX), (
            f"cluster-down must delete against the capishim kubeconfig: {line}"
        )
        assert CLUSTER_NAME in line, (
            f"cluster-down must delete the '{CLUSTER_NAME}' cluster: {line}"
        )
    lines = fake_env.calls()
    delete_idx = _first_index(
        lines, lambda l: "delete" in l and "cluster" in l and l.startswith("kubectl")
    )
    reclaim_idx = _first_index(lines[delete_idx + 1 :], _is_reclamation_wait)
    assert reclaim_idx != -1, (
        "cluster-down must wait for (or poll) reclamation after the delete; "
        f"kubectl calls were:\n{fake_env.calls_of('kubectl')}"
    )


def test_cluster_down_fails_when_delete_fails(fake_env: FakeEnv) -> None:
    """VC-cluster-down: delete failure -> nonzero exit."""
    result = fake_env.run_make("cluster-down", kubectl_mode="delete-cluster-fails")

    assert result.returncode != 0, (
        "cluster-down must fail when the Cluster cannot be deleted"
    )
    deletes = [
        line
        for line in fake_env.calls_of("kubectl")
        if "delete" in line and "cluster" in line
    ]
    assert deletes, (
        "the failure must come from an attempted Cluster delete, not an "
        f"unrelated error; calls were:\n{fake_env.calls()}"
    )


# --- VC-composite: make cluster ordering -------------------------------------


def test_cluster_composite_runs_stages_in_order(fake_env: FakeEnv) -> None:
    """VC-composite: prereq -> mgmt-up -> cluster-up -> ready-wait -> kc -> smoke."""
    result = fake_env.run_make("cluster", timeout=SMOKE_TIMEOUT)

    assert result.returncode == 0, f"make cluster failed:\n{_combined(result)}"
    lines = fake_env.calls()

    start_idx = _first_index(
        lines, lambda l: l.startswith("systemctl") and "start" in l
    )
    apply_idx = _first_index(
        lines, lambda l: l.startswith("kubectl") and "cluster.yaml" in l
    )
    ready_idx = _first_index(
        lines[apply_idx + 1 :] if apply_idx != -1 else [], _is_readiness_wait
    )
    if apply_idx != -1 and ready_idx != -1:
        ready_idx += apply_idx + 1
    secret_idx = _first_index(
        lines, lambda l: l.startswith("kubectl") and "secret" in l
    )
    job_idx = _first_index(
        lines, lambda l: l.startswith("kubectl") and "smoke-test/job.yaml" in l
    )

    ordering = (
        f"start={start_idx} apply={apply_idx} ready={ready_idx} "
        f"secret={secret_idx} job={job_idx}"
    )
    assert start_idx != -1, f"composite never started the management plane:\n{ordering}"
    assert apply_idx != -1, f"composite never applied capi/cluster.yaml:\n{ordering}"
    assert ready_idx != -1, (
        f"composite never waited for Cluster readiness between apply and "
        f"kubeconfig fetch:\n{ordering}"
    )
    assert secret_idx != -1, (
        f"composite never fetched the kubeconfig Secret:\n{ordering}"
    )
    assert job_idx != -1, f"composite never ran the smoke-test Job:\n{ordering}"
    assert start_idx < apply_idx < ready_idx < secret_idx < job_idx, (
        f"composite stages ran out of order ({ordering}); full log:\n"
        + "\n".join(lines)
    )


def test_cluster_composite_gates_on_prereq_failure(tmp_path: Path) -> None:
    """VC-composite: prereq failure aborts before the mgmt plane starts."""
    env = _build_fake_env(
        tmp_path,
        tools=(
            "cloud-hypervisor",
            "openssl",
            "clusterctl",
            "python3",
            "systemctl",
            "kubectl",
            "sudo",
        ),
    )  # jq removed -> prereq must fail
    env.install_quadlets()
    env.install_capishim_kubeconfig()

    result = env.run_make("cluster", timeout=SMOKE_TIMEOUT)

    combined = _combined(result)
    assert result.returncode != 0, "composite must fail when prereq fails"
    assert "jq" in combined, (
        f"composite failure must come from prereq naming 'jq':\n{combined}"
    )
    starts = [line for line in env.calls_of("systemctl") if "start" in line]
    assert not starts, (
        "composite must gate mgmt-up behind prereq; units were started "
        f"despite prereq failure:\n{starts}"
    )


def test_cluster_composite_propagates_smoke_stage_failure(fake_env: FakeEnv) -> None:
    """VC-composite: a failing smoke-test Job fails the whole composite."""
    result = fake_env.run_make(
        "cluster", kubectl_mode="job-wait-fails", timeout=SMOKE_TIMEOUT
    )

    assert result.returncode != 0, "composite must fail when the smoke-test Job fails"
    assert any("smoke-test/job.yaml" in line for line in fake_env.calls()), (
        "composite reached failure without ever applying the smoke-test Job; "
        f"log:\n{fake_env.calls()}"
    )
