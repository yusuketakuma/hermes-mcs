"""Resolve the explicitly selected Hermes or standalone runtime paths."""
from __future__ import annotations

import os
from pathlib import Path

from mcs_util import HOME

HERMES_PY = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
STANDALONE_LABEL = "ai.mcs.standalone"


def mode(cfg: dict) -> str:
    value = cfg.get("runtime_mode", "hermes")
    if value not in ("hermes", "standalone"):
        raise ValueError("runtime_mode_invalid")
    return value


def standalone(cfg: dict) -> bool:
    return mode(cfg) == "standalone"


def runtime_home(cfg: dict, *, root=None) -> str:
    if mode(cfg) == "standalone":
        return str(Path(root or HOME).expanduser().resolve())
    return os.path.expanduser("~/.hermes")


def python_executable(cfg: dict, *, root=None) -> str:
    home = runtime_home(cfg, root=root)
    parts = ("venv", "bin", "python3") if mode(cfg) == "standalone" else (
        "hermes-agent", "venv", "bin", "python")
    return os.path.join(home, *parts)


def scripts_dir(cfg: dict, *, root=None) -> str:
    return os.path.join(runtime_home(cfg, root=root), "scripts")
