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


def _ensure_repo_root() -> None:
    """Put the checkout root on sys.path so `adapters.*` resolves to it."""
    # Hermes loads this plugin by file location (a symlink under its own
    # package name) without the repo root on sys.path.
    root = str(_ADAPTER_DIR.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def _adapter_modules():
    # `adapters.common` (op=summary) needs the repo root; MCS modules
    # need mcs/ ahead of it.
    _ensure_repo_root()
    if str(_ADAPTER_DIR) not in sys.path:
        sys.path.insert(0, str(_ADAPTER_DIR))
    import _mcs_path  # noqa: F401  registers every subdir as import root
    import mcs_requests
    import mcs_view
    return mcs_requests, mcs_view
