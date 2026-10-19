"""Tests for the kernel source gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "kernel_source_gate.py"

_spec = importlib.util.spec_from_file_location("kernel_source_gate", SCRIPT)
assert _spec and _spec.loader
ksg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ksg)


def _make_source(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_pinned_commit_is_linux_next_head() -> None:
    assert ksg.PINNED_COMMIT == "2efb7a11f1aa4d2dc285fe2680cca22cb9f72f64"
    assert ksg.PINNED_TAG == "next-20261016"
    assert len(ksg.PINNED_COMMIT) == 40


def test_required_interfaces_declared() -> None:
    ids = {entry["id"] for entry in ksg.REQUIRED_INTERFACES}
    assert "bpf_f_arena_scalar" in ids
    assert "bpf_jit_supports_arena_scalar_x86" in ids
    assert "sched_ext_ops_sub_cid_sched_updated" in ids


def test_repository_pinned_data_matches_constants() -> None:
    data = ksg.load_pinned()
    assert ksg._get_commit(data) == ksg.PINNED_COMMIT
    assert data["pinned_tag"] == ksg.PINNED_TAG
    assert ksg.check() == []


def test_missing_source_state(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ksg.source_state(empty) == "missing"
    assert ksg.check(empty) != []


def test_partial_source_state(tmp_path: Path) -> None:
    partial = tmp_path / "partial"
    _make_source(
        partial,
        {
            "include/uapi/linux/bpf.h": "#define BPF_F_ARENA_SCALAR (1U << 8)\n",
        },
    )
    assert ksg.source_state(partial) == "partial"
    errors = ksg.check(partial)
    assert errors
    assert any("missing required interface" in e for e in errors)


def test_complete_source_state(tmp_path: Path) -> None:
    complete = tmp_path / "complete"
    _make_source(
        complete,
        {
            "include/uapi/linux/bpf.h": "#define BPF_F_ARENA_SCALAR (1U << 8)\n",
            "arch/x86/net/bpf_jit_comp.c": "bool bpf_jit_supports_arena_scalar(void) { return true; }\n",
            "kernel/sched/ext/internal.h": "void (*sub_cid_sched_updated)(s32 cid, u64 sched);\n",
        },
    )
    assert ksg.source_state(complete) == "complete"
    assert ksg.is_complete(complete) is True
    assert ksg.check(complete) == []


def test_missing_state_for_nonexistent_root(tmp_path: Path) -> None:
    assert ksg.source_state(tmp_path / "does-not-exist") == "missing"
    assert ksg.inspect_source(tmp_path / "does-not-exist")["missing"]


def test_none_source_is_missing() -> None:
    assert ksg.source_state(None) == "missing"
    assert ksg.inspect_source(None)["state"] == "missing"


def test_classify_and_resolve_aliases(tmp_path: Path) -> None:
    complete = tmp_path / "complete"
    _make_source(
        complete,
        {
            "include/uapi/linux/bpf.h": "#define BPF_F_ARENA_SCALAR (1U << 8)\n",
            "arch/x86/net/bpf_jit_comp.c": "bpf_jit_supports_arena_scalar\n",
            "kernel/sched/ext/internal.h": "sub_cid_sched_updated\n",
        },
    )
    assert ksg.classify_source(complete) == "complete"
    assert ksg.kernel_source_state(complete) == "complete"
    assert ksg.get_source_state(complete) == "complete"
    result = ksg.resolve(complete)
    assert result["state"] == "complete"
    assert result["errors"] == []


def test_git_head_mismatch_is_reported(tmp_path: Path) -> None:
    complete = tmp_path / "complete"
    _make_source(
        complete,
        {
            "include/uapi/linux/bpf.h": "#define BPF_F_ARENA_SCALAR (1U << 8)\n",
            "arch/x86/net/bpf_jit_comp.c": "bpf_jit_supports_arena_scalar\n",
            "kernel/sched/ext/internal.h": "sub_cid_sched_updated\n",
        },
    )
    # Create a fake git repo with a different HEAD.
    git_dir = complete / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    refs = git_dir / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("0" * 40 + "\n", encoding="utf-8")
    errors = ksg.check(complete)
    assert any("kernel source HEAD" in e for e in errors)


def test_pinned_data_file_loads() -> None:
    data = ksg.load_pinned(ROOT / "kernel_source_pinned.yaml")
    assert ksg._get_commit(data) == ksg.PINNED_COMMIT

    data_json = ksg.load_pinned(ROOT / "kernel_source_pinned.json")
    assert ksg._get_commit(data_json) == ksg.PINNED_COMMIT


def test_main_print_pinned(capsys: pytest.CaptureFixture[str]) -> None:
    rc = ksg.main(["--print-pinned"])
    assert rc == 0
    captured = capsys.readouterr()
    assert ksg.PINNED_COMMIT in captured.out


def test_main_complete_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    complete = tmp_path / "complete"
    _make_source(
        complete,
        {
            "include/uapi/linux/bpf.h": "#define BPF_F_ARENA_SCALAR (1U << 8)\n",
            "arch/x86/net/bpf_jit_comp.c": "bpf_jit_supports_arena_scalar\n",
            "kernel/sched/ext/internal.h": "sub_cid_sched_updated\n",
        },
    )
    rc = ksg.main(["--source-root", str(complete)])
    assert rc == 0
