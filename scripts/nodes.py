#!/usr/bin/env python3
"""Command-line interface for the k8labs node parser.

Subcommands:
    --list          print a JSON array of nodes (control-plane first, then
                    workers in tfvars order)
    --worker-macs   print worker MACs space-separated in tfvars order; fails
                    naming the node when any worker lacks a MAC
    --validate      check the file for violations and list every one on
                    stderr; exit 0 only when the file is clean
    --fill-macs     assign the next MAC in the c6:e5:50:1c:ec family to every
                    node missing one, write the file back atomically, and
                    print the resulting node list as JSON
    --tfvars PATH   tfvars file to read (default: build/deploy.tfvars)
"""

import argparse
import sys
from pathlib import Path

from nodes_lib import (
    MacAssignmentError,
    NodeValidationError,
    TfvarsError,
    fill_macs,
    load_nodes,
    nodes_to_json,
    validate_nodes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TFVARS = REPO_ROOT / "build" / "deploy.tfvars"


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="nodes.py",
        description="Parse the k8labs tfvars file and report cluster nodes.",
    )
    parser.add_argument(
        "--tfvars",
        default=str(DEFAULT_TFVARS),
        help=f"tfvars file to read (default: {DEFAULT_TFVARS})",
    )
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument(
        "--list",
        action="store_true",
        help="print the node list as JSON",
    )
    commands.add_argument(
        "--worker-macs",
        action="store_true",
        help="print worker MACs space-separated in tfvars order",
    )
    commands.add_argument(
        "--validate",
        action="store_true",
        help="list every violation on stderr; exit non-zero when any is found",
    )
    commands.add_argument(
        "--fill-macs",
        action="store_true",
        help="assign missing MACs and write the file back atomically",
    )
    args = parser.parse_args(argv)

    if args.fill_macs:
        try:
            filled = fill_macs(args.tfvars)
        except TfvarsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except NodeValidationError as exc:
            for message in exc.messages:
                print(message, file=sys.stderr)
            return 1
        except MacAssignmentError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(nodes_to_json(filled))
        return 0

    try:
        nodes = load_nodes(args.tfvars)
    except TfvarsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.list:
        print(nodes_to_json(nodes))
        return 0

    if args.validate:
        violations = validate_nodes(nodes)
        for message in violations:
            print(message, file=sys.stderr)
        return 1 if violations else 0

    workers = [node for node in nodes if node.role == "worker"]
    missing = [node.name for node in workers if not node.mac]
    if missing:
        print(
            f"worker(s) missing a MAC address: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    print(" ".join(node.mac for node in workers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
