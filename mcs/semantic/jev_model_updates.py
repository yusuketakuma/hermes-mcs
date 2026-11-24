"""Notice Jev model versions newer than the one in use, asking Jev at most once a day.

Nothing here switches models (AT-064): a newer version is only named, and
``mcs_setup.py jev-model`` switches after its synthetic probe passes. The
model list is one budgeted GET through ``semantic_jev.live_client``; its
result is kept in ``data/jev-models.json`` so diagnostics read it offline.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import mcs_util
import provider_settings
import safe_io
import semantic_jev as jev

STATE_NAME = "jev-models.json"
STATE_VERSION = 1
CHECK_INTERVAL_S = 86400
STATE_MAX_BYTES = 64 * 1024
MAX_LISTED = 50


def state_path(root) -> Path:
    return Path(root) / "data" / STATE_NAME


def _valid_state(state) -> bool:
    return (isinstance(state, dict) and set(state) == {"version", "checked_at", "models", "error"}
            and state["version"] == STATE_VERSION
            and type(state["checked_at"]) in (int, float) and state["checked_at"] >= 0
            and isinstance(state["models"], list) and len(state["models"]) <= MAX_LISTED
            and all(provider_settings.jev_model_version(m) is not None for m in state["models"])
            and (state["error"] is None or mcs_util.valid_text(state["error"], 64)))


def read_state(root) -> dict | None:
    """The last model-list result, or None when there is none or it is unreadable."""
    try:
        raw = safe_io.read_regular_nofollow(state_path(root), STATE_MAX_BYTES, lambda: ValueError("state"))
        if len(raw) > STATE_MAX_BYTES:
            return None
        state = mcs_util.strict_json(raw)
    except (OSError, ValueError, RecursionError):
        return None
    return state if _valid_state(state) else None


def _write_state(root, state) -> None:
    body = json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n"
    safe_io.atomic_write(str(state_path(root)), lambda stream: stream.write(body), 0o600,
                         makedirs=False)


def newer_models(models, current) -> list[str]:
    """Listed fixed versions above ``current``, newest first; retired pins are never offered."""
    floor = provider_settings.jev_model_version(current)
    if floor is None:
        return []
    found = {m for m in models if m not in provider_settings.JEV_RETIRED_PINS
             and (provider_settings.jev_model_version(m) or ()) > floor}
    return sorted(found, key=provider_settings.jev_model_version, reverse=True)


def in_use(cfg, root) -> dict | None:
    """The effective model and whether Jev may be asked for its list, or None for an invalid config."""
    import semantic_policy
    scfg, errors = semantic_policy.runtime_config(cfg, root=root)
    if errors:
        return None
    key = jev.api_key(root)
    return {"model": scfg["model"], "key": key,
            "askable": bool(key) and scfg["daily_request_budget"] > 0 and not scfg["jev_held"]}


def refresh(root, key, *, now=None, force=False, client_factory=None) -> dict | None:
    """Ask Jev for its model list unless that was done within CHECK_INTERVAL_S.

    A busy writer leaves the last result as it is so the next check asks
    again; any other failure is kept with its code and also waits a day."""
    now = time.time() if now is None else now
    state = read_state(root)
    if not force and state is not None and 0 <= now - state["checked_at"] < CHECK_INTERVAL_S:
        return state
    if not key or not os.path.isdir(Path(root) / "data"):
        return state
    factory = client_factory or jev.live_client
    try:
        with factory(key, operation="jev_model_check", root=root, max_attempts=1) as client:
            listed = client.models()
        models = sorted({m for m in listed if provider_settings.jev_model_version(m) is not None},
                        key=provider_settings.jev_model_version, reverse=True)[:MAX_LISTED]
        fresh = {"version": STATE_VERSION, "checked_at": now, "models": models, "error": None}
    except jev.JevError as error:
        if error.detail in ("usage_lock_busy", "usage_ledger_busy"):
            return state
        fresh = {"version": STATE_VERSION, "checked_at": now,
                 "models": state["models"] if state is not None else [], "error": error.kind}
    except (OSError, ValueError):
        fresh = {"version": STATE_VERSION, "checked_at": now,
                 "models": state["models"] if state is not None else [], "error": "unavailable"}
    try:
        _write_state(root, fresh)
    except OSError:
        pass   # the result is still shown; the next check asks again
    return fresh


def notice(current: str, state: dict | None) -> str | None:
    """Information naming a newer listed model, or None when there is nothing to say."""
    if state is None:
        return None
    newer = newer_models(state["models"], current)
    if not newer:
        return None
    return (f"Jev: 新しいモデル {newer[0]} を利用できます（使用中: {current}）。自動では切り替えません。"
            f"切り替える場合は `python3 mcs/ops/mcs_setup.py jev-model --to {newer[0]} --confirm-human "
            "--reason '理由'` を実行します（合成データの確認に合格したときだけ切り替わり、"
            "完了済みの意味解析は新しいモデルでやり直されます）")


def check(cfg, root, *, now=None, client_factory=None) -> str | None:
    """Refresh the list when Jev is in use and due, then return the notice for ``mcs_setup.py check``."""
    use = in_use(cfg, root)
    if use is None or not use["askable"]:
        return None
    return notice(use["model"], refresh(root, use["key"], now=now, client_factory=client_factory))
