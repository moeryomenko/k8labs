"""Tests for the kernel source gate (research location shim)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENTS))

_spec = importlib.util.spec_from_file_location("kernel_source_gate", EXPERIMENTS / "kernel_source_gate.py")
assert _spec and _spec.loader
ksg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ksg)


def test_pinned_commit() -> None:
    assert ksg.PINNED_COMMIT == "2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64"


def test_repository_gate_passes() -> None:
    assert ksg.check() == []


def test_complete_source_state(tmp_path) -> None:
    root = tmp_path / "src"
    (root / "include/uapi/linux").mkdir(parents=True)
    (root / "arch/x86/net").mkdir(parents=True)
    (root / "kernel/sched/ext").mkdir(parents=True)
    (root / "include/uapi/linux/bpf.h").write_text("#define BPF_F_ARENA_SCALAR 1\n")
    (root / "arch/x86/net/bpf_jit_comp.c").write_text("bpf_jit_supports_arena_scalar\n")
    (root / "kernel/sched/ext/internal.h").write_text("sub_cid_sched_updated\n")
    assert ksg.source_state(root) == "complete"
