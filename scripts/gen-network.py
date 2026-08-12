#!/usr/bin/env python3
"""Generate systemd-networkd TAP configuration for every cluster node.

Reads the tfvars file through the shared node parser and writes one
``.netdev``/``.network`` pair per node (control-plane first, then workers in
tfvars order) into the target directory:

- ``k8s-<node>.netdev`` — a ``[NetDev]`` section with ``Name=k8s-<node>``
  and ``Kind=tap``
- ``k8s-<node>.network`` — a ``[Match]`` section with ``Name=k8s-<node>``
  plus a ``[Network]`` section with ``Bridge=k8sbr0``

The output directory is created with any missing parents and rebuilt from
scratch on every run, so stale or unrelated content never lingers. A missing
or malformed tfvars file is reported on stderr with a non-zero exit; an
output directory that cannot be removed is likewise a hard error and fails
with the underlying cause (e.g. a permission error) on stderr.
"""

import argparse
import shutil
import sys
from pathlib import Path

from nodes_lib import TfvarsError, load_nodes

BRIDGE_NAME = "k8sbr0"


def netdev_document(node: str) -> str:
    """Return the .netdev file body for a node (ends with a single newline)."""
    return f"[NetDev]\nName=k8s-{node}\nKind=tap\n"


def network_document(node: str) -> str:
    """Return the .network file body for a node (ends with a single newline)."""
    return f"[Match]\nName=k8s-{node}\n\n[Network]\nBridge={BRIDGE_NAME}\n"


def write_configs(tfvars_path: str | Path, output_dir: Path) -> None:
    """Recreate the output directory and write one pair per node."""
    nodes = load_nodes(tfvars_path)
    try:
        shutil.rmtree(output_dir)
    except FileNotFoundError:
        # Nothing to remove on a fresh run; only a missing directory is
        # tolerated, any other removal failure propagates to the CLI.
        pass
    output_dir.mkdir(parents=True)
    for node in nodes:
        output_dir.joinpath(f"k8s-{node.name}.netdev").write_text(
            netdev_document(node.name), encoding="utf-8"
        )
        output_dir.joinpath(f"k8s-{node.name}.network").write_text(
            network_document(node.name), encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the generator CLI and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="gen-network.py",
        description="Generate systemd-networkd TAP configs for cluster nodes.",
    )
    parser.add_argument(
        "--tfvars",
        required=True,
        help="tfvars file to read",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="directory to write the generated configs into",
    )
    args = parser.parse_args(argv)

    try:
        write_configs(args.tfvars, Path(args.output_dir))
    except TfvarsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"error: cannot rebuild output directory {args.output_dir}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
