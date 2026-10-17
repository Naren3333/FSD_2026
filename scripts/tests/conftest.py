"""The scripts import each other as top-level modules (`python scripts/<name>.py` puts scripts/ on the path), and
the Kong generator lives in infra/kong; put both on the path for the tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in [ROOT / "scripts", ROOT / "infra" / "kong"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
