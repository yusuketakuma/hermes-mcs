"""Regression: a 確定 human command is refused once interactivity is off,
a restore is pending or the transport moved — the ledger stays unchanged."""
from __future__ import annotations

import json

import pytest

import notify_cards
import notify_cmds
from notify_testkit import CFG, NOW, _delivered_card, led, pinned_clock

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")

CMD = {"version": 1, "cmd": "request.create",
       "command_id": "11111111-2222-4333-8444-555555555557",
       "actor": "discord:1001", "human_confirmed": True, "project_id": 1,
       "source_message_id": 101, "source_hash": f"{101:064x}",
       "title": "合成タスク", "reason": "合成理由"}


def _requests(led):
    return led.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]


@pytest.mark.parametrize("state", ["interactive_off", "restore_pending", "transport"])
def test_human_command_is_rejected_when_interactivity_is_withdrawn(led, tmp_path, state):
    _delivered_card(led)
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    cfg, req = CFG, dict(CMD)
    if state == "interactive_off":
        cfg = {**CFG, "notify": {**CFG["notify"], "interactive": "off"}}
    elif state == "restore_pending":
        (root / notify_cards.RESTORE_MARKER).write_text(json.dumps({"at": NOW}))
    else:
        req["transport"] = "slack"
    out = notify_cmds.dispatch(led, req, cfg, str(root))
    assert out == {"outcome": "rejected", "error": "interactive_off",
                   "command_id": CMD["command_id"]}
    assert _requests(led) == 0


def test_human_command_still_applies_when_interactive(led, tmp_path):
    _delivered_card(led)
    out = notify_cmds.dispatch(led, dict(CMD), CFG, str(tmp_path / "data"))
    assert out["outcome"] == "applied" and _requests(led) == 1
