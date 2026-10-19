"""Resolve this plugin's checkout before loading shared command and MCS APIs."""
from __future__ import annotations

import sys
from pathlib import Path


def _ensure_repo_root() -> None:
    """Make this symlink-resolved checkout importable under a host package alias."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)


def _adapter_modules():
    _ensure_repo_root()
    from adapters.common.mcs_api import modules
    return modules()
