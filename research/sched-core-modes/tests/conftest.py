"""Test path setup for the sched-core-modes series."""

from __future__ import annotations

import sys
from pathlib import Path

EXPERIMENTS = Path(__file__).parents[1] / "experiments"
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))
