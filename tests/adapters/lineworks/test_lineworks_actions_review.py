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
    assert w.client.calls[-1][1]["text"] == "受付結果を確認できませんでした。結果の通知をお待ちください。"
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


def _hold_api_lock(w):
    import threading

    from adapters.lineworks import delivery
    held, release = threading.Event(), threading.Event()

    def hold():
        with delivery.api_lock(str(w.data)):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    return holder, release


def _summary_followup(w):
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    request_id = command_files(w)[0]["request_id"]
    _result(w, request_id, {"request_id": request_id, "outcome": "applied",
                            "action": "summary", "body": "合成の要約"})


def test_followup_dm_waits_out_a_briefly_held_api_lock(tmp_path, monkeypatch):
    from adapters.lineworks import actions
    w = world(tmp_path, action="summary")
    _summary_followup(w)
    holder, release = _hold_api_lock(w)

    async def first_retry_releases(_seconds):
        release.set()
        await asyncio.to_thread(holder.join, 5)

    monkeypatch.setattr(actions.asyncio, "sleep", first_retry_releases)
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 1
    assert "合成の要約" in w.client.calls[0][1]["text"]


def test_followup_survives_a_lock_held_past_the_retry_budget(tmp_path, monkeypatch):
    from adapters.lineworks import actions
    from adapters.lineworks.client import ClientError
    w = world(tmp_path, action="summary")
    _summary_followup(w)
    holder, release = _hold_api_lock(w)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(actions.asyncio, "sleep", no_wait)
    with pytest.raises(ClientError):
        asyncio.run(w.actions.sweep_followups())
    release.set()
    holder.join(5)
    assert w.client.calls == []
    assert len(w.reg.followups()) == 1
    asyncio.run(w.actions.sweep_followups())
    assert len(w.client.calls) == 1
    assert w.reg.followups() == {}


def test_other_send_errors_stay_at_most_once(tmp_path):
    from adapters.lineworks.client import ClientError
    w = world(tmp_path)
    w.client.error = ClientError("server_error", 500)
    with pytest.raises(ClientError):
        asyncio.run(w.actions._say("operator", "合成の個別回答"))
    assert len(w.client.calls) == 1


@pytest.mark.parametrize("error", [OSError("synthetic disk full"), ValueError("command_too_large")])
def test_unpublished_confirm_failure_replies_and_allows_retry(tmp_path, monkeypatch, error):
    w = world(tmp_path)
    actor = "lineworks:40029600:operator"
    payload = envelopes.request_create(actor, w.reg.token(TOKEN)["context"], {
        "title": "完全合成タスク", "reason": "合成の確認理由"})
    confirm_id = "b" * 16
    w.reg.put_confirm(confirm_id, {
        "actor": actor, "token": TOKEN, "origin": SCOPE, "payload": payload})
    publish = w.actions._publish

    async def failing(payload):
        raise error

    monkeypatch.setattr(w.actions, "_publish", failing)
    asyncio.run(w.actions._confirm(confirm_id, False, "operator", actor))
    assert w.client.calls[-1][1]["text"] == "送信に失敗しました。もう一度「確定する」を押してください。"
    assert command_files(w) == []
    assert w.reg.take_confirm(confirm_id, False) == "taken"
    w.reg.end_confirm(confirm_id)
    monkeypatch.setattr(w.actions, "_publish", publish)
    asyncio.run(w.actions._confirm(confirm_id, False, "operator", actor))
    assert w.client.calls[-1][1]["text"] == "受け付けました。"
    assert command_files(w) == [payload]
    assert w.reg.followup(payload["command_id"]) is not None


def test_rate_limited_followup_is_restored_for_the_next_sweep(tmp_path):
    """Regression: a 429 (nothing sent) lost the operation-result DM because
    the followup was dropped before send and only restored on sender_busy."""
    from adapters.lineworks.client import ClientError
    w = world(tmp_path, action="summary")
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    first = command_files(w)[0]
    _result(w, first["request_id"], {
        "request_id": first["request_id"], "outcome": "applied",
        "action": "summary", "body": "合成の要約"})
    w.client.error = ClientError("rate_limited", 429)
    with pytest.raises(ClientError, match="rate_limited"):
        asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(first["command_id"]) is not None
    w.client.error = None
    (Path(w.dirs["state"]) / "rate-limit.json").unlink()
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(first["command_id"]) is None
    assert "合成の要約" in w.client.calls[-1][1]["text"]
