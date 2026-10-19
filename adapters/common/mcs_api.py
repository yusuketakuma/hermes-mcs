"""Load the checkout-local flat MCS API and encode command replies."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_MCS_DIR = Path(__file__).resolve().parents[2] / "mcs"


def encode_reply(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def ok(**fields: Any) -> str:
    return encode_reply({"ok": True, **fields})


def deny(error: str) -> str:
    return encode_reply({"ok": False, "error": error})


def bootstrap() -> None:
    """Register this checkout's flat MCS imports without loading its APIs."""
    if str(_MCS_DIR) not in sys.path:
        sys.path.insert(0, str(_MCS_DIR))
    import _mcs_path  # noqa: F401  registers every subdir as import root


def modules():
    """Load MCS modules lazily from this checkout; no SDK or live I/O."""
    bootstrap()
    import mcs_requests
    import mcs_view
    return mcs_requests, mcs_view
