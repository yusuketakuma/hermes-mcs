"""Resolve whether MCS runs on Hermes Agent or on its own standalone runtime."""
from __future__ import annotations

import os

from mcs_util import HOME

HERMES_PY = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
HERMES_SCRIPTS = os.path.expanduser("~/.hermes/scripts")
STANDALONE_LABEL = "ai.mcs.standalone"


def mode(cfg: dict) -> str:
    """`hermes` unless config.json explicitly selects `standalone`."""
    value = cfg.get("runtime_mode", "hermes") if isinstance(cfg, dict) else "hermes"
    return value if value in ("hermes", "standalone") else "hermes"


def standalone(cfg: dict) -> bool:
    return mode(cfg) == "standalone"


def python_executable(cfg: dict) -> str:
    """Interpreter for services: the Hermes venv, or MCS's own venv."""
    return os.path.join(HOME, "venv", "bin", "python3") if standalone(cfg) else HERMES_PY


def scripts_dir(cfg: dict) -> str:
    """Where rendered cron wrappers live (data/ is never tracked by git)."""
    return os.path.join(HOME, "data", "scripts") if standalone(cfg) else HERMES_SCRIPTS
