"""Independent MCS runtime without Hermes Agent imports or credentials."""
from pathlib import Path
import sys

_mcs = str(Path(__file__).resolve().parents[1] / "mcs")
if _mcs not in sys.path:
    sys.path.insert(0, _mcs)
import _mcs_path  # noqa: E402,F401
