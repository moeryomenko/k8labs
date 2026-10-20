"""The runner must only enable bandwidth where the kernel can honour it."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parents[1]
LIB = HERE / "lib.sh"
RUNNER = HERE / "run-block.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_capability_probe_uses_the_kernel_btf() -> None:
    lib = _read(LIB)
    assert "kernel_cgroup_bw_supported()" in lib
    assert "bpftool btf dump" in lib
    assert "cgroup_set_bandwidth" in lib


def test_bandwidth_flag_is_enabled_for_arms_that_support_it() -> None:
    runner = _read(RUNNER)
    # lavd and mavd are the only arms that honour cpu.max.
    assert "scx_lavd|scx_mavd)" in runner
    assert "--enable-cpu-bw" in runner
    # The flag must be conditional on the probe, never unconditional.
    guarded = runner.split("scx_lavd|scx_mavd)", 1)[1][:600]
    assert "kernel_cgroup_bw_supported" in guarded
    assert "bw_args+=(--enable-cpu-bw)" in guarded


def test_arms_without_bandwidth_support_do_not_get_the_flag() -> None:
    runner = _read(RUNNER)
    branch = runner.split("scx_bpfland|scx_mlfq)", 1)[1].split(";;", 1)[0]
    assert "--enable-cpu-bw" not in branch
