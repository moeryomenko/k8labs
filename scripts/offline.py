#!/usr/bin/env python3
"""Fail-closed checks shared by network-capable k8labs targets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def offline_enabled() -> bool:
    return os.environ.get("OFFLINE", "0") == "1"


def require_cache(cache_root: Path, lock_digest: str) -> Path:
    cache = cache_root / "locks" / lock_digest
    required = ("lock.json", "checksums.txt", "artifacts.json", "complete.json")
    missing = [name for name in required if not (cache / name).is_file()]
    if missing:
        names = ", ".join(missing)
        raise SystemExit(
            f"offline cache is incomplete for lock {lock_digest}: missing {names}"
        )
    return cache


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--network-operation", action="store_true")
    args = parser.parse_args()
    if offline_enabled() and args.network_operation:
        raise SystemExit(
            "OFFLINE=1 forbids network access; use verified local cache and registry artifacts"
        )
    if offline_enabled():
        print(require_cache(args.cache_root, args.lock_digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
