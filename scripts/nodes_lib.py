"""Shared node-parsing library for the k8labs tfvars file.

Parses the HCL tfvars input (``control_plane`` block + ``workers`` list) with
the pinned ``python-hcl2`` package and exposes a stable node model used by the
CLI (``nodes.py``) and downstream generators (network configuration, disk
reconciliation). ``mac`` is optional per node and defaults to an empty string.
"""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

import hcl2

#: Maximum number of nodes the DHCP pool can address (``PoolSize`` in
#: ``network/k8sbr0.network``); the control plane counts toward it.
DHCP_POOL_CAPACITY = 181

#: MAC family auto-assignment stays inside this locally administered unicast
#: prefix; addresses are ``<prefix>:<NN>`` with NN in ``01..fe``.
MAC_FAMILY_PREFIX = "c6:e5:50:1c:ec"

#: Last assignable last-octet of the MAC family (``ec:ff`` is never assigned).
MAX_FAMILY_OCTET = 0xFE

_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
_MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


class TfvarsError(Exception):
    """Raised when a tfvars file cannot be read or parsed."""


@dataclass(frozen=True)
class Node:
    """A single cluster node parsed from the tfvars file."""

    name: str
    role: str
    cpu: int
    ram: int
    disk: int
    mac: str


def _unquote(value: object) -> str:
    """Return a string value with python-hcl2's literal quotes stripped.

    The pinned python-hcl2 8.1.2 returns string tokens still wrapped in double
    quotes (``"cp1"`` becomes ``'"cp1"'``); numeric values pass through as
    their string representation.
    """
    if not isinstance(value, str):
        return str(value)
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _as_int(value: object, key: str, role: str, path: Path) -> int:
    """Coerce a parsed tfvars field to int, failing loudly when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TfvarsError(
            f"tfvars {path}: {role} entry field {key!r} must be a number, got {value!r}"
        )
    return int(value)


def _parse_node(entry: dict[str, object], role: str, path: Path) -> Node:
    """Build a Node from one tfvars entry, failing loudly on bad fields."""
    try:
        return Node(
            name=_unquote(entry["name"]),
            role=role,
            cpu=_as_int(entry["cpu"], "cpu", role, path),
            ram=_as_int(entry["ram"], "ram", role, path),
            disk=_as_int(entry["disk"], "disk", role, path),
            mac=_unquote(entry.get("mac", "")),
        )
    except KeyError as exc:
        raise TfvarsError(
            f"tfvars {path}: {role} entry missing required field {exc.args[0]!r}"
        ) from exc


def _nodes_from(raw: dict[str, object], path: Path) -> list[Node]:
    """Convert the raw hcl2 result into an ordered node list."""
    control_plane = raw.get("control_plane")
    workers = raw.get("workers", [])
    if not isinstance(control_plane, dict):
        raise TfvarsError(f"tfvars {path}: missing or invalid control_plane block")
    if not isinstance(workers, list):
        raise TfvarsError(f"tfvars {path}: workers must be a list")
    nodes = [_parse_node(cast(dict[str, object], control_plane), "control-plane", path)]
    for worker in workers:
        if not isinstance(worker, dict):
            raise TfvarsError(
                f"tfvars {path}: worker entry is not an object: {worker!r}"
            )
        nodes.append(_parse_node(cast(dict[str, object], worker), "worker", path))
    return nodes


def load_nodes(tfvars_path: str | Path) -> list[Node]:
    """Parse the tfvars file and return nodes in display order.

    The control-plane node comes first, then workers in tfvars order. Raises
    TfvarsError when the file is missing or cannot be parsed.
    """
    path = Path(tfvars_path)
    if not path.is_file():
        raise TfvarsError(f"tfvars file not found: {path}")
    try:
        with path.open(encoding="utf-8") as tfvars_file:
            raw = cast(dict[str, object], hcl2.load(tfvars_file))
    except TfvarsError:
        raise
    except Exception as exc:
        raise TfvarsError(f"failed to parse tfvars {path}: {exc}") from exc
    return _nodes_from(raw, path)


def nodes_to_json(nodes: list[Node]) -> str:
    """Serialize nodes to a JSON array document (one object per node)."""
    return json.dumps([asdict(node) for node in nodes], indent=2)


class NodeValidationError(Exception):
    """Raised when validation finds violations and the operation must be refused.

    Carries the full list of human-readable violation messages so the CLI can
    print every one to stderr.
    """

    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


class MacAssignmentError(Exception):
    """Raised when no assignable address remains in the MAC family."""


def _is_valid_name(name: str) -> bool:
    """Return whether name satisfies Kubernetes label rules.

    Lowercase alphanumerics, ``-``/``.`` inside, starts and ends alphanumeric,
    at most 63 characters.
    """
    return len(name) <= 63 and _NAME_PATTERN.fullmatch(name) is not None


def _is_valid_mac(mac: str) -> bool:
    """Return whether mac is well-formed lowercase hex and locally administered unicast."""
    if _MAC_PATTERN.fullmatch(mac) is None:
        return False
    first_octet = int(mac[:2], 16)
    return first_octet & 0x01 == 0 and first_octet & 0x02 != 0


def validate_nodes(nodes: list[Node]) -> list[str]:
    """Return a human-readable violation message per problem found.

    Checks duplicate names (across roles), duplicate non-empty MACs, node
    names against Kubernetes label rules, MAC format plus locally administered
    unicast bits, and total node count against the DHCP pool capacity. A
    missing mac is never a violation. The returned list is empty for a clean
    file.
    """
    violations: list[str] = []
    names_seen: dict[str, str] = {}
    for node in nodes:
        if node.name in names_seen:
            violations.append(
                f"duplicate node name: {node.name!r} "
                f"({names_seen[node.name]} and {node.role})"
            )
        else:
            names_seen[node.name] = node.role
    for node in nodes:
        if not _is_valid_name(node.name):
            violations.append(
                f"invalid node name {node.name!r}: must be lowercase alphanumeric "
                "with optional '-'/'.' inside, start/end alphanumeric, max 63 chars"
            )
    for node in nodes:
        if node.mac and not _is_valid_mac(node.mac):
            violations.append(
                f"invalid MAC {node.mac!r} on node {node.name!r}: must be "
                "xx:xx:xx:xx:xx:xx lowercase hex, locally administered unicast"
            )
    macs_seen: set[str] = set()
    for node in nodes:
        if not node.mac:
            continue
        if node.mac in macs_seen:
            violations.append(f"duplicate MAC address: {node.mac}")
        else:
            macs_seen.add(node.mac)
    if len(nodes) > DHCP_POOL_CAPACITY:
        violations.append(
            f"node count {len(nodes)} exceeds DHCP pool capacity {DHCP_POOL_CAPACITY}"
        )
    return violations


def assign_missing_macs(nodes: list[Node]) -> list[Node]:
    """Return nodes with every missing mac filled in display order.

    The next assigned address is one greater than the largest last octet
    among existing MACs in the MAC family (foreign-family MACs are ignored),
    rendered as two lowercase hex digits. Multiple missing nodes are assigned
    sequentially in node order. Raises MacAssignmentError when the next
    address would exceed the last assignable family octet.
    """
    family_octets = [
        int(node.mac.rsplit(":", 1)[1], 16)
        for node in nodes
        if node.mac.startswith(f"{MAC_FAMILY_PREFIX}:")
    ]
    next_octet = max(family_octets, default=0) + 1
    filled: list[Node] = []
    for node in nodes:
        if node.mac:
            filled.append(node)
            continue
        if next_octet > MAX_FAMILY_OCTET:
            raise MacAssignmentError(
                f"cannot assign a MAC to node {node.name!r}: "
                f"{MAC_FAMILY_PREFIX} family exhausted "
                f"(last assignable address {MAC_FAMILY_PREFIX}:fe)"
            )
        filled.append(replace(node, mac=f"{MAC_FAMILY_PREFIX}:{next_octet:02x}"))
        next_octet += 1
    return filled


def _insert_mac_line(text: str, node_name: str, mac: str) -> str:
    """Return text with a mac field line inserted into the named node's block.

    Locates the ``name = "<node_name>"`` field line, then inserts the mac line
    with matching indentation immediately before the block's closing brace.
    All other lines are returned byte-for-byte.
    """
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf'^\s*name\s*=\s*"{re.escape(node_name)}"\s*(?:#.*)?$')
    name_index = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if name_index is None:
        raise TfvarsError(
            f"could not locate the block for node {node_name!r} in tfvars"
        )
    indent = lines[name_index][
        : len(lines[name_index]) - len(lines[name_index].lstrip())
    ]
    for index in range(name_index, len(lines)):
        if lines[index].strip().startswith("}"):
            lines.insert(index, f'{indent}mac  = "{mac}"\n')
            return "".join(lines)
    raise TfvarsError(
        f"could not find the end of the block for node {node_name!r} in tfvars"
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via a same-directory temp file and os.replace."""
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(content)
        os.chmod(temp_name, path.stat().st_mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def fill_macs(tfvars_path: str | Path) -> list[Node]:
    """Assign family MACs to every node missing one and write the file back.

    Validates the file first and refuses without writing on any violation.
    Assignments are computed in memory; only then is the file rewritten
    atomically (temp file + os.replace), preserving untouched blocks
    byte-for-byte. Returns the resulting nodes in display order. Raises
    TfvarsError on missing/malformed input, NodeValidationError on
    violations, or MacAssignmentError when the family is exhausted.
    """
    path = Path(tfvars_path)
    nodes = load_nodes(path)
    violations = validate_nodes(nodes)
    if violations:
        raise NodeValidationError(violations)
    filled = assign_missing_macs(nodes)
    if filled == nodes:
        return filled
    text = path.read_text(encoding="utf-8")
    new_text = text
    for node, filled_node in zip(nodes, filled):
        if node.mac != filled_node.mac:
            new_text = _insert_mac_line(new_text, node.name, filled_node.mac)
    _atomic_write(path, new_text)
    return filled
