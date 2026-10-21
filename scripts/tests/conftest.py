"""make script and gateway modules importable by the test fixtures."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in [ROOT / "scripts", ROOT / "infra" / "kong"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
