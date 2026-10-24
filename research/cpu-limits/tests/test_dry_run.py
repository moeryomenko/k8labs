"""Tests for the marked synthetic CPU-limits dry run."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from analysis.dry_run import SYNTHETIC, run


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_dry_run_writes_marked_synthetic_analysis(tmp_path: Path) -> None:
    result = run(tmp_path)

    assert result["synthetic"] is True
    assert len(result["cells"]) == 108
    assert all(cell["synthetic"] is True for cell in result["cells"])

    for phase in ("pilot", "confirmatory"):
        for name in ("blocks.csv", "cgroups.csv", "metrics.csv"):
            rows = _csv_rows(tmp_path / phase / name)
            assert rows
            assert all(row["synthetic"] == SYNTHETIC for row in rows)

    analysis = tmp_path / "analysis"
    summary = json.loads((analysis / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic"] is True
    assert all(cell["synthetic"] is True for cell in summary["cells"])
    assert all(
        row["synthetic"] == SYNTHETIC for row in _csv_rows(analysis / "summary.csv")
    )
    assert "SYNTHETIC DATA ONLY" in (analysis / "summary.md").read_text(
        encoding="utf-8"
    )
    assert "SYNTHETIC DATA ONLY" in (analysis / "report.md").read_text(encoding="utf-8")
    assert (analysis / "figures" / "performance-heatmap.svg").is_file()
