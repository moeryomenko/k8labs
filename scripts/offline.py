#!/usr/bin/env python3
"""Fail-closed checks shared by network-capable k8labs targets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

try:
    from cache import lock_digest, validate_cache
    from lock import load_lock
except ModuleNotFoundError:
    from scripts.cache import lock_digest, validate_cache
    from scripts.lock import load_lock


def offline_enabled() -> bool:
    return os.environ.get("OFFLINE", "0") == "1"


def require_cache(cache_root: Path, lock: dict[str, Any]) -> Path:
    digest = lock_digest(lock)
    cache = cache_root / "locks" / digest
    problems = validate_cache(lock, cache_root)
    if problems:
        raise SystemExit(
            f"offline cache is incomplete for lock {digest}: " + "; ".join(problems)
        )
    return cache


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--lock-digest", default=None)
    parser.add_argument("--network-operation", action="store_true")
    args = parser.parse_args()
    if offline_enabled() and args.network_operation:
        raise SystemExit("OFFLINE=1 forbids network access; use verified local cache and registry artifacts")
    if offline_enabled():
        lock = load_lock(args.lock)
        if args.lock_digest is not None and args.lock_digest != lock_digest(lock):
            raise SystemExit("offline lock digest does not match the supplied lock")
        print(require_cache(args.cache_root, lock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
