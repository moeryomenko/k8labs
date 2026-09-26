from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "normalize_manifest",
    Path(__file__).parents[1] / "scripts" / "normalize_manifest.py",
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
normalize = _module.normalize


def test_normalize_resolves_shell_defaults_without_executing_code() -> None:
    source = "args: ['--foo=${FOO:=bar}', '--x=$(touch /tmp/pwned)']\n"
    result = normalize(source)
    assert result == "args: ['--foo=bar', '--x=$(touch /tmp/pwned)']\n"


def test_normalize_is_idempotent() -> None:
    source = Path(__file__).parents[1] / "scripts" / "normalize_manifest.py"
    text = source.read_text(encoding="utf-8")
    assert normalize(normalize(text)) == normalize(text)
