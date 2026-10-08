"""cmd_int drain → card sweep gating: transport-only drains skip the
all-card sweep (begin/settle issue their own renders); a drain that
applies an interaction still sweeps. Temp DB + stubs only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: F401

import notify_cards
import notify_cmds
from notify_testkit import (
    CFG, ORIGIN, SCOPE, _begin, _dispatch, _intent, _latest_render,
    _seed_thread, _uuid, led,
)

__all__ = ["led"]  # shared isolated-ledger fixture

_NOTIF = {"version": 1, "op": "notification",
          "command_id": f"{'c' * 32}:{'ee' * 8}", "actor": "nurse-1",
          "token": "c" * 32, "origin": ORIGIN}


def _ready_receipt(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led))["dispatched"]
    render = _latest_render(led)
    assert _begin(led, render, n=7)["granted"]
    return {"version": 1, "op": "transport_receipt",
            "command_id": _uuid(2), "attempt_id": f"{7:016x}",
            "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
            "payload_hash": render["payload_hash"], "route_epoch": 1,
            "correlation": render["correlation"], **SCOPE,
            "result": "delivered", "message_id": "m-7"}


def _drain(led, tmp_path, monkeypatch, reqs):
    root = tmp_path / "data"
    notify_cards.ensure_dirs(str(root))
    for i, req in enumerate(reqs):
        (root / "cmd_int" / f"{i}.json").write_text(json.dumps(req))
    calls = []
    monkeypatch.setattr(notify_cards, "sweep",
                        lambda *a, **k: calls.append(1))
    n = notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    assert n == len(reqs)
    assert led.db.execute(
        "SELECT state,message_id FROM notification_delivery_attempts WHERE attempt_id=?",
        (f"{7:016x}",)).fetchone()[:] == ("delivered", "m-7")
    return calls


def test_transport_only_drain_skips_sweep(led, tmp_path, monkeypatch):
    assert _drain(led, tmp_path, monkeypatch, [_ready_receipt(led)]) == []


def test_drain_with_notification_sweeps(led, tmp_path, monkeypatch):
    assert _drain(led, tmp_path, monkeypatch, [_ready_receipt(led), _NOTIF]) == [1]
