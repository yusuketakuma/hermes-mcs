"""Shared plugin plumbing: MCS import bootstrap and the JSON reply envelope."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_ADAPTER_DIR = Path(__file__).resolve().parents[1] / "mcs"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _ok(**fields: Any) -> str:
    return _json({"ok": True, **fields})


def _deny(error: str) -> str:
    return _json({"ok": False, "error": error})


def _adapter_modules():
    # Hermes loads this plugin by file location without the repo root on
    # sys.path; `adapters.common` (op=summary) needs it, as the shims do.
    for root in (_ADAPTER_DIR.parent, _ADAPTER_DIR):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    import _mcs_path  # noqa: F401  registers every subdir as import root
    import mcs_requests
    import mcs_view
    return mcs_requests, mcs_view
