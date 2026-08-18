"""Tests for the ``make vm-disks`` stale-disk invalidation and ``make redeploy`` wiring.

``vm-disks`` (Makefile:319-356) is ``.PHONY``: it converts the base image
into per-VM root disks and, on the current code, skips any disk that already
exists (``[ ! -f $$disk ]``). A base-image rebuild therefore never
invalidates stale per-VM disks (the 257MB kubernetes-cp.raw incident the
approved plan targets). The plan (TASK-001/TASK-002/TASK-003) changes the
skip to recreate when ``[ ! -f "$$disk" ] || [ "$$BASE" -nt "$$disk" ]``,
adds a running-VM guard that fails with a "make destroy" message when
recreation is needed while cloud-hypervisor processes exist, and adds a
``redeploy`` convenience target (= destroy + cluster).

The tests below pin those behaviors. They run against the CURRENT code, so
the tests that pin NEW behavior fail now (red phase) and pass after the
engineer lands the Makefile changes. All ``vm-disks`` runs use tiny fake
qcow2 artifacts (``qemu-img create -f qcow2 1M``; resize is metadata-only)
and preserve/restore the real ``build/`` artifacts around every run, so the
working tree is untouched and the suite stays fast, offline, and free of
any live-cluster dependency.

Two tests force the recreate path (stale-recreate and missing-disk happy
path). They skip only when BOTH the guard is already implemented (``pgrep``
appears in the Makefile) AND a real cloud-hypervisor VM is running on this
host: in that state the guard intentionally refuses to recreate disks, so
the test cannot observe recreation without destroying live VMs. On current
HEAD (no guard) they always run - and fail - which is the red phase.

All paths used are whitespace-free (repo convention); pytest's ``tmp_path``
sits under the user's pytest temp root which has no spaces.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
BUILD_DIR = REPO_ROOT / "build"
VDISKS_DIR = BUILD_DIR / "vm-disks"
BASE_IMAGE = BUILD_DIR / "k8labs-base.qcow2"
TFVARS = BUILD_DIR / "deploy.tfvars"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TFVARS_FIXTURE = FIXTURES / "single_worker.tfvars"

# (node name, requested disk size in MiB) pairs the fixture drives.
EXPECTED_DISKS = (("cp1", 20480), ("w1", 40960))

MAKE_TIMEOUT = 120
DRY_RUN_TIMEOUT = 60


def _run(
    cmd: list[str],
    *,
    cwd: Path = REPO_ROOT,
    timeout: int = MAKE_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` non-interactively (stdin is /dev/null, so no prompts)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _run_make(
    *args: str, timeout: int = MAKE_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return _run(["make", *args], timeout=timeout)


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def _set_mtime_ns(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _makefile_has_guard() -> bool:
    """True once the Makefile carries the running-VM guard (TASK-002).

    The planned guard detects processes via ``pgrep -f cloud-hypervisor``;
    ``pgrep`` is absent from the Makefile today, so this is a reliable
    red/green signal for the skip logic below.
    """
    return "pgrep" in MAKEFILE.read_text(encoding="utf-8")


def _cloud_hypervisor_running() -> bool:
    """True when any process whose command line contains 'cloud-hypervisor' exists."""
    result = subprocess.run(
        ["pgrep", "-f", "cloud-hypervisor"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


# Recreate-path tests skip only when the guard exists AND a real VM runs:
# the guard intentionally blocks recreation then, so the test could not
# observe recreation without destroying live VMs.
_RECREATE_BLOCKED = _makefile_has_guard() and _cloud_hypervisor_running()


@contextmanager
def _preserve(path: Path) -> Iterator[None]:
    """Move ``path`` aside for a test, then restore it exactly.

    Files and directories created by the test at a path that did not exist
    before are removed again on exit, so a fake base image never lingers in
    ``build/`` for real ``make`` runs.
    """
    backup = path.with_name(f"{path.name}.pytest-backup")
    moved = False
    if path.exists():
        if backup.exists():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        path.rename(backup)
        moved = True
    try:
        yield
    finally:
        if path.exists():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        if moved:
            backup.rename(path)


@contextmanager
def _vm_disks_env() -> Iterator[None]:
    """Install a fake base image + fixture tfvars and restore build/ after.

    The real build artifacts (vm-disks dir, base image, deploy.tfvars) are
    moved aside for the duration and restored exactly on exit.
    """
    with (
        _preserve(VDISKS_DIR),
        _preserve(BASE_IMAGE),
        _preserve(TFVARS),
    ):
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        _run(
            ["qemu-img", "create", "-f", "qcow2", str(BASE_IMAGE), "1M"],
            timeout=30,
        )
        shutil.copyfile(TFVARS_FIXTURE, TFVARS)
        yield


def _spawn_cloud_hypervisor_marker(tmp_path: Path) -> subprocess.Popen[bytes]:
    """Spawn a process whose command line contains 'cloud-hypervisor'."""
    script = tmp_path / "cloud-hypervisor-marker"
    script.write_text(
        "#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile :; do sleep 30; done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return subprocess.Popen(
        [str(script), "--api-socket=test"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_marker(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _snapshot_build_state() -> dict[str, object]:
    """Record existence + mtime of the build artifacts the vm-disks tests touch.

    Per-VM disk files inside ``build/vm-disks`` are live when the cluster
    runs (cloud-hypervisor writes to them continuously), so only their
    names are pinned - not their mtimes. The base image, tfvars file, and
    the vm-disks directory itself are only ever touched by the tests, so
    those are compared exactly.
    """
    snap: dict[str, object] = {}
    for path in (VDISKS_DIR, BASE_IMAGE, TFVARS):
        rel = str(path.relative_to(REPO_ROOT))
        if not path.exists():
            snap[rel] = None
            continue
        if path.is_dir():
            snap[rel] = {
                "mtime_ns": _mtime_ns(path),
                "entries": sorted(child.name for child in path.iterdir()),
            }
        else:
            snap[rel] = {"mtime_ns": _mtime_ns(path), "size": path.stat().st_size}
    return snap


_BUILD_STATE_BEFORE = _snapshot_build_state()


# --- VC-TD1-RECREATE: stale disk is recreated --------------------------------


@pytest.mark.skipif(
    _RECREATE_BLOCKED,
    reason="running-VM guard implemented and cloud-hypervisor VMs are live; "
    "recreation is intentionally blocked",
)
def test_vm_disks_stale_base_recreates_disk() -> None:
    """VC-TD1-RECREATE: base newer than disk -> vm-disks recreates the disk."""
    with _vm_disks_env():
        first = _run_make("vm-disks")
        assert first.returncode == 0, f"first vm-disks run failed:\n{first.stderr}"
        disk = VDISKS_DIR / "cp1-root.qcow2"
        assert disk.is_file(), "first run must create cp1-root.qcow2"
        original_mtime = _mtime_ns(disk)

        _set_mtime_ns(BASE_IMAGE, original_mtime + 5_000_000_000)

        second = _run_make("vm-disks")
        assert second.returncode == 0, f"second vm-disks run failed:\n{second.stderr}"
        assert _mtime_ns(disk) != original_mtime, (
            "stale disk was not recreated: base image is newer than "
            "cp1-root.qcow2 but make vm-disks left it in place (current code "
            f"skips existing disks). output:\n{second.stdout}{second.stderr}"
        )


# --- VC-TD1-FRESH: fresh disk is skipped -------------------------------------


def test_vm_disks_fresh_disk_is_skipped() -> None:
    """VC-TD1-FRESH: disk newer than base -> 'already exists', mtime unchanged."""
    with _vm_disks_env():
        first = _run_make("vm-disks")
        assert first.returncode == 0, f"first vm-disks run failed:\n{first.stderr}"
        disk = VDISKS_DIR / "cp1-root.qcow2"
        assert disk.is_file(), "first run must create cp1-root.qcow2"
        fresh_mtime = _mtime_ns(disk)

        _set_mtime_ns(BASE_IMAGE, fresh_mtime - 5_000_000_000)

        second = _run_make("vm-disks")
        assert second.returncode == 0, f"second vm-disks run failed:\n{second.stderr}"
        combined = second.stdout + second.stderr
        assert "already exists" in combined, (
            "a disk newer than the base must be skipped, not recreated:\n" + combined
        )
        assert _mtime_ns(disk) == fresh_mtime, (
            "skipped disk must not be touched; its mtime changed"
        )


# --- VC-TD1-GUARD: running-VM guard ------------------------------------------


def test_vm_disks_guard_fails_with_make_destroy_guidance(tmp_path: Path) -> None:
    """VC-TD1-GUARD: recreation needed + cloud-hypervisor -> fail with guidance."""
    with _vm_disks_env():
        first = _run_make("vm-disks")
        assert first.returncode == 0, f"first vm-disks run failed:\n{first.stderr}"
        disk = VDISKS_DIR / "cp1-root.qcow2"
        _set_mtime_ns(BASE_IMAGE, _mtime_ns(disk) + 5_000_000_000)

        marker = _spawn_cloud_hypervisor_marker(tmp_path)
        try:
            time.sleep(0.2)
            assert _cloud_hypervisor_running(), (
                "test harness broken: fake cloud-hypervisor process is not "
                "visible to pgrep -f cloud-hypervisor"
            )
            result = _run_make("vm-disks")
        finally:
            _kill_marker(marker)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "make vm-disks must refuse to recreate disks while a "
        "cloud-hypervisor process is running; it exited 0 (no running-VM "
        f"guard yet?). output:\n{combined}"
    )
    assert "make destroy" in combined, (
        "guard failure must tell the user to run `make destroy` first:\n" + combined
    )


# --- VC-TD1-SKIP-MISSING-BASE: happy path preserved ---------------------------


@pytest.mark.skipif(
    _RECREATE_BLOCKED,
    reason="running-VM guard implemented and cloud-hypervisor VMs are live; "
    "disk creation is guarded",
)
def test_vm_disks_creates_missing_disks_when_base_exists() -> None:
    """VC-TD1-SKIP-MISSING-BASE: base exists, no disk yet -> disks are created."""
    with _vm_disks_env():
        result = _run_make("vm-disks")
        assert result.returncode == 0, f"vm-disks failed:\n{result.stderr}"
        combined = result.stdout + result.stderr
        for node, _size_mib in EXPECTED_DISKS:
            disk = VDISKS_DIR / f"{node}-root.qcow2"
            assert disk.is_file(), f"{node} root disk was not created"
        assert "Creating" in combined, "expected 'Creating' lines in output"
        info = _run(
            ["qemu-img", "info", str(VDISKS_DIR / "cp1-root.qcow2")],
            timeout=30,
        )
        assert "20 GiB" in info.stdout, (
            "cp1 disk must be resized to its tfvars size (20480 MiB):\n" + info.stdout
        )


# --- VC-TD2-REDEPLOY-WIRING: make redeploy dry run ----------------------------


def test_redeploy_wiring_dry_run_shows_destroy_and_cluster() -> None:
    """VC-TD2-REDEPLOY-WIRING: `make -n redeploy` prints destroy + cluster."""
    result = _run_make("-n", "redeploy", timeout=DRY_RUN_TIMEOUT)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        "`make -n redeploy` must succeed once the target exists; current "
        f"Makefile has no redeploy target:\n{combined}"
    )
    assert "destroy" in combined, (
        "redeploy dry run must invoke the destroy target:\n" + combined
    )
    assert "cluster" in combined, (
        "redeploy dry run must invoke the cluster target:\n" + combined
    )


# --- edge case: build/ state is restored -------------------------------------


def test_vm_disks_leaves_build_artifacts_untouched() -> None:
    """Edge: after the vm-disks tests, build/ artifacts are exactly as before."""
    after = _snapshot_build_state()
    assert after == _BUILD_STATE_BEFORE, (
        "vm-disks tests must restore build/vm-disks, build/k8labs-base.qcow2 "
        "and build/deploy.tfvars exactly:\n"
        f"before: {_BUILD_STATE_BEFORE}\nafter: {after}"
    )
