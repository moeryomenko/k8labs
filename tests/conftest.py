"""Shared subprocess and fixture helpers for the node tooling CLI tests.

The CLI under test is ``scripts/nodes.py``; every test invokes it as a
subprocess with the repo venv interpreter so exit codes, stdout, and stderr
shape are pinned exactly. Tests that exercise write-back only ever mutate
copies of the committed fixtures in pytest's ``tmp_path``; the fixtures
themselves and any live tfvars state are never touched.

Helpers shared across the test modules live here as fixtures:
``run_cli`` runs the CLI with the given arguments, ``copy_fixture`` copies a
committed fixture into tmp_path, and ``write_tfvars`` renders a generated
tfvars document (used for parametrized name/mac variants and bulk pools).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so `-m` selection works without warnings."""
    config.addinivalue_line(
        "markers",
        "network: needs registry.opentofu.org reachability "
        "(deselect with -m 'not network')",
    )


CLI = REPO_ROOT / "scripts" / "nodes.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def render_tfvars(
    control_plane: dict[str, object],
    workers: list[dict[str, object]],
) -> str:
    """Render a deterministic tfvars document from node field dicts.

    Field order is name, cpu, ram, disk, mac; absent keys (an optional mac)
    are omitted. The output mirrors the committed fixture style and parses
    with the pinned hcl2 package.
    """

    def render_entry(fields: dict[str, object], prefix: str = "") -> str:
        lines = [f"{prefix}{{"]
        for key in ("name", "cpu", "ram", "disk", "mac"):
            if key not in fields:
                continue
            value = fields[key]
            if isinstance(value, str):
                lines.append(f'  {key}  = "{value}"')
            else:
                lines.append(f"  {key}  = {value}")
        lines.append("}")
        return "\n".join(lines)

    blocks = ["control_plane = " + render_entry(control_plane)]
    if workers:
        inner = ",\n".join(render_entry(worker) for worker in workers)
        blocks.append("workers = [\n" + inner + "\n]")
    else:
        blocks.append("workers = []")
    return "\n\n".join(blocks) + "\n"


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run scripts/nodes.py with the given args from the repo root."""

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=30,
            check=False,
        )

    return _run


@pytest.fixture
def copy_fixture(tmp_path: Path) -> Callable[[str], Path]:
    """Copy a committed fixture into tmp_path; tests only mutate the copy."""

    def _copy(name: str) -> Path:
        dest = tmp_path / name
        dest.write_bytes((FIXTURES / name).read_bytes())
        return dest

    return _copy


@pytest.fixture
def write_tfvars(tmp_path: Path) -> Callable[..., Path]:
    """Render a generated tfvars document into tmp_path and return its path."""

    def _write(
        control_plane: dict[str, object],
        workers: list[dict[str, object]],
        name: str = "generated.tfvars",
    ) -> Path:
        dest = tmp_path / name
        dest.write_text(render_tfvars(control_plane, workers), encoding="utf-8")
        return dest

    return _write
