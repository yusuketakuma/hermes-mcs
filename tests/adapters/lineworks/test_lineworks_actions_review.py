"""Synthetic regressions for fresh LINE WORKS views and authorized human receipts."""
import asyncio
import json
from pathlib import Path

import pytest

from hermes_plugin.mcs_delivery import envelopes, paths
from test_lineworks_adapter import SCOPE, TOKEN, command_files, event, world


def _result(w, result_id, result):
    path = Path(w.dirs["cmd_results"]) / (paths.safe_name(result_id) + ".json")
    paths.atomic_write(str(path), json.dumps(result, ensure_ascii=False).encode())


def test_notification_publish_failure_can_retry_same_click(tmp_path, monkeypatch):
    w = world(tmp_path, action="summary")
    publish = w.actions._publish
    calls = []

    async def transient_publish(payload):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise OSError("synthetic pre-publication failure")
        await publish(payload)

    monkeypatch.setattr(w.actions, "_publish", transient_publish)
    click = event(postback="mcs:a:" + TOKEN)
    with pytest.raises(OSError):
        asyncio.run(w.actions.handle(click))
    assert command_files(w) == []
    asyncio.run(w.actions.handle(click))
    assert len(command_files(w)) == 1
    assert calls[0] == calls[1]


def test_completed_view_click_waits_for_a_fresh_runner_result(tmp_path):
    w = world(tmp_path, action="summary")
    click = event(postback="mcs:a:" + TOKEN)
    asyncio.run(w.actions.handle(click))
    first = command_files(w)[0]
    _result(w, first["request_id"], {
        "request_id": first["request_id"], "outcome": "applied",
        "action": "summary", "body": "合成の古い要約"})
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 1

    asyncio.run(w.actions.handle(click))
    second = command_files(w)[0]
    assert second["command_id"] == first["command_id"]
    assert second["request_id"] != first["request_id"]
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 1
    _result(w, second["request_id"], {
        "request_id": second["request_id"], "outcome": "applied",
        "action": "summary", "body": "合成の新しい要約"})
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 2
    assert "合成の新しい要約" in w.client.calls[-1][1]["text"]


@pytest.mark.parametrize("action,field", [("search", "query"), ("mytasks", "name")])
def test_repeated_private_view_input_uses_a_fresh_result_id(tmp_path, action, field):
    w = world(tmp_path, action=action)
    actor = "lineworks:40029600:operator"
    session = {"actor": actor, "user": "operator", "action": action,
               "token": TOKEN, "origin": {**SCOPE, "message_id": "lw:" + "c" * 32},
               "fields": {}, "index": 0,
               "definitions": [{"id": field, "required": True, "max": 120}]}
    modal_id = "lw-form-" + envelopes.actor_hash(actor)
    w.reg.put_modal(modal_id, session)
    asyncio.run(w.actions._input("operator", actor, "合成条件"))
    first = command_files(w)[0]
    _result(w, first["request_id"], {
        "request_id": first["request_id"], "outcome": "applied", "action": "list"})
    asyncio.run(w.actions.sweep_followups())
    w.reg.put_modal(modal_id, {**session, "fields": {}, "index": 0})
    asyncio.run(w.actions._input("operator", actor, "合成条件"))
    second = command_files(w)[0]
    assert second["command_id"] == first["command_id"]
    assert second["request_id"] != first["request_id"]


@pytest.mark.parametrize("change", [None, "epoch", "user", "project", "origin"])
def test_human_receipt_rechecks_scope_after_its_card_token_is_retired(tmp_path, change):
    w = world(tmp_path)
    actor = "lineworks:40029600:operator"
    w.reg.put_tokens({TOKEN: {**w.reg.token(TOKEN), "card_key": "synthetic-card"}})
    payload = envelopes.request_create(actor, w.reg.token(TOKEN)["context"], {
        "title": "完全合成タスク", "reason": "合成の確認理由"})
    confirm_id = "a" * 16
    w.reg.put_confirm(confirm_id, {
        "actor": actor, "token": TOKEN, "origin": SCOPE, "payload": payload})
    asyncio.run(w.actions._confirm(confirm_id, False, "operator", actor))
    assert command_files(w) == [payload]
    assert len(w.client.calls) == 1
    w.client.calls.clear()
    _result(w, payload["command_id"], {"outcome": "applied"})
    w.reg.retire_card_tokens("synthetic-card")

    if change == "epoch":
        flag_path = w.data / "flags" / "notify.json"
        flags = json.loads(flag_path.read_text())
        flags["route_epoch"] = 2
        flag_path.write_text(json.dumps(flags))
    elif change == "user":
        w.settings["allowed_user_ids"] = {"other"}
    elif change == "project":
        w.settings["project_ids"] = {2}
    elif change == "origin":
        rec = w.reg.followup(payload["command_id"])
        w.reg.put_followup(payload["command_id"], {
            **rec, "origin": {**rec["origin"], "channel_id": "foreign-room"}})

    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(payload["command_id"]) is None
    if change is None:
        assert w.client.calls == [("message", {"type": "text", "text": "反映しました。"},
                                   {"user_id": "operator", "channel_id": None})]
    else:
        assert w.client.calls == []


def test_human_receipt_is_tracked_before_publication_can_lose_its_ack(tmp_path, monkeypatch):
    w = world(tmp_path)
    actor = "lineworks:40029600:operator"
    payload = envelopes.request_create(actor, w.reg.token(TOKEN)["context"], {
        "title": "完全合成タスク", "reason": "合成の確認理由"})
    confirm_id = "a" * 16
    w.reg.put_confirm(confirm_id, {
        "actor": actor, "token": TOKEN, "origin": SCOPE, "payload": payload})
    publish = w.actions._publish

    async def interrupted(payload):
        await publish(payload)
        raise OSError("synthetic lost publication acknowledgement")

    monkeypatch.setattr(w.actions, "_publish", interrupted)
    with pytest.raises(OSError):
        asyncio.run(w.actions._confirm(confirm_id, False, "operator", actor))
    assert command_files(w) == [payload]
    assert w.reg.followup(payload["command_id"]) is not None
    asyncio.run(w.actions._confirm(confirm_id, False, "operator", actor))
    assert command_files(w) == [payload]
    w.client.calls.clear()
    _result(w, payload["command_id"], {"outcome": "applied"})
    asyncio.run(w.actions.sweep_followups())
    assert w.client.calls[-1][1]["text"] == "反映しました。"


def test_dm_summary_word_answers_privately_from_the_snapshot(tmp_path, monkeypatch):
    from adapters.common import summary
    w = world(tmp_path, action="summary")
    seen = []

    def answer(snapshot, rest, **kw):
        seen.append((snapshot, rest, kw))
        return {"text": "【📊 MCS サマリー】\n対象: 全患者"}
    monkeypatch.setattr(summary, "answer", answer)

    async def scenario():
        await w.actions.handle(event(text="サマリー mine name:山田", channel=None))
        assert w.client.calls[-1][2] == {"user_id": "operator", "channel_id": None}
        w.settings["snapshot"] = "/synthetic/ledger-snapshot.db"
        await w.actions.handle(event(text="サマリー mine name:山田", channel=None))
        # a shared room never answers, a non-allowlisted user is ignored
        await w.actions.handle(event(text="サマリー", channel=SCOPE["channel_id"]))
        await w.actions.handle(event(text="サマリー", user="stranger", channel=None))
    asyncio.run(scenario())
    assert [(s, r) for s, r, _ in seen] == [
        ("/synthetic/ledger-snapshot.db", "mine name:山田")]
    assert seen[0][2]["allowed"] == [1] and seen[0][2]["dialect"] == "plain"


@pytest.mark.parametrize("text", ["", "   ", "　　"])
def test_dm_without_words_is_ignored(tmp_path, text):
    """Regression: an empty or blank DM (image, sticker, spaces) raised
    in the summary-word check and was recorded as an unknown callback."""
    w = world(tmp_path, action="summary")
    asyncio.run(w.actions.handle(event(text=text, channel=None)))
    assert w.client.calls == []
