"""Reproducible kernel build harness for the pinned k8labs kernel source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

PINNED_SOURCE_COMMIT = "2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64"
SOURCE_COMMIT = PINNED_SOURCE_COMMIT

FEDORA_VERSION = "44"
FEDORA_IMAGE = "registry.fedoraproject.org/fedora@sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011"
FEDORA_IMAGE_REF = FEDORA_IMAGE
FEDORA_IMAGE_DIGEST = (
    "sha256:10667e171df877cedd245b7065238143e09eaae7da4656d3e62eb7c094378011"
)
PODMAN_IMAGE = FEDORA_IMAGE

MOCK_BUILDROOT = "fedora-44-x86_64"
MOCK_CONFIG = MOCK_BUILDROOT
BUILDROOT = MOCK_BUILDROOT

KERNEL_ARK_REPO = "https://gitlab.com/cki-project/kernel-ark.git"
ARK_INFRA_REPO = KERNEL_ARK_REPO
# The packaging tree is pinned to an immutable commit, not the moving branch:
# ark-infra carries only the RPM spec/build scripts and is merged onto the
# pinned upstream source, so it must never advance underneath a release.
ARK_INFRA_BRANCH = "ark-infra"
ARK_INFRA_COMMIT = "858721d1e05149394e01ae4f5ce3202484c22850"
ARK_INFRA_REF = ARK_INFRA_COMMIT
PACKAGING = "kernel-ark-ark-infra"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FRAGMENT = PROJECT_ROOT / "configs" / "fedora-44-kernel.config-fragment"
CONFIG_FRAGMENT_PATH = CONFIG_FRAGMENT
FEDORA_CONFIG_FRAGMENT = CONFIG_FRAGMENT
KERNEL_CONFIG_FRAGMENT = CONFIG_FRAGMENT
CONFIG_FRAGMENT_CANDIDATES = (
    PROJECT_ROOT / "configs" / "fedora-44-kernel.config-fragment",
    PROJECT_ROOT / "configs" / "kernel.config-fragment",
    PROJECT_ROOT / "kernel.config-fragment",
    PROJECT_ROOT / "packer" / "kernel.config-fragment",
)

CLEAN_BUILDS_REQUIRED = 2
BUILDS_REQUIRED = CLEAN_BUILDS_REQUIRED
PAYLOAD_BUILDS_REQUIRED = CLEAN_BUILDS_REQUIRED
RELEASE_REQUIRES_HSM_SIGNATURE = True
HSM_SIGNATURE_REQUIRED = RELEASE_REQUIRES_HSM_SIGNATURE
OPERATOR_APPROVAL_REQUIRED = True

# Exact rpmbuild switches for the release build.
RPMBUILD_OPTS = (
    "--without debug",
    "--without doc",
    "--without headers",
    "--without cross_headers",
    "--without perf",
    "--without libperf",
    "--without tools",
    "--without ynl",
    "--without kmap",
    "--without kernel_abi_stablelists",
    "--without selftests",
)

# `--without debuginfo` makes the kernel spec strip every CONFIG_DEBUG_INFO*=y
# from the merged .config. That removes CONFIG_DEBUG_INFO_BTF and, because
# CONFIG_SCHED_CLASS_EXT depends on BPF_SYSCALL && BPF_JIT && DEBUG_INFO_BTF,
# it silently drops sched_ext too. A kernel built that way cannot run any of
# the scheduler arms, so the build must fail closed instead of producing it.
FORBIDDEN_RPMBUILD_OPTS = ("--without debuginfo",)

# Reproducibility. rpm 6 does not honour SOURCE_DATE_EPOCH for %{BUILDTIME}, so
# the build time is pinned through %_buildtime; the kABI stablelists tarball is
# created with plain tar, so tar metadata is normalised through TAR_OPTIONS.
# With these two settings two independent SRPM builds are byte-identical.
REPRODUCIBLE_BUILD_EPOCH = 1792368000
REPRODUCIBLE_BUILD_TIMESTAMP = "Mon Oct 19 00:00:00 UTC 2026"
KBUILD_BUILD_USER = "k8labs"
KBUILD_BUILD_HOST = "k8labs-build"
RPM_MACROS_LINE = f"%_buildtime {REPRODUCIBLE_BUILD_EPOCH}"
TAR_OPTIONS = (
    f"--mtime=@{REPRODUCIBLE_BUILD_EPOCH} --owner=0 --group=0 "
    "--numeric-owner --sort=name"
)


def reproducible_build_env() -> dict[str, str]:
    """Environment that makes the SRPM and RPM payloads reproducible.

    The kernel embeds its build timestamp and builder identity, so those must be
    fixed too, not just rpm's header time.
    """
    return {
        "SOURCE_DATE_EPOCH": str(REPRODUCIBLE_BUILD_EPOCH),
        "KBUILD_BUILD_TIMESTAMP": REPRODUCIBLE_BUILD_TIMESTAMP,
        "KBUILD_BUILD_USER": KBUILD_BUILD_USER,
        "KBUILD_BUILD_HOST": KBUILD_BUILD_HOST,
        "TAR_OPTIONS": TAR_OPTIONS,
    }


def reproducible_build_macros() -> str:
    """Content of ~/.rpmmacros required for a reproducible build."""
    return RPM_MACROS_LINE + "\n"

RUNTIME_RPMS = (
    "kernel-core",
    "kernel-modules",
    "kernel-modules-core",
    "kernel-modules-extra",
)
RUNTIME_RPM_NAMES = RUNTIME_RPMS
LFS_TRACKED_RPMS = RUNTIME_RPMS
LFS_TRACKED = RUNTIME_RPMS
TRACKED_RPMS = RUNTIME_RPMS

NON_LFS_ARTIFACTS = ("base-image-qcow2", "firmware", "debuginfo", "debugsource")
OUT_OF_LFS_ARTIFACTS = NON_LFS_ARTIFACTS

LOCK_FILE = PROJECT_ROOT / "kernel_build_lock.yaml"
LOCK_PATH = LOCK_FILE
ARTIFACT_LOCK = LOCK_FILE
INVENTORY_FILE = PROJECT_ROOT / "kernel_build_inventory.json"
INVENTORY_PATH = INVENTORY_FILE

PROVISIONER = PROJECT_ROOT / "packer" / "scripts" / "01-configure-system.sh"
PACKER_PROVISIONER = PROVISIONER

# Kernel version fields, read from the pinned source's top-level Makefile at
# PINNED_SOURCE_COMMIT (VERSION=7 PATCHLEVEL=3 SUBLEVEL=0 EXTRAVERSION=-rc7)
# and from the pinned ark-infra packaging revision.
#
# The kernel-ark build system defines the RPM release as
#   <upstream build><ark build><dist localversion><dist>
# and the values below reproduce exactly what `make dist-srpm` emits, verified
# against the produced SRPM:
#   kernel-7.3.0-0.0.next.20261016.64.k8.46c58e1.fc44.src.rpm
NVR_VERSION_BASE = "7.3.0"
# linux-next path: UPSTREAMBUILD = "0.0.next.<tag date>."
NVR_NEXT_TAG_DATE = "20261016"
NVR_UPSTREAM_BUILD = f"0.0.next.{NVR_NEXT_TAG_DATE}."
# ark-infra RHEL_RELEASE (Makefile.rhelver at the pinned ark-infra commit).
NVR_ARK_BUILD = "64"
# dist localversion prefix; the config-fragment hash follows it.
NVR_LOCALVERSION_PREFIX = ".k8."
NVR_LOCALVERSION_HASH_LEN = 7
NVR_RELEASE_PREFIX = NVR_UPSTREAM_BUILD

# Config options the campaign actually depends on. The merged .config must
# enable every one of these or the build is not usable for the study.
REQUIRED_CONFIG_OPTIONS = (
    "CONFIG_SCHED_CLASS_EXT",
    "CONFIG_BPF_SYSCALL",
    "CONFIG_BPF_JIT",
    "CONFIG_DEBUG_INFO_BTF",
    "CONFIG_CGROUPS",
    "CONFIG_CGROUP_SCHED",
    "CONFIG_CFS_BANDWIDTH",
    "CONFIG_EFI_STUB",
    "CONFIG_MODULE_SIG",
)


class LFSUnavailableError(RuntimeError):
    """Raised when Git LFS storage is unavailable."""


class HashMismatchError(ValueError):
    """Raised when an artifact hash does not match the lock."""


class NVR:
    def __init__(self, name: str, version: str, release: str) -> None:
        self.name = name
        self.version = version
        self.release = release

    def __str__(self) -> str:
        return f"{self.name}-{self.version}-{self.release}"


def _first_existing(paths: tuple[Path, ...]) -> Path:
    for path in paths:
        if path.is_file():
            return path
    return paths[0]


def resolve_config_fragment(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    return _first_existing(CONFIG_FRAGMENT_CANDIDATES)


def read_config_fragment(path: str | os.PathLike[str] | None = None) -> str:
    resolved = resolve_config_fragment(path)
    return resolved.read_text(encoding="utf-8")


def normalize_config_fragment(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def config_fragment_options(text: str) -> set[str]:
    """Return the enabled CONFIG_* symbols declared by a config fragment."""
    enabled: set[str] = set()
    for line in normalize_config_fragment(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if value.strip() == "y":
            enabled.add(name.strip())
    return enabled


def assert_reproducible_recipe(
    env: dict[str, str] | None = None,
    macros: str | None = None,
) -> None:
    """Raise when the build recipe cannot produce reproducible artifacts."""
    env = reproducible_build_env() if env is None else env
    macros = reproducible_build_macros() if macros is None else macros
    tar_options = env.get("TAR_OPTIONS", "")
    for required in ("--mtime=@", "--owner=0", "--group=0", "--sort=name"):
        if required not in tar_options:
            raise ValueError(f"TAR_OPTIONS is missing {required!r}")
    for key in ("KBUILD_BUILD_TIMESTAMP", "KBUILD_BUILD_USER", "KBUILD_BUILD_HOST"):
        if not env.get(key):
            raise ValueError(f"{key} is not pinned")
    if f"%_buildtime {REPRODUCIBLE_BUILD_EPOCH}" not in macros:
        raise ValueError("%_buildtime is not pinned in the rpm macros")


def missing_required_options(
    path: str | os.PathLike[str] | None = None,
    content: str | None = None,
) -> list[str]:
    """Return required campaign config options the fragment does not enable."""
    if content is None:
        content = read_config_fragment(path)
    enabled = config_fragment_options(content)
    return [option for option in REQUIRED_CONFIG_OPTIONS if option not in enabled]


def forbidden_rpmbuild_opts(opts: str | list[str] | None = None) -> list[str]:
    """Return forbidden rpmbuild switches present in *opts*.

    A build with `--without debuginfo` loses BTF and therefore sched_ext, so it
    must never be used for a scheduler-campaign kernel.
    """
    if opts is None:
        opts = list(RPMBUILD_OPTS)
    text = " ".join(opts) if not isinstance(opts, str) else opts
    return [flag for flag in FORBIDDEN_RPMBUILD_OPTS if flag in text]


def assert_rpmbuild_opts(opts: str | list[str] | None = None) -> None:
    bad = forbidden_rpmbuild_opts(opts)
    if bad:
        raise ValueError(
            "rpmbuild options drop BTF/sched_ext and are forbidden: " + ", ".join(bad)
        )


def config_fragment_hash(
    path: str | os.PathLike[str] | None = None,
    content: str | None = None,
    *,
    text: str | None = None,
) -> str:
    if content is None:
        content = text
    if content is None:
        content = read_config_fragment(path)
    normalized = normalize_config_fragment(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _short(value: str, length: int = 12) -> str:
    return value.strip()[:length]


def derive_nvr(
    source_commit: str,
    config_hash: str,
    *,
    name: str = "kernel",
    version: str | None = None,
    release_prefix: str | None = None,
    config_fragment: str | None = None,
    commit: str | None = None,
) -> str:
    """Derive the RPM NVR the kernel-ark build emits for this source+config.

    The source enters the release through the linux-next tag the pinned commit
    carries (`0.0.next.<tag date>.`), the way kernel-ark represents a linux-next
    snapshot; the commit is checked rather than hashed into the string.
    """
    if commit is not None:
        source_commit = commit
    if source_commit != PINNED_SOURCE_COMMIT:
        raise ValueError(
            f"source commit {source_commit!r} is not the pinned source "
            f"{PINNED_SOURCE_COMMIT!r}"
        )
    if config_fragment is not None:
        config_hash = config_fragment_hash(content=config_fragment)
    version = version or NVR_VERSION_BASE
    release_prefix = release_prefix or NVR_UPSTREAM_BUILD
    localversion = NVR_LOCALVERSION_PREFIX + _short(
        config_hash, NVR_LOCALVERSION_HASH_LEN
    )
    release = f"{release_prefix}{NVR_ARK_BUILD}{localversion}.fc{FEDORA_VERSION}"
    return f"{name}-{version}-{release}"


derive_nvr_from_commit_and_config = derive_nvr
build_nvr = derive_nvr
compute_nvr = derive_nvr
nvr_for = derive_nvr
make_nvr = derive_nvr
calculate_nvr = derive_nvr


def derive_nvr_from_paths(
    source_commit: str,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    return derive_nvr(source_commit, config_fragment_hash(path=config_path))


def parse_nvr(value: str) -> NVR:
    name, version, release = value.split("-", 2)
    return NVR(name=name, version=version, release=release)


def lfs_available(root: str | os.PathLike[str] | None = None) -> bool:
    if os.environ.get("K8LABS_LFS_AVAILABLE") == "0":
        return False
    if os.environ.get("GIT_LFS_AVAILABLE") == "0":
        return False
    if shutil.which("git-lfs") is None:
        return False
    target = Path(root) if root is not None else PROJECT_ROOT
    try:
        subprocess.run(
            ["git", "-C", str(target), "lfs", "version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return os.environ.get("K8LABS_LFS_STORAGE_AVAILABLE") != "0"


def ensure_lfs_available(root: str | os.PathLike[str] | None = None) -> None:
    if not lfs_available(root):
        raise LFSUnavailableError("Git LFS storage is unavailable")


is_lfs_available = lfs_available


def track_runtime_rpms_in_lfs(
    gitattributes_path: str | os.PathLike[str] | None = None,
) -> Path:
    path = (
        Path(gitattributes_path)
        if gitattributes_path is not None
        else (PROJECT_ROOT / ".gitattributes")
    )
    lines = ["# Reproducible kernel build: only the four runtime RPMs are in Git LFS."]
    for rpm in RUNTIME_RPMS:
        lines.append(f"{rpm}-*.rpm filter=lfs diff=lfs merge=lfs -text")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _gitattributes_has_lfs(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return all(f"{rpm}-" in text and "filter=lfs" in text for rpm in RUNTIME_RPMS)


def _provisioner_fail_closed() -> bool:
    if not PROVISIONER.is_file():
        return False
    text = PROVISIONER.read_text(encoding="utf-8")
    if "Exact kernel" in text and "is unavailable" in text:
        return False
    return "kernel-modules-extra-" in text


def load_lock(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else LOCK_FILE
    if not target.is_file():
        return {}
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        return {}
    return data


def save_lock(data: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    target = Path(path) if path is not None else LOCK_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        target.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        try:
            import yaml

            target.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        except Exception:  # noqa: BLE001
            target.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    return target


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_entries(lock: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    artifacts = lock.get("artifacts")
    if isinstance(artifacts, list):
        entries.extend(item for item in artifacts if isinstance(item, dict))
    if isinstance(artifacts, dict):
        for group in artifacts.values():
            if isinstance(group, list):
                entries.extend(item for item in group if isinstance(item, dict))
    for key in ("non_lfs_artifacts", "out_of_lfs", "locked_artifacts", "non_lfs"):
        group = lock.get(key)
        if isinstance(group, list):
            entries.extend(item for item in group if isinstance(item, dict))
    runtime = lock.get("runtime_rpms")
    if isinstance(runtime, list):
        for item in runtime:
            if isinstance(item, dict) and "sha256" in item:
                entries.append(item)
    return entries


def verify_artifact_hashes(
    lock: Any,
    artifact_dir: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> list[str]:
    if isinstance(lock, (str, os.PathLike)):
        lock = load_lock(lock)
    if not isinstance(lock, dict):
        lock = {}
    errors: list[str] = []
    base = Path(artifact_dir) if artifact_dir is not None else PROJECT_ROOT
    if root is not None:
        base = Path(root)
    for entry in _artifact_entries(lock):
        expected = entry.get("sha256")
        if not isinstance(expected, str):
            continue
        location = entry.get("cache_path") or entry.get("path") or entry.get("file")
        if not isinstance(location, str):
            continue
        candidate = Path(location)
        if not candidate.is_absolute():
            candidate = base / candidate
        if not candidate.is_file():
            continue
        actual = sha256_file(candidate)
        if actual != expected.lower():
            errors.append(f"hash mismatch: {location} expected {expected} got {actual}")
    return errors


def verify_rpm_hashes(*args: Any, **kwargs: Any) -> list[str]:
    return verify_artifact_hashes(*args, **kwargs)


check_artifact_hashes = verify_artifact_hashes
verify_hashes = verify_artifact_hashes
check_hashes = verify_artifact_hashes


def assert_artifact_hashes(*args: Any, **kwargs: Any) -> None:
    errors = verify_artifact_hashes(*args, **kwargs)
    if errors:
        raise HashMismatchError("; ".join(errors))


def check(
    root: str | os.PathLike[str] | None = None,
    *,
    lfs_root: str | os.PathLike[str] | None = None,
) -> list[str]:
    target = Path(root) if root is not None else PROJECT_ROOT
    errors: list[str] = []

    if not resolve_config_fragment().is_file():
        errors.append("Fedora config fragment is missing")
    else:
        for option in missing_required_options():
            errors.append(f"config fragment does not enable {option}")

    try:
        nvr = derive_nvr(PINNED_SOURCE_COMMIT, config_fragment_hash())
        if not nvr.startswith("kernel-"):
            errors.append("derived NVR is invalid")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"NVR derivation failed: {exc}")

    try:
        assert_rpmbuild_opts()
    except ValueError as exc:
        errors.append(str(exc))

    try:
        assert_reproducible_recipe()
    except ValueError as exc:
        errors.append(str(exc))

    gitattributes = target / ".gitattributes"
    if not _gitattributes_has_lfs(gitattributes):
        errors.append("Git LFS tracking for the four runtime RPMs is missing")
    try:
        ensure_lfs_available(lfs_root or target)
    except LFSUnavailableError as exc:
        errors.append(str(exc))

    if not _provisioner_fail_closed():
        errors.append("RPM install path is not fail-closed on exact package hashes")

    lock = load_lock(LOCK_FILE)
    if lock:
        if lock.get("source_commit") not in (None, PINNED_SOURCE_COMMIT):
            errors.append(
                "kernel build lock source_commit does not match pinned source"
            )
        if lock.get("fedora_image") not in (None, FEDORA_IMAGE):
            errors.append("kernel build lock Fedora image is not digest-pinned")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproducible kernel build harness")
    parser.add_argument("--check", action="store_true", help="validate the harness")
    parser.add_argument("--derive-nvr", action="store_true", help="print derived NVR")
    parser.add_argument("--source-commit", default=PINNED_SOURCE_COMMIT)
    parser.add_argument("--config-fragment", default=None)
    parser.add_argument(
        "--ensure-lfs", action="store_true", help="fail if LFS unavailable"
    )
    parser.add_argument("--write-gitattributes", action="store_true")
    args = parser.parse_args(argv)

    if args.write_gitattributes:
        track_runtime_rpms_in_lfs()
        print("wrote .gitattributes")
    if args.ensure_lfs:
        try:
            ensure_lfs_available()
        except LFSUnavailableError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print("Git LFS storage available")
        if not (args.check or args.derive_nvr or args.write_gitattributes):
            return 0
    if args.derive_nvr:
        fragment_hash = config_fragment_hash(path=args.config_fragment)
        print(derive_nvr(args.source_commit, fragment_hash))
        return 0
    if args.check:
        errors = check()
        if errors:
            for error in errors:
                print(f"FAIL: {error}", file=sys.stderr)
            return 1
        print("kernel build harness OK")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
