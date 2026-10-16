"""Unit tests for the SVG plot generation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sched_plots  # noqa: E402
from sched_plots import (  # noqa: E402
    grouped_bar_svg,
    main,
    parse_histogram,
    plot_primary,
    plot_runq,
    read_blocks,
)

RUNQLAT = """\
Tracing run queue latency... Hit Ctrl-C to end.

     usecs               : count    distribution
         0 -> 1          : 364      |****************                        |
         2 -> 3          : 268      |**************                          |
         4 -> 7          : 722      |****************************************|
"""

RUNQLEN = """\
Sampling run queue length... Hit Ctrl-C to end.

     runqlen       : count     distribution
        0          : 198      |****************************************|
        1          : 42       |********                                |
"""


def test_parse_histogram_reads_buckets_and_counts() -> None:
    assert parse_histogram(RUNQLAT) == [
        ("0 -> 1", 364),
        ("2 -> 3", 268),
        ("4 -> 7", 722),
    ]
    assert parse_histogram(RUNQLEN) == [("0", 198), ("1", 42)]


def test_parse_histogram_ignores_prose_and_the_header() -> None:
    # The header has a colon and a word where a count would be, and the banner
    # has neither shape; neither may become a bucket.
    assert parse_histogram(RUNQLAT)[0][0] == "0 -> 1"
    assert all(count > 0 for _, count in parse_histogram(RUNQLAT))


def test_grouped_bar_svg_is_deterministic_and_escapes_labels() -> None:
    first = grouped_bar_svg("t<&>", "s", ["a"], {"arm": [1.0]}, "v")
    second = grouped_bar_svg("t<&>", "s", ["a"], {"arm": [1.0]}, "v")
    assert first == second
    assert "&lt;&amp;&gt;" in first
    assert first.startswith("<svg") and first.rstrip().endswith("</svg>")


def _rows() -> list[dict[str, str]]:
    rows = []
    for block in (1, 2):
        for arm, value in (("eevdf", 100.0 + block), ("scx_lavd", 120.0 + block)):
            rows.append(
                {
                    "block_id": str(block),
                    "scheduler": arm,
                    "workload": "postgres",
                    "metric": "transactions_per_sec",
                    "value": str(value),
                    "direction": "higher-is-better",
                }
            )
    return rows


def test_read_blocks_rejects_an_empty_table(tmp_path: Path) -> None:
    path = tmp_path / "blocks.csv"
    path.write_text(
        "block_id,scheduler,workload,metric,value,direction\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        read_blocks(path)


def test_plot_primary_writes_one_figure_per_scenario(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "blocks.csv").write_text(
        "block_id,scheduler,workload,metric,value,direction\n"
        + "".join(
            f"{r['block_id']},{r['scheduler']},{r['workload']},"
            f"{r['metric']},{r['value']},{r['direction']}\n"
            for r in _rows()
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    written = plot_primary(data_root, out, read_blocks(data_root / "blocks.csv"))
    assert [p.name for p in written] == ["primary-postgres-transactions_per_sec.svg"]
    body = written[0].read_text(encoding="utf-8")
    assert "block 1" in body and "block 2" in body
    assert "eevdf" in body and "scx_lavd" in body


def test_plot_primary_skips_blocks_missing_an_arm(tmp_path: Path) -> None:
    rows = _rows() + [
        {
            "block_id": "3",
            "scheduler": "eevdf",
            "workload": "postgres",
            "metric": "transactions_per_sec",
            "value": "1",
            "direction": "higher-is-better",
        }
    ]
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "blocks.csv").write_text(
        "block_id,scheduler,workload,metric,value,direction\n"
        + "".join(
            f"{r['block_id']},{r['scheduler']},{r['workload']},"
            f"{r['metric']},{r['value']},{r['direction']}\n"
            for r in rows
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    written = plot_primary(data_root, out, read_blocks(data_root / "blocks.csv"))
    # Block 3 has only one arm, so it is not a comparison and must not appear.
    assert "block 3" not in written[0].read_text(encoding="utf-8")


def test_plot_runq_prefers_arm_scoped_artifacts(tmp_path: Path) -> None:
    block = tmp_path / "data" / "postgres" / "block-01"
    for arm in ("eevdf", "scx_lavd"):
        arm_dir = block / f"observer-{arm}"
        arm_dir.mkdir(parents=True)
        (arm_dir / "runqlat.txt").write_text(RUNQLAT, encoding="utf-8")
        (arm_dir / "runqlen.txt").write_text(RUNQLEN, encoding="utf-8")
    # A stale block-level copy must not be treated as a third arm.
    (block / "runqlat.txt").write_text(RUNQLAT, encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir()
    written = plot_runq(tmp_path / "data", out)
    names = sorted(p.name for p in written)
    assert names == [
        "runqlat-postgres-block-01.svg",
        "runqlen-postgres-block-01.svg",
    ]
    body = (out / "runqlat-postgres-block-01.svg").read_text(encoding="utf-8")
    assert "eevdf" in body and "scx_lavd" in body
    assert "arm not recorded" not in body


def test_plot_runq_falls_back_to_legacy_block_level_artifacts(tmp_path: Path) -> None:
    block = tmp_path / "data" / "postgres" / "block-01"
    block.mkdir(parents=True)
    (block / "runqlat.txt").write_text(RUNQLAT, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    written = plot_runq(tmp_path / "data", out)
    assert [p.name for p in written] == ["runqlat-postgres-block-01.svg"]
    assert "arm not recorded" in written[0].read_text(encoding="utf-8")


def test_main_needs_data(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "blocks.csv").write_text(
        "block_id,scheduler,workload,metric,value,direction\n", encoding="utf-8"
    )
    # An empty table is an error, not an empty plot.
    with pytest.raises(ValueError):
        main(
            [
                "--data-root",
                str(tmp_path / "data"),
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
