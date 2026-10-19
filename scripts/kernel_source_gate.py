"""Kernel source gate for the linux-sched research series."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

PINNED_COMMIT = "2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64"
PINNED_TAG = "next-20261016"
PINNED_DATE = "2026-10-16"
UPSTREAM_REPO = "https://git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git"

PINNED_COMMITS = (PINNED_COMMIT,)
PINNED_TAGS = (PINNED_TAG,)

REQUIRED_INTERFACES: list[dict[str, str]] = [
    {
        "id": "bpf_f_arena_scalar",
        "name": "bpf_f_arena_scalar",
        "description": "BPF_F_ARENA_SCALAR prog-flag definition",
        "file": "include/uapi/linux/bpf.h",
        "path": "include/uapi/linux/bpf.h",
        "pattern": r"#define\s+BPF_F_ARENA_SCALAR\b",
        "symbol": "BPF_F_ARENA_SCALAR",
    },
    {
        "id": "bpf_jit_supports_arena_scalar_x86",
        "name": "bpf_jit_supports_arena_scalar_x86",
        "description": "x86 JIT support for arena scalar",
        "file": "arch/x86/net/bpf_jit_comp.c",
        "path": "arch/x86/net/bpf_jit_comp.c",
        "pattern": r"bpf_jit_supports_arena_scalar",
        "symbol": "bpf_jit_supports_arena_scalar",
    },
    {
        "id": "sched_ext_ops_sub_cid_sched_updated",
        "name": "sched_ext_ops_sub_cid_sched_updated",
        "description": "sched_ext_ops.sub_cid_sched_updated() callback",
        "file": "kernel/sched/ext/internal.h",
        "path": "kernel/sched/ext/internal.h",
        "pattern": r"sub_cid_sched_updated",
        "symbol": "sub_cid_sched_updated",
    },
]

REQUIRED = REQUIRED_INTERFACES
REQUIRES = REQUIRED_INTERFACES
REQUIRED_PATHS = [entry["file"] for entry in REQUIRED_INTERFACES]
REQUIRED_SYMBOLS = [entry["symbol"] for entry in REQUIRED_INTERFACES]
INTERFACES = REQUIRED_INTERFACES


def _find_project_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return start.parent if start.parent != start else start


PROJECT_ROOT = _find_project_root(Path(__file__).resolve().parent)


def _data_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    names = (
        "kernel_source_pinned.yaml",
        "kernel_source_pinned.yml",
        "kernel_source_pinned.json",
        "kernel_source.lock.yaml",
        "kernel_source.lock.yml",
        "kernel_source.lock.json",
        "kernel-source-pinned.yaml",
        "pinned_kernel_source.yaml",
        "kernel_source.yaml",
    )
    candidates: list[Path] = []
    seen: set[Path] = set()
    for base in (here, PROJECT_ROOT):
        for name in names:
            path = base / name
            if path not in seen:
                seen.add(path)
                candidates.append(path)
    return candidates


def _embedded_pinned() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "kernel-source-gate",
        "pinned_commit": PINNED_COMMIT,
        "commit": PINNED_COMMIT,
        "kernel_commit": PINNED_COMMIT,
        "linux_next_commit": PINNED_COMMIT,
        "source_commit": PINNED_COMMIT,
        "revision": PINNED_COMMIT,
        "pinned_tag": PINNED_TAG,
        "tag": PINNED_TAG,
        "linux_next_tag": PINNED_TAG,
        "pinned_date": PINNED_DATE,
        "date": PINNED_DATE,
        "upstream_repo": UPSTREAM_REPO,
        "repository": UPSTREAM_REPO,
        "required_interfaces": [dict(entry) for entry in REQUIRED_INTERFACES],
        "interfaces": [dict(entry) for entry in REQUIRED_INTERFACES],
        "requirements": [dict(entry) for entry in REQUIRED_INTERFACES],
    }


def _normalise_interface(entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        entry = {}
    file_path = (
        entry.get("file")
        or entry.get("path")
        or entry.get("source_file")
        or entry.get("location")
        or ""
    )
    pattern = (
        entry.get("pattern")
        or entry.get("symbol")
        or entry.get("regex")
        or entry.get("match")
        or ""
    )
    if pattern and not any(ch in pattern for ch in ".*+?[](){}\\|^$"):
        pattern = re.escape(pattern)
    identifier = (
        entry.get("id")
        or entry.get("name")
        or entry.get("interface")
        or entry.get("symbol")
        or file_path
    )
    description = entry.get("description") or entry.get("desc") or ""
    return {
        "id": str(identifier),
        "name": str(identifier),
        "description": str(description),
        "file": str(file_path),
        "path": str(file_path),
        "pattern": str(pattern),
        "symbol": str(entry.get("symbol") or entry.get("id") or entry.get("name") or ""),
    }


def _get_commit(pinned: dict[str, Any]) -> str:
    for key in (
        "pinned_commit",
        "commit",
        "kernel_commit",
        "linux_next_commit",
        "source_commit",
        "revision",
    ):
        value = pinned.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _get_interfaces(pinned: dict[str, Any]) -> list[dict[str, str]]:
    raw = None
    for key in ("required_interfaces", "interfaces", "requirements", "required"):
        if isinstance(pinned.get(key), list):
            raw = pinned[key]
            break
    if raw is None:
        return [dict(entry) for entry in REQUIRED_INTERFACES]
    normalised = [_normalise_interface(entry) for entry in raw]
    if not normalised or all(not item["file"] for item in normalised):
        return [dict(entry) for entry in REQUIRED_INTERFACES]
    return normalised


def load_pinned(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = [candidate for candidate in _data_candidates() if candidate.is_file()]
    if not candidates:
        return _embedded_pinned()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            if candidate.suffix.lower() == ".json":
                data = json.loads(text)
            else:
                try:
                    import yaml

                    data = yaml.safe_load(text)
                except (ImportError, OSError, ValueError, RuntimeError, TypeError):
                    data = None
            if isinstance(data, dict):
                merged = _embedded_pinned()
                merged.update({k: v for k, v in data.items() if v is not None})
                for key in ("required_interfaces", "interfaces", "requirements"):
                    if isinstance(data.get(key), list):
                        merged[key] = data[key]
                return merged
        except (OSError, ValueError, TypeError, RuntimeError):
            continue
    return _embedded_pinned()


def _git_head(source_root: Path) -> str | None:
    if not (source_root / ".git").exists():
        return None
    # Prefer the real git command when the tree is a valid repository.
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        value = result.stdout.strip()
        if value:
            return value
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    # Fall back to a minimal manual parse so synthetic trees used in tests are
    # still comparable against the pinned commit.
    try:
        head_file = source_root / ".git" / "HEAD"
        content = head_file.read_text(encoding="utf-8").strip()
        if not content:
            return None
        if content.startswith("ref:"):
            ref = content[4:].strip()
            ref_path = source_root / ".git" / ref
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip() or None
            packed = source_root / ".git" / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split()[0]
            return None
        return content
    except OSError:
        return None


def inspect_source(source_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "missing",
        "source_root": None if source_root is None else str(source_root),
        "present": [],
        "missing": [],
        "interfaces": [],
    }
    if source_root is None:
        return result
    root = Path(source_root)
    result["source_root"] = str(root)
    if not root.exists() or not root.is_dir():
        for entry in REQUIRED_INTERFACES:
            result["missing"].append(entry["id"])
        return result

    pinned = load_pinned()
    interfaces = _get_interfaces(pinned)
    present: list[str] = []
    missing: list[str] = []
    details: list[dict[str, Any]] = []
    for entry in interfaces:
        interface = _normalise_interface(entry)
        relative = interface["file"]
        path = root / relative if relative else root
        file_exists = path.is_file()
        matched = False
        if file_exists:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            matched = re.search(interface["pattern"], text) is not None
        ok = bool(file_exists and matched)
        if ok:
            present.append(interface["id"])
        else:
            missing.append(interface["id"])
        details.append(
            {
                "id": interface["id"],
                "file": relative,
                "present": ok,
            }
        )
    result["present"] = present
    result["missing"] = missing
    result["interfaces"] = details
    total = len(interfaces)
    if total == 0 or len(present) == 0:
        result["state"] = "missing"
    elif len(present) == total:
        result["state"] = "complete"
    else:
        result["state"] = "partial"
    return result


def is_complete(source_root: str | os.PathLike[str] | None) -> bool:
    return inspect_source(source_root).get("state") == "complete"


def check(
    source_root: str | os.PathLike[str] | None = None,
    pinned_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    pinned = load_pinned(pinned_path)

    commit = _get_commit(pinned)
    if not commit:
        errors.append("pinned kernel source commit is missing")
    elif len(commit) != 40:
        errors.append(f"pinned kernel source commit is not a 40-char hash: {commit!r}")

    tag = pinned.get("pinned_tag") or pinned.get("tag") or pinned.get("linux_next_tag")
    if not tag:
        errors.append("pinned linux-next tag is missing")

    interfaces = _get_interfaces(pinned)
    if not interfaces:
        errors.append("no required interfaces declared")
    for entry in interfaces:
        interface = _normalise_interface(entry)
        if not interface["file"]:
            errors.append(f"required interface {interface['id']} has no source file")
        if not interface["pattern"]:
            errors.append(f"required interface {interface['id']} has no match pattern")

    root: Path | None = None
    if source_root is not None:
        root = Path(source_root)
    else:
        env_root = os.environ.get("KERNEL_SOURCE_ROOT") or os.environ.get("LINUX_SOURCE_ROOT")
        if env_root:
            root = Path(env_root)

    if root is not None:
        if not root.exists() or not root.is_dir():
            errors.append(f"kernel source root not found: {root}")
        else:
            inspection = inspect_source(root)
            for missing in inspection.get("missing", []):
                errors.append(f"missing required interface: {missing}")
            head = _git_head(root)
            if head is not None and head != commit:
                errors.append(f"kernel source HEAD {head} != pinned commit {commit}")

    return errors


validate = check
verify = check
validate_source = check
verify_source = check


def source_state(source_root: str | os.PathLike[str] | None = None) -> str:
    return str(inspect_source(source_root).get("state", "missing"))


def classify_source(source_root: str | os.PathLike[str] | None = None) -> str:
    return source_state(source_root)


def kernel_source_state(source_root: str | os.PathLike[str] | None = None) -> str:
    return source_state(source_root)


def get_source_state(source_root: str | os.PathLike[str] | None = None) -> str:
    return source_state(source_root)


def resolve(source_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    return {
        "pinned_commit": PINNED_COMMIT,
        "pinned_tag": PINNED_TAG,
        "state": source_state(source_root),
        "errors": check(source_root),
        "inspection": inspect_source(source_root) if source_root is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the pinned kernel source gate.")
    parser.add_argument("--source-root", help="Path to a kernel source tree to validate.")
    parser.add_argument("--pinned-file", help="Path to an alternate pinned data file.")
    parser.add_argument("--print-pinned", action="store_true", help="Print the pinned commit and exit.")
    args = parser.parse_args(argv)

    if args.print_pinned:
        print(f"{PINNED_COMMIT} {PINNED_TAG}")
        return 0

    pinned_path = args.pinned_file
    errors = check(args.source_root, pinned_path)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("kernel source gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
