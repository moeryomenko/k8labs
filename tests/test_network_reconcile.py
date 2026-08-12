"""Contract tests for the systemd-networkd reconciliation tool.

The tool under test is ``scripts/network-reconcile.py``: it converges a
systemd-networkd directory (``TARGET_DIR``) to the set of managed files in a
generated source directory (``SOURCE_DIR``). The CLI takes exactly two
positional arguments, ``SOURCE_DIR`` and ``TARGET_DIR``.

The managed set is defined by a single predicate: a regular file whose
basename starts with ``k8s-`` and ends with either ``.netdev`` or
``.network``. Every managed file in ``SOURCE_DIR`` is installed into
``TARGET_DIR`` byte-identical (created or overwritten). Every managed file in
``TARGET_DIR`` whose basename is absent from the source managed set is
deleted. Nothing else is ever touched: files outside the managed predicate
(``packer-tap.*``, ``k8sbr0.*``, unrelated files of any extension) and
directories in either location are ignored — not installed, not removed, not
counted toward staleness.

A missing ``SOURCE_DIR`` (or a path that exists but is not a directory) exits
non-zero with a clean message on stderr naming the path; stdout stays empty.
A successful run exits 0 with empty stderr (stdout is free-form). An empty
``SOURCE_DIR`` installs nothing and removes every managed file from
``TARGET_DIR``.

Tests invoke the CLI as a subprocess from the repo root and only ever write
into pytest's ``tmp_path`` — no root privileges, no real system directories.
``TARGET_DIR`` is pre-created by every test so behavior on a missing target
directory is left to the implementation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECONCILE = REPO_ROOT / "scripts" / "network-reconcile.py"


def run_reconcile(
    source_dir: Path, target_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Run scripts/network-reconcile.py in the repo venv with the given paths."""
    return subprocess.run(
        [sys.executable, str(RECONCILE), str(source_dir), str(target_dir)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
        check=False,
    )


def netdev(node: str) -> bytes:
    """Realistic .netdev document body for a node."""
    return f"[NetDev]\nName={node}\nKind=tap\n".encode()


def network(node: str) -> bytes:
    """Realistic .network document body for a node."""
    return f"[Match]\nName={node}\n\n[Network]\nBridge=k8sbr0\n".encode()


def write_files(directory: Path, files: dict[str, bytes]) -> None:
    """Write every file in the mapping into directory (bytes verbatim)."""
    for name, content in files.items():
        (directory / name).write_bytes(content)


def snapshot(directory: Path) -> dict[str, bytes]:
    """Map of top-level regular file name to bytes in directory."""
    return {
        entry.name: entry.read_bytes()
        for entry in sorted(directory.iterdir())
        if entry.is_file()
    }


def test_install_copies_every_source_file_byte_identical(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source_files = {
        "k8s-cp1.netdev": netdev("k8s-cp1"),
        "k8s-cp1.network": network("k8s-cp1"),
        "k8s-w1.netdev": netdev("k8s-w1"),
        "k8s-w1.network": network("k8s-w1"),
    }
    write_files(source_dir, source_files)

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert snapshot(target_dir) == source_files


def test_install_overwrites_diverged_target_content(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    write_files(source_dir, {"k8s-w1.network": network("k8s-w1")})
    (target_dir / "k8s-w1.network").write_bytes(
        b"[Match]\nName=k8s-w1\n\n[Network]\nBridge=br0\n"
    )

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert (target_dir / "k8s-w1.network").read_bytes() == network("k8s-w1")


def test_stale_k8s_files_not_in_source_are_removed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    write_files(source_dir, {"k8s-w1.network": network("k8s-w1")})
    write_files(
        target_dir,
        {
            "k8s-w1.network": network("k8s-w1"),
            "k8s-w9.netdev": netdev("k8s-w9"),
            "k8s-w9.network": network("k8s-w9"),
        },
    )

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert snapshot(target_dir) == {"k8s-w1.network": network("k8s-w1")}


def test_stale_removal_only_targets_netdev_and_network_extensions(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    link_doc = b"[Link]\nName=k8s-orphan\n"
    write_files(source_dir, {"k8s-w1.network": network("k8s-w1")})
    write_files(
        target_dir,
        {
            "k8s-w1.network": network("k8s-w1"),
            "k8s-orphan.link": link_doc,
            "k8s-w9.network": network("k8s-w9"),
        },
    )

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert snapshot(target_dir) == {
        "k8s-w1.network": network("k8s-w1"),
        "k8s-orphan.link": link_doc,
    }


def test_non_k8s_files_in_target_are_never_touched(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    foreign_files = {
        "packer-tap.netdev": b"[NetDev]\nName=packer-tap\nKind=tap\n",
        "packer-tap.network": b"[Match]\nName=packer-tap\n\n[Network]\nBridge=k8sbr0\n",
        "k8sbr0.netdev": b"[NetDev]\nName=k8sbr0\nKind=bridge\n",
        "k8sbr0.network": b"[Match]\nName=k8sbr0\n",
        "unrelated.txt": b"keep me\n",
    }
    write_files(source_dir, {"k8s-w1.network": network("k8s-w1")})
    write_files(target_dir, {**foreign_files, "k8s-w9.network": network("k8s-w9")})

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert snapshot(target_dir) == {
        **foreign_files,
        "k8s-w1.network": network("k8s-w1"),
    }


def test_non_k8s_files_in_source_are_not_installed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    write_files(
        source_dir,
        {
            "k8s-w1.network": network("k8s-w1"),
            "packer-tap.network": b"not for install\n",
            "k8sbr0.network": b"not for install either\n",
            "stray.txt": b"not a network file\n",
        },
    )

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert snapshot(target_dir) == {"k8s-w1.network": network("k8s-w1")}


def test_missing_source_dir_exits_nonzero_with_message(tmp_path: Path) -> None:
    source_dir = tmp_path / "missing-source"
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert str(source_dir) in proc.stderr
    # The failure must be a reconcile error, not the interpreter failing to
    # find the tool itself on disk.
    assert "No such file" not in proc.stderr


def test_source_path_is_a_file_exits_nonzero(tmp_path: Path) -> None:
    source_dir = tmp_path / "not-a-directory"
    target_dir = tmp_path / "target"
    source_dir.write_bytes(b"x")
    target_dir.mkdir()

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert str(source_dir) in proc.stderr
    assert "No such file" not in proc.stderr


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    write_files(
        source_dir,
        {"k8s-w1.netdev": netdev("k8s-w1"), "k8s-w1.network": network("k8s-w1")},
    )
    write_files(
        target_dir,
        {
            "k8s-w1.netdev": netdev("k8s-w1"),
            "k8s-w9.network": network("k8s-w9"),
            "packer-tap.network": b"foreign\n",
        },
    )

    first = run_reconcile(source_dir, target_dir)
    assert first.returncode == 0
    after_first = snapshot(target_dir)

    second = run_reconcile(source_dir, target_dir)

    assert second.returncode == 0
    assert snapshot(target_dir) == after_first


def test_empty_source_dir_removes_all_k8s_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    foreign_files = {
        "packer-tap.netdev": b"[NetDev]\nName=packer-tap\nKind=tap\n",
        "k8sbr0.network": b"[Match]\nName=k8sbr0\n",
    }
    write_files(
        target_dir,
        {
            **foreign_files,
            "k8s-w1.netdev": netdev("k8s-w1"),
            "k8s-w1.network": network("k8s-w1"),
            "k8s-w2.network": network("k8s-w2"),
        },
    )

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert snapshot(target_dir) == foreign_files


def test_directories_are_ignored_in_both_source_and_target(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "sub").mkdir()
    (source_dir / "sub" / "k8s-w1.network").write_bytes(network("k8s-w1"))
    (target_dir / "k8s-dir").mkdir()
    (target_dir / "k8s-dir" / "inner.txt").write_bytes(b"nested\n")
    (target_dir / "k8s-w9.network").write_bytes(network("k8s-w9"))

    proc = run_reconcile(source_dir, target_dir)

    assert proc.returncode == 0
    assert not (target_dir / "k8s-w1.network").exists()
    assert not (target_dir / "k8s-w9.network").exists()
    assert (target_dir / "k8s-dir" / "inner.txt").read_bytes() == b"nested\n"
    assert snapshot(target_dir) == {}
