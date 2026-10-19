"""Resolve the explicitly selected Hermes or standalone runtime paths."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict

from mcs_util import HOME

STANDALONE_LABEL = "ai.mcs.standalone"
GATEWAY_SOURCE_DIRS = ("hermes_plugin", "adapters/common", "adapters/slack", "adapters/discord", "mcs")


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


def sqlite_wal_safe(version: tuple[int, ...]) -> bool:
    """Official WAL-reset fixes, including the two maintained backports."""
    return version >= (3, 51, 3) or (
        version[:2] == (3, 50) and version >= (3, 50, 7)) or (
        version[:2] == (3, 44) and version >= (3, 44, 6))


class RuntimeFacts(TypedDict):
    python: str
    sqlite: str
    packages: dict[str, str | None]


def runtime_problem(facts: RuntimeFacts | None, *, recovery: bool = False,
                     allow_runtime_migration: bool = False) -> str | None:
    if facts is None:
        return "実行環境の情報を取得できません"
    minimum = (3, 10) if allow_runtime_migration and not recovery else (3, 14)
    if tuple(map(int, facts["python"].split("."))) < minimum:
        return "実行環境のPythonの版が対応範囲外です"
    if not sqlite_wal_safe(tuple(map(int, facts["sqlite"].split(".")))):
        return "実行環境のSQLiteに、データ保存の安全性に必要な修正がありません"
    return None
