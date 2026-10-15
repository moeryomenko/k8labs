"""Gate: the node image provisions the native observer tooling.

The linux-sched series runs bpftrace, perf and the BCC tools **directly on the
worker** instead of as privileged observer DaemonSets. Two environment facts
forced that choice on this lab:

* cri-o on the worker refuses to create device nodes for privileged containers
  (``mknodat /dev/loop0: Permission denied``), so a privileged observer Pod
  cannot start at all; and
* the apiserver's kubelet client lacks ``nodes/proxy``, so ``kubectl logs`` and
  ``kubectl exec`` are forbidden cluster-wide and cannot be used to collect
  results from a Pod anyway.

This module checks that the observer tooling is baked into the node image and
that no privileged observer manifests have crept back in. It runs without a
cluster so it belongs in the default test suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKER_PROVISIONER = PROJECT_ROOT / "packer" / "scripts" / "01-configure-system.sh"
SERIES_DIR = PROJECT_ROOT / "research" / "linux-sched"

REQUIRED_PACKAGES = ("bpftrace", "perf", "bcc-tools", "python3-bcc", "bpftool")


def _dnf_install_block(text: str) -> str:
    """Return the concatenation of every `dnf install ...` command."""

    lines = text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("dnf install"):
            collecting = True
            current = [stripped]
        elif collecting:
            if stripped.endswith("\\"):
                current.append(stripped)
            else:
                current.append(stripped)
                blocks.append(" ".join(current))
                collecting = False
    return "\n".join(blocks)


def check() -> list[str]:
    errors: list[str] = []

    if not PACKER_PROVISIONER.is_file():
        return [f"missing node-image provisioner: {PACKER_PROVISIONER}"]

    block = _dnf_install_block(PACKER_PROVISIONER.read_text(encoding="utf-8"))
    for package in REQUIRED_PACKAGES:
        if not re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", block):
            errors.append(
                f"node image does not install required observer package: {package}"
            )

    telemetry_dir = SERIES_DIR / "telemetry"
    if telemetry_dir.exists():
        errors.append(
            "research/linux-sched/telemetry still exists; privileged observer "
            "DaemonSets are not supported on this lab"
        )

    capture = SERIES_DIR / "capture" / "capture-block.sh"
    if capture.is_file():
        text = capture.read_text(encoding="utf-8")
        if "privileged: true" in text or "securityContext" in text:
            errors.append(
                "capture-block.sh must not create privileged Pods; observers run "
                "node-native"
            )
        for tool in ("bpftrace", "perf sched", "runqlat"):
            if tool not in text:
                errors.append(f"capture-block.sh does not invoke native tool: {tool}")

    return errors


def main() -> int:
    errors = check()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("node-native observer tooling OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
