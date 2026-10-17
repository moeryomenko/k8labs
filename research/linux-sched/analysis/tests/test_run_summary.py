"""Tests for the per-run summary record."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_summary import build, main  # noqa: E402


def test_a_capped_run_says_so(tmp_path: Path) -> None:
    out = tmp_path / "run-summary-postgres.json"
    assert main([
        "--out", str(out), "--protocol", "v2", "--phase", "confirmatory",
        "--scenario", "postgres", "--first", "1", "--last", "9",
        "--completed", "4", "--cap-s", "14400", "--elapsed-s", "15000",
        "--cap-reached", "--duration-s", "30", "--warmup-s", "30",
        "--block-seconds", "420,430,415,425",
    ]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["cap_reached"] is True
    assert payload["blocks_completed"] == 4
    assert payload["blocks_requested"] == 9
    assert payload["block_seconds"] == [420, 430, 415, 425]
    assert payload["duration_s"] == 30
    assert payload["protocol_version"] == "v2"


def test_a_complete_run_is_not_marked_capped() -> None:
    payload = build(
        protocol_version="v2", phase="confirmatory", scenario="ffmpeg",
        first_block=1, last_block=9, completed=9, cap_s=14400, elapsed_s=3000,
        cap_reached=False, duration_s=30, warmup_s=0, block_seconds=[200] * 9,
    )
    assert payload["cap_reached"] is False
    assert payload["blocks_completed"] == payload["blocks_requested"]
