"""Alias for the kernel source gate implementation."""

from __future__ import annotations

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "kernel_source_gate.py"
exec(compile(_SCRIPT.read_text(encoding="utf-8"), str(_SCRIPT), "exec"))  # noqa: S102
