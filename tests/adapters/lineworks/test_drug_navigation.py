"""Synthetic drug navigation stays in the clicker's LINE WORKS 1:1 talk."""
import asyncio
import json

import ledger
import notify_cards
import notify_cmds
from adapters.lineworks import delivery
from adapters.common import registry
from notify_testkit import NOW, _dispatch, _intent, _llm_extract, _seed_thread
from test_lineworks_adapter import (
    CONFIG, SCOPE, TOKEN, command_files, event, inbox, raw, signature, world)


def test_navigation_is_durable_before_first_private_chunk_and_actor_bound(tmp_path, monkeypatch):
    w = world(tmp_path, action="meds")
    token, actor = "c" * 32, "lineworks:40029600:operator"
    context = {"action": "meds", "project_id": 1, "actor": actor, "ephemeral": True}
    result = {"outcome": "applied", "action": "list",
              "list": {"title": "完全合成の薬剤", "notes": ["合" * 4000]},
              "navigation": [{"id": "meds", "ui": "button", "label": "次の薬剤",
                              "token": token, "style": "secondary"}],
              "token_ctx": {token: context}}
    rec = {"kind": "action", "origin": {**SCOPE, "message_id": "lw:" + "d" * 32},
           "actor": actor}
    send = w.client.send_message

    def checked_send(content, **targets):
        restored = registry.Registry(w.dirs["state"], scope=w.settings)
        assert restored.token(token)["actor"] == actor
        return send(content, **targets)

    monkeypatch.setattr(w.client, "send_message", checked_send)
    asyncio.run(w.actions._deliver_followup("operator", rec, result))
    assert w.client.calls[0][1]["type"] == "flex"
    assert all(call[1]["type"] == "text" for call in w.client.calls[1:])
    assert all(call[2] == {"user_id": "operator", "channel_id": None} for call in w.client.calls)
    assert w.actions._pinned(token, actor) is not None
    assert w.actions._pinned(token, "lineworks:40029600:other") is None
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + token, user="other", channel=None)))
    assert command_files(w) == []
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + token, channel=None)))
    assert command_files(w)[0]["origin"]["message_id"] == rec["origin"]["message_id"]


def test_drug_next_page_and_post_callback_reach_runner_and_private_clicker(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    w = world(tmp_path, action="meds")
    led = ledger.Ledger(str(w.data / "ledger.db"))
    try:
        _seed_thread(led)
        medication = {"dose": "5mg", "action": "start", "subject": "patient",
                      "status": "current", "negated": False, "unverified": False,
                      "evidence": "fictional quotation only"}
        _llm_extract(led, 100, {"meds": [{**medication, "name": "架空前投稿薬"}]})
        _llm_extract(led, 101, {"meds": [
            {**medication, "name": f"架空薬{i}"} for i in range(1, 7)]})
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(w.data),
                                         reg=w.reg, worker_id=registry.new_worker_id(),
                                         log=lambda *a, **kw: None)
        box = inbox(tmp_path)

        def drain():
            result = {"errors": []}
            notify_cmds.drain_int_commands(led, result, CONFIG, str(w.data))
            assert result["errors"] == []

        async def callback(token, user="operator"):
            body = raw(event(postback="mcs:a:" + token, channel=None, user=user))
            assert box.accept(body, signature(body), SCOPE["application_id"]) == 200
            working, accepted = box.take(box.pending()[0])
            await w.actions.handle(accepted)
            box.finish(working, "processed")

        def private_buttons():
            return [button["action"] for _, content, _ in w.client.calls
                    if content["type"] == "flex"
                    for button in content["contents"]["footer"]["contents"]]

        async def scenario():
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                drain()
                await worker.tick()
                drain()
                meds = next(token for token, context in w.reg._data["tokens"].items()
                            if token != TOKEN and context["action"] == "meds")
                w.client.calls.clear()
                await callback(meds)
                drain()
                await w.actions.sweep_followups()
                assert w.client.calls
                first_buttons = private_buttons()
                next_page = next(button for button in first_buttons if button["label"] == "次の5件")
                token = next_page["postback"].removeprefix("mcs:a:")
                before = len(command_files(w))
                await callback(token, user="other")
                assert len(command_files(w)) == before
                w.client.calls.clear()
                await callback(token)
                drain()
                await w.actions.sweep_followups()
                assert "架空薬6" in json.dumps(w.client.calls, ensure_ascii=False)
                post = next(button for button in private_buttons() if button["label"] == "古い投稿")
                w.client.calls.clear()
                await callback(post["postback"].removeprefix("mcs:a:"))
                drain()
                await w.actions.sweep_followups()
                assert "架空前投稿薬" in json.dumps(w.client.calls, ensure_ascii=False)
                assert all(call[2] == {"user_id": "operator", "channel_id": None}
                           for call in w.client.calls)
            finally:
                worker.release_scope_lock()

        asyncio.run(scenario())
    finally:
        led.close()
