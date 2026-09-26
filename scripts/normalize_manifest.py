#!/usr/bin/env python3
"""Normalize release manifests before caching or applying them.

Release YAML sometimes contains shell-style defaults intended for a shell
wrapper. Kubernetes receives those strings literally, so this tool resolves
only the documented `${NAME:=default}` form without evaluating arbitrary code.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:=([^}]*)\}")


def normalize(text: str) -> str:
    return DEFAULT.sub(lambda match: match.group(1), text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(
        normalize(args.source.read_text(encoding="utf-8")), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
