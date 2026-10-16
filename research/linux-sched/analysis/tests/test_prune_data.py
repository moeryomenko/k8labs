"""Unit tests for bounded data-root pruning."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prune_data import human, main, plan_pruning  # noqa: E402


def _block(data_root: Path, scenario: str, number: int, *, big: bool = True) -> Path:
    block = data_root / scenario / f"block-{number:02d}"
    arm_dir = block / "observer-eevdf"
    arm_dir.mkdir(parents=True)
    (block / "metadata.json").write_text('{"block":1}\n', encoding="utf-8")
    (block / "eevdf.metric").write_text("1\n", encoding="utf-8")
    (arm_dir / "runqlat.txt").write_text("0 -> 1 : 5\n", encoding="utf-8")
    if big:
        (block / "perf.data.gz").write_bytes(b"x" * 2048)
        (arm_dir / "sched-trace.txt.gz").write_bytes(b"y" * 1024)
    return block


def test_keeps_the_newest_blocks_and_the_primary_records(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    for number in (1, 2, 3):
        _block(data_root, "postgres", number)

    removable, removable_bytes, kept_bytes = plan_pruning(data_root, keep_blocks=2)
    removable_names = {p.name for p in removable}

    # Only the oldest block's bulk evidence is prunable.
    assert removable_names == {"perf.data.gz", "sched-trace.txt.gz"}
    assert all("block-01" in str(p) for p in removable)
    assert removable_bytes > 0 and kept_bytes > 0

    # The records a result is built from survive in every block, newest or not.
    survivors = {p.name for p in (data_root / "postgres" / "block-01").rglob("*")}
    assert "metadata.json" in survivors
    assert "eevdf.metric" in survivors
    assert "runqlat.txt" in survivors


def test_never_prunes_blocks_csv_or_analysis(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _block(data_root, "postgres", 1)
    (data_root / "blocks.csv").write_text("block_id\n1\n", encoding="utf-8")
    analysis = data_root / "analysis"
    analysis.mkdir()
    (analysis / "summary.json").write_text("{}\n", encoding="utf-8")

    removable, _, _ = plan_pruning(data_root, keep_blocks=0)
    assert not any(p.name == "blocks.csv" for p in removable)
    assert not any("analysis" in p.parts for p in removable)


def test_keep_blocks_zero_prunes_every_block(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    for number in (1, 2):
        _block(data_root, "postgres", number)
    removable, _, _ = plan_pruning(data_root, keep_blocks=0)
    assert len(removable) == 4


def test_report_does_not_delete_then_apply_does(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _block(data_root, "postgres", 1)
    _block(data_root, "postgres", 2)

    assert main(["--data-root", str(data_root), "--keep-blocks", "1"]) == 0
    assert (data_root / "postgres" / "block-01" / "perf.data.gz").is_file()

    assert main(["--data-root", str(data_root), "--keep-blocks", "1", "--apply"]) == 0
    assert not (data_root / "postgres" / "block-01" / "perf.data.gz").exists()
    assert (data_root / "postgres" / "block-02" / "perf.data.gz").is_file()


def test_missing_data_root_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plan_pruning(tmp_path / "absent", keep_blocks=1)


def test_human_reads_in_units() -> None:
    assert human(512).endswith("B")
    assert human(2048).endswith("KiB")
    assert human(5 * 1024**2).endswith("MiB")
