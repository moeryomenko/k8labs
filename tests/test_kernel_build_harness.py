"""Tests for the reproducible kernel build harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "kernel_build_harness.py"

_spec = importlib.util.spec_from_file_location("kernel_build_harness", SCRIPT)
assert _spec and _spec.loader
kbh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kbh)


def test_nvr_is_deterministic() -> None:
    commit = kbh.PINNED_SOURCE_COMMIT
    fragment_hash = kbh.config_fragment_hash()
    first = kbh.derive_nvr(commit, fragment_hash)
    second = kbh.derive_nvr(commit, fragment_hash)
    assert first == second
    assert first.startswith("kernel-")
    # The config fragment is part of the release, so changing it changes the NVR.
    assert kbh.derive_nvr(commit, "f" * 64) != first
    assert kbh.derive_nvr(commit, "f" * 64).endswith(".fc44")


def test_nvr_rejects_a_non_pinned_source_commit() -> None:
    with pytest.raises(ValueError, match="is not the pinned source"):
        kbh.derive_nvr("0" * 40, kbh.config_fragment_hash())


def test_nvr_matches_the_kernel_ark_build_output() -> None:
    # Verified against the SRPM the build system emitted:
    # kernel-7.3.0-0.0.next.20261016.64.k8.680e037.fc44.src.rpm
    assert (
        kbh.derive_nvr(kbh.PINNED_SOURCE_COMMIT, kbh.config_fragment_hash())
        == "kernel-7.3.0-0.0.next.20261016.64.k8.680e037.fc44"
    )


def test_config_fragment_exists_and_hash_stable() -> None:
    path = kbh.resolve_config_fragment()
    assert path.is_file()
    assert len(kbh.config_fragment_hash()) == 64
    assert kbh.config_fragment_hash() == kbh.config_fragment_hash(path=path)


def test_runtime_rpms_are_exactly_four() -> None:
    assert set(kbh.RUNTIME_RPMS) == {
        "kernel-core",
        "kernel-modules",
        "kernel-modules-core",
        "kernel-modules-extra",
    }
    assert len(kbh.RUNTIME_RPMS) == 4


def test_lfs_tracking_contains_only_runtime_rpms(tmp_path: Path) -> None:
    gitattributes = tmp_path / ".gitattributes"
    kbh.track_runtime_rpms_in_lfs(gitattributes)
    text = gitattributes.read_text(encoding="utf-8")
    for rpm in kbh.RUNTIME_RPMS:
        assert f"{rpm}-" in text
        assert "filter=lfs" in text
    assert "qcow2" not in text
    assert "firmware" not in text


def test_missing_lfs_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8LABS_LFS_AVAILABLE", "0")
    assert kbh.lfs_available(tmp_path) is False
    with pytest.raises(kbh.LFSUnavailableError):
        kbh.ensure_lfs_available(tmp_path)


def test_hash_mismatch_fails(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"hello")
    actual = kbh.sha256_file(artifact)
    wrong = "0" * 64
    lock = {"artifacts": [{"cache_path": "artifact.bin", "sha256": wrong}]}
    errors = kbh.verify_artifact_hashes(lock, tmp_path)
    assert errors
    assert "hash mismatch" in errors[0]
    # Matching hash produces no error.
    lock_ok = {"artifacts": [{"cache_path": "artifact.bin", "sha256": actual}]}
    assert kbh.verify_artifact_hashes(lock_ok, tmp_path) == []


def test_no_fallback_provisioner() -> None:
    assert kbh._provisioner_fail_closed() is True
    assert kbh.check() == []


def test_lock_file_records_nvr_and_image() -> None:
    lock = kbh.load_lock(kbh.LOCK_FILE)
    assert lock.get("source_commit") == kbh.PINNED_SOURCE_COMMIT
    assert lock.get("fedora_image") == kbh.FEDORA_IMAGE
    assert lock.get("nvr", "").startswith("kernel-")
    assert lock.get("clean_builds_required") == 2


def test_hsm_and_operator_approval_required() -> None:
    assert kbh.RELEASE_REQUIRES_HSM_SIGNATURE is True
    assert kbh.OPERATOR_APPROVAL_REQUIRED is True
    assert kbh.CLEAN_BUILDS_REQUIRED == 2


def test_build_without_debuginfo_is_rejected() -> None:
    # The spec strips CONFIG_DEBUG_INFO*=y for --without debuginfo, which drops
    # BTF and therefore CONFIG_SCHED_CLASS_EXT.
    assert kbh.forbidden_rpmbuild_opts(("--without debuginfo",)) == [
        "--without debuginfo"
    ]
    with pytest.raises(ValueError, match="forbidden"):
        kbh.assert_rpmbuild_opts(["--without debug", "--without debuginfo"])
    assert kbh.forbidden_rpmbuild_opts() == []


def test_release_build_opts_keep_bpf_and_sched_ext_config() -> None:
    assert "CONFIG_DEBUG_INFO_BTF" in kbh.REQUIRED_CONFIG_OPTIONS
    assert "CONFIG_SCHED_CLASS_EXT" in kbh.REQUIRED_CONFIG_OPTIONS
    assert "--without debuginfo" not in kbh.RPMBUILD_OPTS
    # debug (the second kernel variant) is skipped; debuginfo packaging is not.
    assert "--without debug" in kbh.RPMBUILD_OPTS


def test_reproducible_recipe_pins_build_time_and_tar_metadata() -> None:
    env = kbh.reproducible_build_env()
    macros = kbh.reproducible_build_macros()
    assert f"--mtime=@{kbh.REPRODUCIBLE_BUILD_EPOCH}" in env["TAR_OPTIONS"]
    assert "--sort=name" in env["TAR_OPTIONS"]
    assert f"%_buildtime {kbh.REPRODUCIBLE_BUILD_EPOCH}" in macros
    assert env["KBUILD_BUILD_TIMESTAMP"] == kbh.REPRODUCIBLE_BUILD_TIMESTAMP
    assert env["KBUILD_BUILD_USER"] == "k8labs"
    assert env["KBUILD_BUILD_HOST"] == "k8labs-build"
    kbh.assert_reproducible_recipe()


def test_reproducible_recipe_rejects_a_missing_pin() -> None:
    with pytest.raises(ValueError, match="TAR_OPTIONS"):
        kbh.assert_reproducible_recipe(env={"TAR_OPTIONS": ""})
    with pytest.raises(ValueError, match="_buildtime"):
        kbh.assert_reproducible_recipe(macros="")
