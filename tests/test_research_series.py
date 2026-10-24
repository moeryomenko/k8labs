"""Contracts for the research series boundary.

The approved plan replaces the retired `cpu-sched` suite with independent
series. These tests assert the boundary holds: every series has a Taskfile,
the research aggregator discovers them, and no active reference to the retired
series remains.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / "research"
SERIES = ("linux-sched", "kube-sched", "sched-core-modes")

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


def _taskfile(series: str) -> Path:
    return RESEARCH / series / "Taskfile.yml"


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def test_every_series_has_a_taskfile() -> None:
    for series in SERIES:
        assert _taskfile(series).is_file(), series


def test_aggregator_discovers_every_series() -> None:
    includes = _load(RESEARCH / "Taskfile.yml").get("includes", {})
    assert isinstance(includes, dict)
    for series in SERIES:
        assert series in includes, includes
        assert includes[series]["flatten"] is True


def test_every_series_exposes_only_its_prefixed_tasks() -> None:
    for series in SERIES:
        tasks = _load(_taskfile(series)).get("tasks", {})
        assert tasks, series
        for name in tasks:
            assert name.startswith(f"{series}-"), (series, name)


def test_help_lists_only_the_new_series() -> None:
    root = _load(ROOT / "Taskfile.yml")
    tasks = root.get("tasks", {})
    assert tasks["default"]["aliases"] == ["help"]
    for series in SERIES:
        assert any(
            name.startswith(f"{series}-")
            for name in _load(_taskfile(series)).get("tasks", {})
        )
    assert not any("cpu-sched" in name for name in tasks)


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
    linux = _taskfile("linux-sched").read_text(encoding="utf-8")
    kube = _taskfile("kube-sched").read_text(encoding="utf-8")
    sched = _taskfile("sched-core-modes").read_text(encoding="utf-8")
    assert "linux-sched-" in linux and "kube-sched-" not in linux
    assert "kube-sched-" in kube and "linux-sched-" not in kube
    assert "sched-core-modes-" in sched
    assert "linux-sched-" not in sched and "kube-sched-" not in sched
