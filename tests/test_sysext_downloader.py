from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def make_project(
    tmp_path: Path,
    expected_bytes: bytes,
    *,
    artifact_id: str = "kubelet-v1.37.0",
    sysext_name: str = "kubelet",
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    extensions = project / "extensions"
    scripts = project / "scripts"
    extensions.mkdir(parents=True)
    scripts.mkdir()
    (project / "sysext").mkdir()
    shutil.copyfile(
        ROOT / "extensions/download-sysexts.sh",
        extensions / "download-sysexts.sh",
    )
    shutil.copyfile(ROOT / "scripts/lock.py", scripts / "lock.py")

    lock = yaml.safe_load((ROOT / "versions.lock.yaml").read_text(encoding="utf-8"))
    artifact = next(
        item
        for item in lock["artifacts"] + lock["guest_upgrade_artifacts"]
        if item["id"] == artifact_id
    )
    artifact["url"] = f"https://fixture.invalid/{sysext_name}"
    artifact["sha256"] = hashlib.sha256(expected_bytes).hexdigest()
    (project / "versions.lock.yaml").write_text(
        yaml.safe_dump(lock, sort_keys=False), encoding="utf-8"
    )

    manifest = yaml.safe_load(
        (ROOT / "extensions/manifest.yaml").read_text(encoding="utf-8")
    )
    sysext = next(item for item in manifest["sysexts"] if item["name"] == sysext_name)
    sysext["sources"][0]["url"] = artifact["url"]
    (extensions / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "output=\n"
        "url=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    -o) output=$2; shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) url=$1; shift ;;\n"
        "  esac\n"
        "done\n"
        'printf \'%s\\n\' "$url" >> "$CURL_LOG"\n'
        'cp -- "$FAKE_RESPONSE" "$output"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    return project, fake_bin


def run_download(
    project: Path, fake_bin: Path, response: bytes, sysext_name: str
) -> subprocess.CompletedProcess[str]:
    response_file = project.parent / "response.bin"
    response_file.write_bytes(response)
    curl_log = project.parent / "curl.log"
    env = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RESPONSE": str(response_file),
        "CURL_LOG": str(curl_log),
    }
    return subprocess.run(
        [
            "sh",
            str(project / "extensions/download-sysexts.sh"),
            "--sequential",
            sysext_name,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_downloader_rejects_bytes_that_do_not_match_lock_digest(tmp_path: Path) -> None:
    expected = b"pinned kubelet bytes"
    project, fake_bin = make_project(tmp_path, expected)

    result = run_download(project, fake_bin, b"tampered kubelet bytes", "kubelet")

    target = project / "sysext/kubelet/usr/bin/kubelet"
    marker = project / "build/.sysext-download-cache/kubelet.version"
    assert result.returncode != 0
    assert "sha256 mismatch" in result.stderr
    assert not target.exists()
    assert not marker.exists()


def test_downloader_redownloads_when_cached_binary_bytes_change(tmp_path: Path) -> None:
    expected = b"pinned kubelet bytes"
    project, fake_bin = make_project(tmp_path, expected)

    first = run_download(project, fake_bin, expected, "kubelet")
    target = project / "sysext/kubelet/usr/bin/kubelet"
    assert first.returncode == 0, first.stderr
    assert target.read_bytes() == expected

    target.write_bytes(b"stale or modified cached binary")
    second = run_download(project, fake_bin, expected, "kubelet")

    assert second.returncode == 0, second.stderr
    assert target.read_bytes() == expected
    assert (
        len((project.parent / "curl.log").read_text(encoding="utf-8").splitlines()) == 2
    )


def test_downloader_does_not_reuse_stale_crio_archive_extraction(
    tmp_path: Path,
) -> None:
    expected = b"pinned crio archive"
    project, fake_bin = make_project(
        tmp_path,
        expected,
        artifact_id="cri-o-v1.37.0",
        sysext_name="cri-o",
    )
    stale_root = project / "sysext/cri-o/.download.tmp/stale/bin"
    stale_root.mkdir(parents=True)
    (stale_root / "crictl").write_bytes(b"stale crictl from an earlier download")
    (project / "sysext/cri-o/usr/bin").mkdir(parents=True)
    (project / "sysext/cri-o/usr/bin/crictl").write_bytes(b"old cached crictl")

    fake_tar = fake_bin / "tar"
    fake_tar.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "extract=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    -C) extract=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'mkdir -p "$extract/release/bin"\n'
        "printf 'new crio' > \"$extract/release/bin/crio\"\n"
        "printf 'new pinns' > \"$extract/release/bin/pinns\"\n",
        encoding="utf-8",
    )
    fake_tar.chmod(0o755)

    result = run_download(project, fake_bin, expected, "cri-o")

    assert result.returncode != 0
    assert "crictl" in result.stderr
    assert not (project / "build/.sysext-download-cache/cri-o.version").exists()
