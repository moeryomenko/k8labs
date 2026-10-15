from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "offline", Path(__file__).parents[1] / "scripts/offline.py"
)
assert SPEC and SPEC.loader
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def test_incomplete_cache_is_rejected_offline(tmp_path: Path) -> None:
    lock = {"schema_version": 1}
    with patch.object(offline, "lock_digest", return_value="missing"), patch.object(
        offline, "validate_cache", return_value=["missing OCI blob for manager"]
    ):
        try:
            offline.require_cache(tmp_path, lock)
        except SystemExit as error:
            assert "missing OCI blob for manager" in str(error)
        else:
            raise AssertionError("incomplete offline cache must fail")


def test_complete_cache_is_accepted_offline(tmp_path: Path) -> None:
    lock = {"schema_version": 1}
    with patch.object(offline, "lock_digest", return_value="digest"), patch.object(
        offline, "validate_cache", return_value=[]
    ):
        assert offline.require_cache(tmp_path, lock) == tmp_path / "locks" / "digest"
