"""Tests for the ``make tofu-init`` target and the OpenTofu provider cache.

``make cluster`` (Makefile:519-521) runs ``tofu -chdir=terraform apply`` and
then ``make configure`` (Makefile:496-501), which runs
``tofu -chdir=terraform/runtime apply``. Neither step runs ``tofu init``, so
the runtime module fails with "Required plugins are not installed" whenever
``terraform/runtime/.terraform`` is missing (the cache is gitignored). These
tests pin the contract for the ``make tofu-init`` target that initializes
both modules before any apply step.

Tests that download providers from registry.opentofu.org are marked
``network`` so they can be deselected with ``pytest -m "not network"``. They
fail loudly (with the init stderr) instead of skipping when the registry is
unreachable.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
RUNTIME_DIR = REPO_ROOT / "terraform" / "runtime"
RUNTIME_CACHE = RUNTIME_DIR / ".terraform"
RUNTIME_PROVIDERS = RUNTIME_CACHE / "providers" / "registry.opentofu.org" / "hashicorp"
LOCK_FILES = (
    REPO_ROOT / "terraform" / ".terraform.lock.hcl",
    RUNTIME_DIR / ".terraform.lock.hcl",
)

# Exact versions pinned in terraform/runtime/.terraform.lock.hcl (source of
# truth for what init must install).
RUNTIME_PROVIDER_VERSIONS = {
    "null": "3.3.0",
    "random": "3.9.0",
    "tls": "4.3.0",
    "local": "2.9.0",
}

INIT_TIMEOUT = 600  # provider downloads from the registry need headroom
DRY_RUN_TIMEOUT = 60


def _run(
    cmd: list[str],
    *,
    cwd: Path = REPO_ROOT,
    timeout: int = INIT_TIMEOUT,
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
    *args: str, timeout: int = INIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    return _run(["make", *args], timeout=timeout)


def _run_tofu(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return _run(["tofu", *args], cwd=cwd)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _makefile_prereqs(target: str) -> list[str]:
    """Return the whitespace-separated prerequisites of a Makefile rule."""
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{target}:"):
            body = line.split(":", 1)[1]
            body = body.split("##", 1)[0]
            return [token for token in body.split()]
    return []


@contextmanager
def _preserve(path: Path) -> Iterator[None]:
    """Move ``path`` aside for the duration of a fresh-state test.

    Only directories that existed before the test are restored. A cache
    created by the test itself is left in place (it is gitignored and the
    build needs it), so rerunning the suite stays cheap and the workspace
    ends up more initialized than it started.
    """
    backup = path.with_name(f"{path.name}.pytest-backup")
    moved = False
    if path.exists():
        if backup.exists():
            shutil.rmtree(backup)
        path.rename(backup)
        moved = True
    try:
        yield
    finally:
        if moved:
            if path.exists():
                shutil.rmtree(path)
            backup.rename(path)


# --- REQ-1: `make tofu-init` target initializes both modules ---------------


def test_tofu_init_target_inits_both_modules() -> None:
    """REQ-1: target exists and initializes terraform/ and terraform/runtime/."""
    result = _run_make("-n", "tofu-init", timeout=DRY_RUN_TIMEOUT)

    assert result.returncode == 0, (
        "`make tofu-init` is not a target or fails before running; "
        f"stderr:\n{result.stderr}"
    )
    assert "tofu -chdir=terraform init" in result.stdout, (
        "tofu-init must initialize the terraform/ module"
    )
    assert "tofu -chdir=terraform/runtime init" in result.stdout, (
        "tofu-init must initialize the terraform/runtime/ module"
    )
    assert "-input=false" in result.stdout, (
        "init must be non-interactive (-input=false semantics)"
    )


# --- REQ-2: fresh runtime module gets providers at locked versions ---------


@pytest.mark.network
def test_tofu_init_fresh_state_installs_pinned_runtime_providers() -> None:
    """REQ-2: cache-less runtime module gets null/random/tls/local at pins."""
    with _preserve(RUNTIME_CACHE):
        result = _run_make("tofu-init")

        assert result.returncode == 0, (
            "`make tofu-init` failed on a fresh runtime module; "
            f"stderr:\n{result.stderr}"
        )
        for provider, version in RUNTIME_PROVIDER_VERSIONS.items():
            version_dir = RUNTIME_PROVIDERS / provider / version
            assert version_dir.is_dir(), (
                f"provider {provider} {version} missing after init; expected "
                f"directory {version_dir} under registry.opentofu.org/hashicorp"
            )


# --- REQ-3: idempotent reruns ----------------------------------------------


@pytest.mark.network
def test_tofu_init_idempotent_on_initialized_modules() -> None:
    """REQ-3: a second run over initialized modules exits 0 with no errors."""
    first = _run_make("tofu-init")
    assert first.returncode == 0, f"first init failed; stderr:\n{first.stderr}"

    second = _run_make("tofu-init")
    assert second.returncode == 0, f"second init failed; stderr:\n{second.stderr}"
    combined = second.stdout + second.stderr
    assert "Error" not in combined, (
        "second init reported an error on an initialized module:\n" + combined
    )
    assert "Enter a value" not in combined, (
        "second init prompted for input despite -input=false semantics:\n" + combined
    )


# --- REQ-4: lock files are never rewritten ---------------------------------


@pytest.mark.network
def test_tofu_init_preserves_committed_lock_files() -> None:
    """REQ-4: init must not modify the committed lock files (no -upgrade)."""
    before = {path: _sha256(path) for path in LOCK_FILES}

    result = _run_make("tofu-init")
    assert result.returncode == 0, f"`make tofu-init` failed; stderr:\n{result.stderr}"

    for path, checksum in before.items():
        assert _sha256(path) == checksum, (
            f"tofu-init modified {path.relative_to(REPO_ROOT)}; init must not "
            "drift the locked provider versions"
        )


# --- REQ-5: configure never applies with missing plugins -------------------


def test_configure_target_prereqs_include_tofu_init() -> None:
    """REQ-5: `configure` depends on tofu-init before its runtime apply."""
    prereqs = _makefile_prereqs("configure")

    assert "tofu-init" in prereqs, (
        f"`configure` prerequisites are {prereqs}; it must depend on "
        "tofu-init so `tofu -chdir=terraform/runtime apply` never hits "
        "missing plugins"
    )


@pytest.mark.network
def test_runtime_init_succeeds_on_cacheless_module() -> None:
    """REQ-5: direct runtime init succeeds where `make configure` failed."""
    with _preserve(RUNTIME_CACHE):
        result = _run_tofu("-chdir=terraform/runtime", "init", "-input=false")

        assert result.returncode == 0, (
            "runtime module init failed on a fresh cache; registry "
            f"unreachable or lock mismatch. stderr:\n{result.stderr}"
        )
        assert RUNTIME_CACHE.is_dir(), (
            "runtime init reported success but created no .terraform cache"
        )
