"""Makes sdk/python importable without installing it (pip install -e sdk/python works too)."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "sdk" / "python" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
