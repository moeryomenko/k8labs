"""Contracts for the research series boundary.

The approved plan replaces the retired `cpu-sched` suite with two independent
series, `linux-sched` and `kube-sched`. These tests assert the boundary holds:
both series exist and are discovered by the aggregator, and no active reference
to the retired series remains.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / "research"
SERIES = ("linux-sched", "kube-sched")

# Files whose contents must not reference the retired suite. `.git` history and
# historical notes are intentionally excluded from this check.
_SKIP_DIRS = {".git", "build", "__pycache__", ".venv", "node_modules"}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in RESEARCH.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def test_both_series_makefiles_exist() -> None:
    for series in SERIES:
        assert (RESEARCH / series / f"{series}.mk").is_file(), series


def test_aggregator_discovers_both_series() -> None:
    makefile = (RESEARCH / "Makefile").read_text(encoding="utf-8")
    assert "include $(wildcard */*.mk)" in makefile


def test_make_help_lists_only_the_new_series() -> None:
    result = subprocess.run(
        ["make", "-C", str(RESEARCH), "help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Strip ANSI colour codes: `make help` wraps target names in `\x1b[36m...`,
    # and the trailing `m` would otherwise defeat a `\b`-anchored match.
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    # `help` renders `  <target><pad> <description>` (no colon).
    targets = set(re.findall(r"^\s{2}([A-Za-z0-9_./-]+)\s{2,}\S", output, re.MULTILINE))
    assert any(t.startswith("linux-sched-") for t in targets), targets
    assert any(t.startswith("kube-sched-") for t in targets), targets
    assert not any(t.startswith("cpu-sched-") for t in targets), targets


def test_retired_series_directory_is_gone() -> None:
    assert not (RESEARCH / "cpu-sched").exists()


def test_no_active_reference_to_retired_series() -> None:
    offenders: list[str] = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "cpu-sched" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"active cpu-sched references remain: {offenders}"


def test_series_are_independent() -> None:
    linux = (RESEARCH / "linux-sched" / "linux-sched.mk").read_text(encoding="utf-8")
    kube = (RESEARCH / "kube-sched" / "kube-sched.mk").read_text(encoding="utf-8")
    assert "linux-sched-" in linux and "kube-sched-" not in linux
    assert "kube-sched-" in kube and "linux-sched-" not in kube
