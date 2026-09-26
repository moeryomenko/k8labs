"""Validate and canonicalize the k8labs immutable dependency lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_PATHS = (
    ("fedora", "kernel"),
    ("kubernetes", "version"),
    ("kubernetes", "cri_o"),
    ("kubernetes", "crun"),
    ("cilium", "version"),
    ("cluster_api", "version"),
    ("kind", "version"),
    ("kind", "node_image"),
    ("packer", "version"),
    ("cloud_hypervisor", "version"),
)
MUTABLE_REFERENCE = re.compile(
    r"(?:^|[/:@])(?:latest|main|master)(?:$|[/:@])", re.IGNORECASE
)
DIGEST_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")


def load_lock(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("lock must contain a mapping")
    for keys in REQUIRED_PATHS:
        value: Any = data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"missing lock field: {'.'.join(keys)}")
            value = value[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"lock field must be a non-empty string: {'.'.join(keys)}")
    walk_mutable(data, "")
    node_image = data["kind"]["node_image"]
    if not DIGEST_REFERENCE.search(node_image):
        raise ValueError("kind.node_image must be pinned by a sha256 digest")
    return data


def walk_mutable(value: Any, location: str) -> None:
    if isinstance(value, str) and MUTABLE_REFERENCE.search(value):
        raise ValueError(f"mutable reference at {location or '<root>'}: {value}")
    if isinstance(value, dict):
        for key, child in value.items():
            walk_mutable(child, f"{location}.{key}" if location else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk_mutable(child, f"{location}[{index}]")


def canonical_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--json", action="store_true", dest="emit_json")
    args = parser.parse_args()
    try:
        data = load_lock(args.lock)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    payload = canonical_json(data)
    digest = hashlib.sha256(payload).hexdigest()
    if args.emit_json:
        print(payload.decode(), end="")
    else:
        print(f"lock_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
