"""Run MCS's Slack/Discord connection and delivery without Hermes Agent."""
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_ROOT), str(_ROOT / "mcs")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import _mcs_path  # noqa: E402,F401
