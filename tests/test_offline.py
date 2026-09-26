from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "offline", Path(__file__).parents[1] / "scripts/offline.py"
)
assert SPEC and SPEC.loader
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def test_network_operation_is_rejected_offline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OFFLINE", "1")
    try:
        offline.require_cache(tmp_path, "missing")
    except SystemExit as error:
        assert "missing" in str(error)
    else:
        raise AssertionError("incomplete offline cache must fail")


def test_complete_cache_is_accepted_offline(tmp_path: Path) -> None:
    cache = tmp_path / "locks" / "digest"
    cache.mkdir(parents=True)
    for name in ("lock.json", "checksums.txt", "artifacts.json", "complete.json"):
        (cache / name).write_text("{}\n", encoding="utf-8")
    assert offline.require_cache(tmp_path, "digest") == cache
