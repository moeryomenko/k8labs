"""Test configuration for the CPU-limits series."""

from __future__ import annotations

import sys
from pathlib import Path

SERIES_ROOT = Path(__file__).resolve().parents[1]

if str(SERIES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERIES_ROOT))
