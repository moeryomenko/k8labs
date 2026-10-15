#!/usr/bin/env python3
"""Build isolated, non-activated sysext images from locked upgrade artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import cache
from lock import load_lock

STAGE_BINARIES = {
    "kubelet": ("kubelet",),
    "kubernetes-cp": (
        "kube-apiserver",
        "kube-controller-manager",
        "kube-scheduler",
        "kubectl",
    ),
    "cri-o": ("crio", "crictl", "pinns"),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_template(template: Path, destination: Path, extension: str) -> None:
    if not template.is_dir():
        raise FileNotFoundError(f"sysext template directory is missing: {template}")
    ignored_binaries = set(STAGE_BINARIES[extension])

    def ignore_existing_binaries(directory: str, names: list[str]) -> set[str]:
        if Path(directory).relative_to(template).as_posix() == "usr/bin":
            return ignored_binaries.intersection(names)
        return set()

    shutil.copytree(template, destination, ignore=ignore_existing_binaries)


def _member_matches(member_path: PurePosixPath, binary: str, layout: str) -> bool:
    if member_path.is_absolute() or ".." in member_path.parts:
        return False
    if layout == "cri-o":
        return len(member_path.parts) >= 2 and member_path.parts[-2:] == ("bin", binary)
    return member_path.parts in {
        ("kubernetes", "server", "bin", binary),
        ("server", "bin", binary),
        ("bin", binary),
    }


def _extract_archive_binaries(
    archive_path: Path,
    destination: Path,
    binary_names: tuple[str, ...],
    layout: str,
) -> None:
    matches: dict[str, list[tarfile.TarInfo]] = {name: [] for name in binary_names}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isreg():
                continue
            member_path = PurePosixPath(member.name)
            for binary in binary_names:
                if _member_matches(member_path, binary, layout):
                    matches[binary].append(member)

        for binary, members in matches.items():
            if len(members) != 1:
                raise ValueError(
                    f"{archive_path.name} must contain exactly one {binary} binary; "
                    f"found {len(members)}"
                )

        destination.mkdir(parents=True, exist_ok=True)
        for binary, members in matches.items():
            source = archive.extractfile(members[0])
            if source is None:
                raise ValueError(f"unable to read {binary} from {archive_path.name}")
            target = destination / binary
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            if target.stat().st_size == 0:
                raise ValueError(
                    f"{binary} extracted as an empty file from {archive_path.name}"
                )
            target.chmod(0o755)


def build_stage(
    lock: dict[str, Any],
    cache_root: Path,
    repo_root: Path,
    stage: dict[str, Any],
    output_dir: Path,
) -> list[dict[str, object]]:
    """Build one version's kubelet, control-plane, and CRI-O sysext images.

    The output path must not exist. Images are built in a private temporary
    directory and atomically published only after all three builds succeed.
    """
    if output_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite existing stage output: {output_dir}"
        )

    artifact_ids = {
        "kubelet": stage["kubelet_artifact"],
        "kubernetes-cp": stage["control_plane_artifact"],
        "cri-o": stage["cri_o_artifact"],
    }
    inventory = {item["id"]: item for item in cache.kubernetes_upgrade_inventory(lock)}
    artifact_paths: dict[str, Path] = {}
    for extension, artifact_id in artifact_ids.items():
        if artifact_id not in inventory:
            raise ValueError(
                f"upgrade matrix references an unknown artifact: {artifact_id}"
            )
        item = inventory[artifact_id]
        artifact_path = cache.cache_dir(lock, cache_root) / item["cache_path"]
        if not artifact_path.is_file():
            raise FileNotFoundError(
                f"missing locked artifact {artifact_id}: {artifact_path}"
            )
        if _sha256_file(artifact_path) != item["sha256"]:
            raise ValueError(f"checksum mismatch for locked artifact {artifact_id}")
        artifact_paths[extension] = artifact_path

    build_script_source = repo_root / "extensions" / "build.sh"
    if not build_script_source.is_file():
        raise FileNotFoundError(
            f"sysext build script is missing: {build_script_source}"
        )
    for extension in STAGE_BINARIES:
        template = repo_root / "sysext" / extension
        if not template.is_dir():
            raise FileNotFoundError(f"sysext template directory is missing: {template}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.stage-", dir=output_dir.parent
    ) as temporary:
        work_dir = Path(temporary)
        sources_dir = work_dir / "sources"
        for extension in STAGE_BINARIES:
            _copy_template(
                repo_root / "sysext" / extension,
                sources_dir / extension,
                extension,
            )

        _copy_locked_binary(
            artifact_paths["kubelet"],
            sources_dir / "kubelet/usr/bin/kubelet",
        )
        _extract_archive_binaries(
            artifact_paths["kubernetes-cp"],
            sources_dir / "kubernetes-cp/usr/bin",
            STAGE_BINARIES["kubernetes-cp"],
            "kubernetes-server",
        )
        _extract_archive_binaries(
            artifact_paths["cri-o"],
            sources_dir / "cri-o/usr/bin",
            STAGE_BINARIES["cri-o"],
            "cri-o",
        )

        builder_extensions = work_dir / "builder" / "extensions"
        builder_extensions.mkdir(parents=True)
        build_script = builder_extensions / "build.sh"
        shutil.copyfile(build_script_source, build_script)
        images: list[dict[str, object]] = []
        for extension in STAGE_BINARIES:
            source = sources_dir / extension
            result = subprocess.run(
                ["sh", str(build_script), "sysext", str(source), extension],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"building {extension} sysext failed: {detail}")
            image_path = builder_extensions / "release" / f"{extension}.raw"
            if not image_path.is_file() or image_path.stat().st_size == 0:
                raise RuntimeError(f"sysext builder did not produce {image_path.name}")
            images.append(
                {
                    "name": extension,
                    "path": f"images/{extension}.raw",
                    "sha256": _sha256_file(image_path),
                    "size_bytes": image_path.stat().st_size,
                }
            )

        images_dir = work_dir / "images"
        images_dir.mkdir()
        for image in images:
            source = builder_extensions / "release" / f"{image['name']}.raw"
            shutil.move(str(source), str(images_dir / f"{image['name']}.raw"))

        stage_manifest = {
            "lock_sha256": cache.lock_digest(lock),
            "kubernetes": stage["kubernetes"],
            "cri_o": stage["cri_o"],
            "input_artifacts": {
                extension: artifact_ids[extension] for extension in STAGE_BINARIES
            },
            "images": images,
        }
        (work_dir / "manifest.json").write_text(
            json.dumps(stage_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(sources_dir)
        shutil.rmtree(work_dir / "builder")
        if output_dir.exists():
            raise FileExistsError(
                f"refusing to overwrite existing stage output: {output_dir}"
            )
        os.rename(work_dir, output_dir)

    return images


def _copy_locked_binary(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if destination.stat().st_size == 0:
        raise ValueError(f"locked binary is empty: {source}")
    destination.chmod(0o755)


def build_stages(
    lock: dict[str, Any],
    cache_root: Path,
    repo_root: Path,
    output_dir: Path,
    version: str | None = None,
) -> list[dict[str, object]]:
    """Build all locked stages, or only the requested Kubernetes version."""
    matrix = lock["kubernetes_upgrade_matrix"]
    selected = [
        stage for stage in matrix if version is None or stage["kubernetes"] == version
    ]
    if not selected:
        raise ValueError(f"Kubernetes version is not in the upgrade matrix: {version}")
    if output_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite existing output directory: {output_dir}"
        )
    output_dir.mkdir(parents=True)

    outputs: list[dict[str, object]] = []
    for stage in selected:
        stage_path = output_dir / stage["kubernetes"]
        images = build_stage(lock, cache_root, repo_root, stage, stage_path)
        outputs.append(
            {
                "kubernetes": stage["kubernetes"],
                "cri_o": stage["cri_o"],
                "images": images,
            }
        )

    (output_dir / "manifest.json").write_text(
        json.dumps(
            {"lock_sha256": cache.lock_digest(lock), "stages": outputs},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build non-activated sysext images from verified Kubernetes upgrade artifacts."
    )
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kubernetes-version", default=None)
    args = parser.parse_args()

    try:
        lock = load_lock(args.lock)
        if shutil.which("mksquashfs") is None:
            raise RuntimeError("mksquashfs is required to build systemd sysext images")
        outputs = build_stages(
            lock,
            args.cache_root.expanduser(),
            args.repo_root.resolve(),
            args.output_dir.expanduser(),
            args.kubernetes_version,
        )
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        tarfile.TarError,
    ) as error:
        parser.error(str(error))

    print(f"built {len(outputs)} Kubernetes stage(s) under {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
