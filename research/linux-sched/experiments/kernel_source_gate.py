"""Kernel source gate implementation (re-exported from scripts/)."""

from __future__ import annotations

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "kernel_source_gate.py"
exec(compile(_SCRIPT.read_text(encoding="utf-8"), str(_SCRIPT), "exec"))  # noqa: S102
