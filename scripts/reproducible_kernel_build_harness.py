"""Alias for the reproducible kernel build harness."""

from __future__ import annotations

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "kernel_build_harness.py"
exec(compile(_SCRIPT.read_text(encoding="utf-8"), str(_SCRIPT), "exec"))  # noqa: S102
