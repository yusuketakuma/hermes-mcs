"""Stable CLI entry point for the canonical LINE WORKS adapter."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "mcs"))
import _mcs_path  # noqa: E402,F401  (flat module roots; same bootstrap as adapters.lineworks)
if __name__ == "__main__":
    import runtime_entry as _runtime_entry  # noqa: E402
    _RUNTIME_ENTRY = _runtime_entry.acquire()  # noqa: E402  (Q SH; process lifetime via runtime_entry.current())

from adapters.lineworks.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
