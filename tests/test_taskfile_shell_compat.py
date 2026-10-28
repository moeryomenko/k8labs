"""Guard Taskfile recipes against shell options the internal Task shell rejects."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TASKFILES = sorted(ROOT.rglob("Taskfile*.yml"))
ERRTRACE = re.compile(r"(?m)^\s*set\s+-[A-Za-z]*E|set\s+-o\s+errtrace")


@pytest.mark.parametrize("path", TASKFILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_recipes_do_not_enable_errtrace(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert ERRTRACE.search(text) is None, (
        f"{path.relative_to(ROOT)} enables errtrace; Task's internal shell "
        "rejects it, use `set -euo pipefail` instead"
    )
