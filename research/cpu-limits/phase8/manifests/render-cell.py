#!/usr/bin/env python3
"""Render and validate one campaign cell manifest from the workload template.

The renderer is the only place that decides a cell's resource values. Keeping
it separate means the campaign runner cannot invent a shape the analysis does
not expect, and a reviewer can reproduce any cell's manifest with one command.

Substitution is strict: an unknown placeholder is a hard error, and a
placeholder left unfilled in the output is never acceptable, because both cases
silently produce a workload the analysis would then treat as measured.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).with_name("workload-template.yaml")

# Every placeholder the template may contain, with the validation applied to it.
PLACEHOLDERS = {
    "CELL": r"^[a-z0-9][a-z0-9-]{0,40}$",
    "MIX": r"^(read-heavy|write-heavy|balanced|cpu-heavy)$",
    "CPU_ITERATIONS": r"^[1-9][0-9]{2,7}$",
    "POSTGRES_POOL_SIZE": r"^[1-9][0-9]?$",
    "REDIS_POOL_SIZE": r"^[1-9][0-9]?$",
    "TRACE_SAMPLE_RATIO": r"^(0(\.\d+)?|1(\.0+)?)$",
    # Kubernetes CPU quantities in millicores or whole cores.
    "CPU_REQUEST": r"^[1-9][0-9]{1,4}m$",
    "CPU_LIMIT": r"^[1-9][0-9]{1,4}m$",
    "SIDECAR_CPU_REQUEST": r"^[1-9][0-9]{1,4}m$",
    "SIDECAR_CPU_LIMIT": r"^[1-9][0-9]{1,4}m$",
}

PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Z_]+)\}\}")


class RenderError(Exception):
    """A manifest cannot be produced from these values."""


def parse_quantity_millicores(value: str) -> int:
    """Convert a Kubernetes CPU quantity to millicores.

    Only the forms the template permits are accepted, so the model never sees a
    value it cannot compare against a measured millicore limit. A bare millicore
    count below one core is rejected on purpose: `500` means half a core to
    Kubernetes, and writing `500` where `500m` was meant is a 1000x error that
    would otherwise look plausible in a result table.
    """
    # Fixed width so a leading zero cannot slip through as a distinct spelling.
    if value.endswith("m"):
        digits = value[:-1]
        if not re.fullmatch(r"[1-9][0-9]*", digits):
            raise RenderError(f"unsupported CPU quantity: {value!r}")
        return int(digits)
    if re.fullmatch(r"[1-9][0-9]*", value):
        return int(value) * 1000
    raise RenderError(f"unsupported CPU quantity: {value!r}")


def render(values: dict[str, str], template: Path = TEMPLATE) -> str:
    """Substitute validated values into the template.

    Raises on an unknown key, a missing key, or a value that fails its pattern.
    """
    unknown = sorted(set(values) - set(PLACEHOLDERS))
    if unknown:
        raise RenderError(f"unknown placeholder(s): {', '.join(unknown)}")

    missing = sorted(set(PLACEHOLDERS) - set(values))
    if missing:
        raise RenderError(f"missing placeholder(s): {', '.join(missing)}")

    for key, pattern in PLACEHOLDERS.items():
        if not re.fullmatch(pattern, values[key]):
            raise RenderError(
                f"placeholder {key} has invalid value {values[key]!r}; "
                f"expected {pattern}"
            )

    cpu_request = parse_quantity_millicores(values["CPU_REQUEST"])
    cpu_limit = parse_quantity_millicores(values["CPU_LIMIT"])
    if cpu_request > cpu_limit:
        raise RenderError(
            f"CPU_REQUEST ({cpu_request}m) exceeds CPU_LIMIT ({cpu_limit}m); "
            "Kubernetes rejects this and the cell would never start"
        )

    sidecar_request = parse_quantity_millicores(values["SIDECAR_CPU_REQUEST"])
    sidecar_limit = parse_quantity_millicores(values["SIDECAR_CPU_LIMIT"])
    if sidecar_request > sidecar_limit:
        raise RenderError(
            f"SIDECAR_CPU_REQUEST ({sidecar_request}m) exceeds "
            f"SIDECAR_CPU_LIMIT ({sidecar_limit}m)"
        )

    text = template.read_text()

    def substitute(match: re.Match[str]) -> str:
        return values[match.group(1)]

    rendered = PLACEHOLDER_PATTERN.sub(substitute, text)

    leftover = PLACEHOLDER_PATTERN.findall(rendered)
    if leftover:
        raise RenderError(
            f"unsubstituted placeholder(s): {', '.join(sorted(set(leftover)))}"
        )

    return rendered


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--values",
        required=True,
        help="JSON object mapping placeholder names to values, or '-' for stdin",
    )
    parser.add_argument(
        "--output",
        help="Write the manifest here instead of stdout",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.values == "-" else Path(args.values).read_text()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as err:
        print(f"values file is not valid JSON: {err}", file=sys.stderr)
        return 2

    if not isinstance(values, dict):
        print("values must be a JSON object", file=sys.stderr)
        return 2

    try:
        rendered = render({str(k): str(v) for k, v in values.items()})
    except RenderError as err:
        print(f"cannot render cell: {err}", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
