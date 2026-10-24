"""Tests for CPU-limits Taskfile command contracts."""

from pathlib import Path

SERIES_ROOT = Path(__file__).resolve().parents[1]


def test_sample_size_task_passes_all_pilot_csv_inputs() -> None:
    taskfile = (SERIES_ROOT / "Taskfile.yml").read_text(encoding="utf-8")
    start = taskfile.index("  cpu-limits-sample-size:")
    end = taskfile.index("  cpu-limits-confirm:", start)
    command = taskfile[start:end]

    assert 'PYTHONPATH="{{.CPU_LIMITS_DIR}}"' in command
    assert '"{{.CPU_LIMITS_DATA_ROOT}}/pilot/blocks.csv"' in command
    assert '"{{.CPU_LIMITS_DATA_ROOT}}/pilot/cgroups.csv"' in command
    assert '"{{.CPU_LIMITS_DATA_ROOT}}/pilot/metrics.csv"' in command
    assert "--blocks" in command
    assert "--cgroups" in command
    assert "--metrics" in command
    assert "--data-root" not in command
