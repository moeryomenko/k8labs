"""Test path setup for the sched-core-delta series."""

from __future__ import annotations

import sys
from pathlib import Path

SERIES = Path(__file__).parents[1]
for directory in (SERIES / "experiments", SERIES / "analysis"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
