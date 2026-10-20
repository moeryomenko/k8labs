from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quota_calibration as qc  # noqa: E402


def test_parse_cpu() -> None:
    assert qc.parse_cpu_quantity("500m") == 500.0
    assert qc.parse_cpu_quantity("1") == 1000.0


def test_calibrate_tightens_when_low_throttle() -> None:
    assert qc.parse_cpu_quantity(qc.calibrate_cpu_limit("1", 0.0)) < 1000.0


def test_target_ratio() -> None:
    assert qc.DEFAULT_TARGET_THROTTLED_RATIO >= 0.10


def test_cli_refuses_to_go_below_the_request_floor(capsys) -> None:
    import quota_calibration as qc

    assert qc.main(["--current", "500m", "--observed", "0.04",
                    "--target", "0.10", "--floor", "500m"]) == 3
    assert "cannot reach the target" in capsys.readouterr().err


def test_cli_returns_the_next_limit_above_the_floor(capsys) -> None:
    import quota_calibration as qc

    assert qc.main(["--current", "1", "--observed", "0.04",
                    "--target", "0.10", "--floor", "500m"]) == 0
    assert capsys.readouterr().out.strip() == "900m"
