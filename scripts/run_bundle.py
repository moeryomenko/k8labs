"""Create a redacted, schema-versioned k8labs run bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from lock import canonical_json, load_lock

SCHEMA_VERSION = 1
SENSITIVE = ("KUBECONFIG", "TOKEN", "PASSWORD", "PRIVATE_KEY", "SECRET")


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def redacted_env() -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(token in key.upper() for token in SENSITIVE):
            result[key] = "<redacted>"
        else:
            result[key] = value
    return dict(sorted(result.items()))


def command_result(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "argv": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20000:],
        "stderr": completed.stderr[-20000:],
        "timestamp": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("build/runs"))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--lock", type=Path, default=Path("versions.lock.yaml"))
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    run_id = (
        args.run_id
        or f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    )
    bundle = args.root / run_id
    bundle.mkdir(parents=True, exist_ok=False)
    lock_data = load_lock(args.lock)
    lock_bytes = canonical_json(lock_data)
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()
    (bundle / "lock.yaml").write_bytes(lock_bytes)
    (bundle / "environment.json").write_text(
        json.dumps(redacted_env(), indent=2) + "\n", encoding="utf-8"
    )

    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "lock_sha256": lock_sha,
        "offline": os.environ.get("OFFLINE", "0") == "1",
        "started_at": now(),
        "commands": [],
        "probes": [],
        "status": "created",
    }
    if args.probe:
        run["commands"].append(command_result(["make", "versions-check"]))
        run["probes"].append(
            {
                "name": "lock",
                "status": "PASS" if run["commands"][-1]["returncode"] == 0 else "FAIL",
            }
        )
    run["status"] = "complete"
    run["finished_at"] = now()
    (bundle / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
