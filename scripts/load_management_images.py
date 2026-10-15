#!/usr/bin/env python3
"""Import lock-verified OCI layouts into Podman and, optionally, Kind."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

try:
    from cache import cache_dir, oci_inventory, validate_cache
    from lock import load_lock
except ModuleNotFoundError:
    from scripts.cache import cache_dir, oci_inventory, validate_cache
    from scripts.lock import load_lock


def run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, text=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"command failed ({' '.join(command)}): {exc.returncode}") from exc


def image_digest(reference: str) -> str:
    expected = reference.rsplit("@", 1)[1]
    result = subprocess.run(
        ["podman", "image", "inspect", "--format", "{{json .RepoDigests}}", reference],
        check=True,
        text=True,
        capture_output=True,
    )
    if expected not in result.stdout:
        raise SystemExit(f"Podman import did not preserve locked image digest for {reference}")
    return expected


def staging_reference(reference: str) -> str:
    repository, digest = reference.rsplit("@", 1)
    algorithm, value = digest.split(":", 1)
    return f"{repository}:{algorithm}-{value}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--image-id", action="append", dest="image_ids")
    parser.add_argument("--kind-name")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    problems = validate_cache(lock, args.cache_root)
    if problems:
        raise SystemExit("verified image cache required: " + "; ".join(problems))
    directory = cache_dir(lock, args.cache_root)
    entries = oci_inventory(lock)
    requested = set(args.image_ids or [entry["id"] for entry in entries])
    unknown = requested - {entry["id"] for entry in entries}
    if unknown:
        raise SystemExit(f"unknown locked OCI image: {', '.join(sorted(unknown))}")
    for entry in entries:
        if entry["id"] not in requested:
            continue
        layout = directory / entry["cache_path"]
        run(
            [
                "skopeo",
                "copy",
                f"oci:{layout}:{entry['id']}",
                f"containers-storage:{entry['reference']}",
            ]
        )
        image_digest(entry["reference"])
        if args.kind_name:
            staging = staging_reference(entry["reference"])
            run(["podman", "tag", entry["reference"], staging])
            try:
                run(["kind", "load", "docker-image", "--name", args.kind_name, staging])
            finally:
                run(["podman", "untag", staging])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
