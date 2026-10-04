"""Synthetic: a confirmed human command's receipt survives its card re-render."""

import asyncio
import json
from pathlib import Path

import pytest

from test_mcs_slack_actions import (TOKEN, _to_confirm, ack, command,  # noqa: F401
                                    fixture, result, _no_modal_wait)


def _flags(dirs, **extra):
    Path(dirs["flags"]).mkdir(exist_ok=True)
    (Path(dirs["flags"]) / "notify.json").write_text(json.dumps(
        {"interactive": True, "transport": "slack", **extra}), encoding="utf-8")


def _receipt(tmp_path, spoil, legacy=False):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        actions._settings["route_epoch"] = 3
        _flags(dirs, route_epoch=3)
        body, (confirm_action, _) = await _to_confirm(actions, app, dirs, 123)
        await actions._confirm(ack, body, confirm_action)
        human = next(json.loads(p.read_text()) for p in Path(dirs["cmd_int"]).glob("*.json")
                     if json.loads(p.read_text()).get("cmd") == "request.create")
        result(dirs, human["command_id"], outcome="applied")
        if legacy:
            # saved by a worker predating route_epoch/project_id
            for rec in reg._data["followups"].values():
                rec.pop("route_epoch"), rec.pop("project_id")
        else:
            # the runner's card re-render pruned the confirmed button's token
            del reg._data["tokens"][TOKEN]
        spoil(actions, dirs)
        before = len(app.client.messages)
        await actions.sweep_followups()
        await actions.sweep_followups()
        return [m["text"] for m in app.client.messages[before:]], reg.followups()
    return asyncio.run(scenario())


def test_applied_receipt_reaches_user_after_card_token_is_pruned(tmp_path):
    texts, pending = _receipt(tmp_path, lambda *_: None)
    assert texts == ["反映しました。"]
    assert pending == {}


@pytest.mark.parametrize("spoil", [
    lambda actions, _: actions._settings.update(allowed_user_ids={"U_OTHER"}),
    lambda actions, _: actions._settings.update(project_ids=frozenset({999})),
    lambda actions, _: actions._settings.update(route_epoch=4),
    lambda _, dirs: _flags(dirs, route_epoch=4),
    lambda _, dirs: _flags(dirs, route_epoch=3, interactive=False),
    lambda _, dirs: _flags(dirs, route_epoch=3, transport="discord"),
], ids=["user", "project", "epoch", "flag_epoch", "interactive_off", "transport"])
def test_receipt_rechecks_current_authority(tmp_path, spoil):
    texts, pending = _receipt(tmp_path, spoil)
    assert texts == []
    assert pending == {}


def test_legacy_receipt_without_project_id_keeps_token_pin(tmp_path):
    texts, pending = _receipt(tmp_path, lambda *_: None, legacy=True)
    assert texts == ["反映しました。"]
    assert pending == {}
