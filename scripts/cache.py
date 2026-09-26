#!/usr/bin/env python3
"""Validate and maintain a lock-keyed k8labs artifact cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    from lock import canonical_json, load_lock
except ModuleNotFoundError:
    from scripts.lock import canonical_json, load_lock


def lock_digest(lock: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(lock)).hexdigest()


def cache_dir(lock: dict[str, Any], root: Path) -> Path:
    return root / "locks" / lock_digest(lock)


def inventory(lock: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = lock.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise TypeError("lock.artifacts must be a list")
    result: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise TypeError(f"lock.artifacts[{index}] must be a mapping")
        artifact_id = artifact.get("id")
        relative_path = artifact.get("cache_path")
        digest = artifact.get("sha256")
        url = artifact.get("url")
        if not all(
            isinstance(value, str) and value
            for value in (artifact_id, relative_path, digest, url)
        ):
            raise ValueError(
                f"lock.artifacts[{index}] requires id, cache_path, sha256, and url"
            )
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest.lower()
        ):
            raise ValueError(
                f"lock.artifacts[{index}].sha256 must be a hexadecimal SHA-256 digest"
            )
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"lock.artifacts[{index}].cache_path must stay inside the cache"
            )
        if parse.urlparse(url).scheme != "https":
            raise ValueError(f"lock.artifacts[{index}].url must use HTTPS")
        result.append(
            {
                "id": artifact_id,
                "cache_path": relative_path,
                "sha256": digest.lower(),
                "url": url,
            }
        )
    return result


def validate_cache(lock: dict[str, Any], root: Path) -> list[str]:
    directory = cache_dir(lock, root)
    problems: list[str] = []
    if not directory.is_dir():
        return [f"cache directory is missing: {directory}"]
    for name in ("lock.json", "checksums.txt", "artifacts.json", "complete.json"):
        if not (directory / name).is_file():
            problems.append(f"missing cache metadata: {name}")
    if problems:
        return problems
    if json.loads((directory / "lock.json").read_text(encoding="utf-8")) != lock:
        problems.append("lock.json does not match the active lock")
    cached_inventory = json.loads(
        (directory / "artifacts.json").read_text(encoding="utf-8")
    )
    expected_inventory = inventory(lock)
    if cached_inventory != expected_inventory:
        problems.append("artifacts.json does not match the active lock inventory")
    checksums = (directory / "checksums.txt").read_text(encoding="utf-8").splitlines()
    expected_lines = [
        f"{item['sha256']}  {item['cache_path']}" for item in expected_inventory
    ]
    if checksums != expected_lines:
        problems.append("checksums.txt does not match the active lock inventory")
    for item in expected_inventory:
        artifact = directory / item["cache_path"]
        if not artifact.is_file():
            problems.append(f"missing artifact: {item['id']} ({item['cache_path']})")
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            problems.append(f"checksum mismatch: {item['id']} ({item['cache_path']})")
    try:
        complete = json.loads((directory / "complete.json").read_text(encoding="utf-8"))
        if complete.get("lock_sha256") != lock_digest(lock) or complete.get(
            "artifact_count"
        ) != len(expected_inventory):
            problems.append("complete.json does not describe the active lock")
    except (json.JSONDecodeError, OSError) as error:
        problems.append(f"invalid complete.json: {error}")
    return problems


def write_metadata(lock: dict[str, Any], root: Path) -> Path:
    directory = cache_dir(lock, root)
    directory.mkdir(parents=True, exist_ok=True)
    items = inventory(lock)
    (directory / "lock.json.tmp").write_bytes(canonical_json(lock))
    os.replace(directory / "lock.json.tmp", directory / "lock.json")
    (directory / "artifacts.json.tmp").write_text(
        json.dumps(items, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(directory / "artifacts.json.tmp", directory / "artifacts.json")
    checksums = "".join(f"{item['sha256']}  {item['cache_path']}\n" for item in items)
    (directory / "checksums.txt.tmp").write_text(checksums, encoding="utf-8")
    os.replace(directory / "checksums.txt.tmp", directory / "checksums.txt")
    complete = {
        "lock_sha256": lock_digest(lock),
        "artifact_count": len(items),
    }
    (directory / "complete.json.tmp").write_text(
        json.dumps(complete, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(directory / "complete.json.tmp", directory / "complete.json")
    return directory


def populate(lock: dict[str, Any], root: Path) -> Path:
    """Download and checksum-verify every locked artifact before publication."""
    directory = cache_dir(lock, root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "complete.json").unlink(missing_ok=True)

    for item in inventory(lock):
        destination = directory / item["cache_path"]
        if (
            destination.is_file()
            and hashlib.sha256(destination.read_bytes()).hexdigest() == item["sha256"]
        ):
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        digest = hashlib.sha256()
        try:
            with (
                request.urlopen(item["url"], timeout=60) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
        except (OSError, error.URLError) as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"failed to download {item['id']}: {exc}") from exc

        if digest.hexdigest() != item["sha256"]:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"checksum mismatch while downloading {item['id']}")
        os.replace(temporary, destination)

    return write_metadata(lock, root)


def prune(lock: dict[str, Any], root: Path) -> list[Path]:
    locks = root / "locks"
    active = lock_digest(lock)
    removed: list[Path] = []
    if not locks.is_dir():
        return removed
    for candidate in locks.iterdir():
        if candidate.is_dir() and candidate.name != active:
            shutil.rmtree(candidate)
            removed.append(candidate)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--populate", action="store_true")
    parser.add_argument("--write-metadata", action="store_true")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    root = (
        args.cache_root
        or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "k8labs"
    )
    if args.populate:
        if os.environ.get("OFFLINE", "0") == "1":
            parser.error("OFFLINE=1 forbids cache population from the network")
        try:
            print(populate(lock, root))
        except RuntimeError as error:
            parser.error(str(error))
    elif args.write_metadata:
        print(write_metadata(lock, root))
    if args.prune:
        for path in prune(lock, root):
            print(f"removed {path}")
    if args.check or not (args.write_metadata or args.prune):
        problems = validate_cache(lock, root)
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}")
            return 1
        print(f"artifact cache ready: {cache_dir(lock, root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
