#!/usr/bin/env python3
"""Converge a systemd-networkd directory to the generated node TAP configs.

Reads the managed network files (``k8s-*.netdev`` and ``k8s-*.network``) from
``SOURCE_DIR`` and makes ``TARGET_DIR`` match that set: every managed file is
installed byte-identical (created or overwritten), and every managed file in
the target whose basename is absent from the source is removed. Files outside
the managed set (``packer-tap.*``, ``k8sbr0.*``, unrelated files) and
directories in either directory are never touched.

The source directory must exist; a missing source (or a path that is not a
directory) is reported on stderr with a non-zero exit. The target directory
is created with any missing parents on demand. An empty source directory
installs nothing and removes every managed file from the target, which is the
teardown mechanism ``network-down`` relies on.
"""

import argparse
import shutil
import sys
from pathlib import Path

MANAGED_PREFIX = "k8s-"
MANAGED_SUFFIXES = (".netdev", ".network")


def is_managed(name: str) -> bool:
    """Return whether a basename belongs to the managed file set."""
    return name.startswith(MANAGED_PREFIX) and name.endswith(MANAGED_SUFFIXES)


def managed_files(directory: Path) -> dict[str, Path]:
    """Map basename to path for every managed regular file in a directory."""
    return {
        entry.name: entry
        for entry in directory.iterdir()
        if entry.is_file() and is_managed(entry.name)
    }


def reconcile(source_dir: Path, target_dir: Path) -> None:
    """Install managed source files into the target and drop stale ones."""
    target_dir.mkdir(parents=True, exist_ok=True)
    desired = managed_files(source_dir)
    for name, source in desired.items():
        shutil.copyfile(source, target_dir / name)
    for name, target in managed_files(target_dir).items():
        if name not in desired:
            target.unlink()


def main(argv: list[str] | None = None) -> int:
    """Run the reconcile CLI and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="network-reconcile.py",
        description=(
            "Converge a systemd-networkd directory to the generated node TAP configs."
        ),
    )
    parser.add_argument(
        "source_dir", help="directory with generated k8s-* network configs"
    )
    parser.add_argument("target_dir", help="systemd-networkd directory to converge")
    args = parser.parse_args(argv)

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        print(
            f"network-reconcile: source directory not found: {args.source_dir}",
            file=sys.stderr,
        )
        return 1
    if not source_dir.is_dir():
        print(
            f"network-reconcile: source path is not a directory: {args.source_dir}",
            file=sys.stderr,
        )
        return 1

    reconcile(source_dir, Path(args.target_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
