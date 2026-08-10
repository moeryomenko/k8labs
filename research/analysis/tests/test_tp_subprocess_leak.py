"""Regression tests — no perfetto trace_processor subprocess leaks.

perfetto.trace_processor.TraceProcessor spawns a ~400-500 MB
``trace_processor_shell`` subprocess that only exits on ``close()`` (api.py
``close()`` or the context manager).  The analysis scripts used to create the
processor and never close it, leaking one child per processed trace (an
earlier run leaked ~170 children, ~70 GB, and OOMed the host).  The scripts
now close every processor via ``with tp:``; these tests pin that behavior.

The primary test runs the REAL fixed path against the smallest available
.perfetto-trace and asserts no ``trace_processor`` process survives
(``pgrep -f trace_processor`` == 0, short settle).  It skips when pgrep, the
perfetto package, or a real trace is unavailable.  A secondary unit test
exercises the same path with a recording fake so the close() contract is
verified even without a real trace.

Runs in the normal suite (no ``memory`` marker).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ANALYSIS_DIR = Path(__file__).resolve().parent.parent
EEVDF_SCRIPT = ANALYSIS_DIR / "eevdf-analyze.py"

# The trace_processor child's command line contains this literal (the
# perfetto prebuilt binary is named trace_processor_shell-<sha>).
PGREP_PATTERN = "trace_processor"

# Poll budget granted for the child to die after close() kills it.
SETTLE_POLL_S = 0.5
SETTLE_TIMEOUT_S = 15.0


def _count_trace_processor_procs() -> int | None:
    """Return the number of live trace_processor processes, or None when pgrep is unusable."""
    if shutil.which("pgrep") is None:
        return None
    try:
        proc = subprocess.run(
            ["pgrep", "-f", PGREP_PATTERN],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 1:
        return 0  # no matches
    if proc.returncode != 0:
        return None
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def _smallest_trace() -> Path | None:
    """Smallest .perfetto-trace under research/experiments/data, if any."""
    data_root = ANALYSIS_DIR.parent / "experiments" / "data"
    if not data_root.is_dir():
        return None
    traces = [p for p in data_root.rglob("*.perfetto-trace") if p.is_file()]
    if not traces:
        return None
    return min(traces, key=lambda p: p.stat().st_size)


def _perfetto_available() -> bool:
    """True when the real perfetto package (which spawns the subprocess) is importable."""
    try:
        import perfetto  # noqa: F401
    except ImportError:
        return False
    return True


def _wait_settled() -> int:
    """Poll pgrep until zero trace_processor processes remain, or timeout.

    Returns the final count (0 == the child exited; >0 == a leak survived).
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    count: int | None = None
    while time.monotonic() < deadline:
        count = _count_trace_processor_procs()
        if count == 0:
            return 0
        time.sleep(SETTLE_POLL_S)
    return count if count is not None else 0


def test_no_tp_subprocess_survives_after_analysis(tmp_path) -> None:
    """A minimal analysis run over a real trace must leave zero children.

    Runs eevdf-analyze.py (the fixed path) against the smallest available
    trace and asserts no ``trace_processor`` subprocess remains after the
    process exits (with a short settle for the child to die on close()).
    """
    if _count_trace_processor_procs() is None:
        pytest.skip("pgrep unavailable")
    if not _perfetto_available():
        pytest.skip("perfetto package not installed (no subprocess to leak)")
    trace = _smallest_trace()
    if trace is None:
        pytest.skip("no .perfetto-trace fixture under research/experiments/data")

    out_dir = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(EEVDF_SCRIPT),
            "--trace",
            str(trace),
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"eevdf-analyze failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert out_dir.is_dir()

    count = _wait_settled()
    assert count == 0, f"trace_processor subprocess(es) survived: {count}"


class _FakeQueryResult:
    """Minimal Perfetto QueryResult stand-in returning an empty DataFrame."""

    def __init__(self):
        import pandas as pd

        self._df = pd.DataFrame()

    def __iter__(self):
        return iter(self._df.itertuples(index=False))

    def as_pandas_dataframe(self):
        return self._df


def test_eevdf_analyze_closes_trace_processor(monkeypatch, tmp_path) -> None:
    """The fixed path must close() every TraceProcessor (recording fake).

    Unit-level pin of the fix: with a fake perfetto package installed via
    sys.modules, running eevdf-analyze's per-trace path must call close()
    exactly once (the with-block's __exit__), independent of real traces.
    """
    import types

    closed: list[bool] = []

    class _FakeTP:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql):
            return _FakeQueryResult()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

        def close(self):
            closed.append(True)

    fake_module = types.ModuleType("perfetto.trace_processor")
    setattr(fake_module, "TraceProcessor", _FakeTP)
    monkeypatch.setitem(sys.modules, "perfetto.trace_processor", fake_module)

    spec = importlib.util.spec_from_file_location("eevdf_analyze", EEVDF_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eevdf_analyze"] = module
    spec.loader.exec_module(module)

    trace = tmp_path / "fake.perfetto-trace"
    trace.write_bytes(b"HPb\x00\x01")
    out_dir = tmp_path / "out"

    rc = module.process_trace_file_perfetto(str(trace), str(out_dir))
    assert rc == 0
    assert closed == [True], "TraceProcessor.close() was never called"
