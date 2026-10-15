from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_guest_upgrade_sysexts as builder
import cache


def make_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def make_stage_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    crio_binaries: tuple[str, ...] = ("crio", "crictl", "pinns"),
) -> tuple[dict[str, object], Path, Path, dict[str, object], Path]:
    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    stage = lock["kubernetes_upgrade_matrix"][0]
    artifact_groups = lock["artifacts"] + lock["guest_upgrade_artifacts"]
    artifacts = {item["id"]: item for item in artifact_groups}

    kubelet = b"locked kubelet v1.32"
    control_plane = {
        f"kubernetes/server/bin/{name}": f"locked {name} v1.32".encode()
        for name in (
            "kube-apiserver",
            "kube-controller-manager",
            "kube-scheduler",
            "kubectl",
        )
    }
    crio = {
        f"cri-o/bin/{name}": f"locked {name} v1.32".encode() for name in crio_binaries
    }
    payloads = {
        stage["kubelet_artifact"]: kubelet,
        stage["control_plane_artifact"]: make_archive(control_plane),
        stage["cri_o_artifact"]: make_archive(crio),
    }
    for artifact_id, payload in payloads.items():
        artifacts[artifact_id]["sha256"] = hashlib.sha256(payload).hexdigest()

    cache_root = tmp_path / "cache"
    cache_directory = cache.cache_dir(lock, cache_root)
    for artifact_id, payload in payloads.items():
        destination = cache_directory / artifacts[artifact_id]["cache_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    repo_root = tmp_path / "repo"
    for extension, binary_names in {
        "kubelet": ("kubelet",),
        "kubernetes-cp": (
            "kube-apiserver",
            "kube-controller-manager",
            "kube-scheduler",
            "kubectl",
        ),
        "cri-o": ("crio", "crictl", "pinns"),
    }.items():
        source = repo_root / "sysext" / extension
        release = source / "usr/lib/extension-release.d"
        binaries = source / "usr/bin"
        release.mkdir(parents=True)
        binaries.mkdir(parents=True)
        extension_value = "crio" if extension == "cri-o" else extension
        (release / f"extension-release.{extension}").write_text(
            f"ID=fedora\nVERSION_ID=44\nKERNEL_VERSION=7.2.10-200.fc44\nEXTENSION={extension_value}\n",
            encoding="utf-8",
        )
        for name in binary_names:
            (binaries / name).write_bytes(f"stale template {name}".encode())

    (repo_root / "extensions").mkdir()
    (repo_root / "extensions/build.sh").write_bytes(
        (ROOT / "extensions/build.sh").read_bytes()
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mksquashfs = fake_bin / "mksquashfs"
    fake_mksquashfs.write_text(
        '#!/bin/sh\nset -eu\ntar -cf "$2" -C "$1" .\n',
        encoding="utf-8",
    )
    fake_mksquashfs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    return lock, cache_root, repo_root, stage, tmp_path / "stage-output"


def test_stage_builder_uses_locked_binaries_and_keeps_templates_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    lock, cache_root, repo_root, stage, output = make_stage_fixture(
        tmp_path, monkeypatch
    )

    images = builder.build_stage(lock, cache_root, repo_root, stage, output)

    raw_images = output / "images"
    assert {path.name for path in raw_images.glob("*.raw")} == {
        "kubelet.raw",
        "kubernetes-cp.raw",
        "cri-o.raw",
    }
    assert len(images) == 3
    assert all(image["sha256"] for image in images)
    assert (output / "manifest.json").is_file()
    with tarfile.open(raw_images / "kubelet.raw", mode="r:") as image:
        assert image.extractfile("./usr/bin/kubelet").read() == b"locked kubelet v1.32"
    with tarfile.open(raw_images / "kubernetes-cp.raw", mode="r:") as image:
        assert image.extractfile("./usr/bin/kube-apiserver").read() == (
            b"locked kube-apiserver v1.32"
        )
    with tarfile.open(raw_images / "cri-o.raw", mode="r:") as image:
        assert image.extractfile("./usr/bin/crictl").read() == b"locked crictl v1.32"
    assert (repo_root / "sysext/kubelet/usr/bin/kubelet").read_bytes() == (
        b"stale template kubelet"
    )
    assert (
        repo_root / "sysext/kubernetes-cp/usr/bin/kube-apiserver"
    ).read_bytes() == b"stale template kube-apiserver"
    assert (repo_root / "sysext/cri-o/usr/bin/crictl").read_bytes() == (
        b"stale template crictl"
    )


def test_stage_builder_rejects_missing_crio_archive_member(
    tmp_path: Path, monkeypatch
) -> None:
    lock, cache_root, repo_root, stage, output = make_stage_fixture(
        tmp_path, monkeypatch, crio_binaries=("crio", "pinns")
    )

    try:
        builder.build_stage(lock, cache_root, repo_root, stage, output)
    except ValueError as error:
        assert "crictl" in str(error)
    else:
        raise AssertionError("missing locked CRI-O binary must fail the stage build")

    assert not output.exists()
