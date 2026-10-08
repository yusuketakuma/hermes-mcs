"""Synthetic Slack signal replies retain their source root through delivery and private views."""
import asyncio
import json

import pytest

import notify_cards
from adapters.common import envelopes, registry
from adapters.common.spec import token_map
from adapters.slack.delivery import DeliveryWorker
from notify_testkit import _dispatch, _intent, _latest_render, _seed_thread, _signal_row
from slack_testkit import SLACK, SCOPE, _granted_card, _mkworld
from test_mcs_slack import _attach, isolated_slack_ledger as led
from test_mcs_slack_actions import TOKEN, TS, fixture, result

__all__ = ["led"]


def test_signal_worker_grants_parts_resume_and_update_original_thread(led, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards, "drug_search_available", lambda _db: True)
    _seed_thread(led)
    aid, attachment, blob = _attach(led, tmp_path, mid=101, name="synthetic-signal.bin")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    source = dict(led.db.execute("SELECT * FROM notification_cards WHERE kind='thread'").fetchone())
    root = source["thread_id"]
    # Signal producers normally plan keys only; seal a file here to exercise the worker's existing part contract.
    plan_attachments = notify_cards._plan_attachments
    monkeypatch.setattr(notify_cards, "_plan_attachments", lambda db, shown:
                        plan_attachments(db, [101] if "synthetic-med-followup" in shown else shown))
    _signal_row(led, "synthetic-med-followup", mids=[101])
    event = _intent(led, "signal", payload={"signal_keys": ["synthetic-med-followup"], "project_id": 1})
    assert _dispatch(led, event, SLACK)["dispatched"]
    card_id = led.db.execute("SELECT card_id FROM notification_cards WHERE kind='signal'").fetchone()[0]
    render = _latest_render(led, card_id)
    spec = json.loads(render["spec_json"])
    assert spec["delivery"]["thread_id"] == root
    assert sum(part["kind"] == "attachment_part" for part in spec["parts"]["manifest"]) == 1
    before = len(world.client.thread_posts)
    asyncio.run(_granted_card(world.worker, led, world.root))
    card = dict(led.db.execute("SELECT * FROM notification_cards WHERE card_id=?", (card_id,)).fetchone())
    assert card["message_id"] != root and card["thread_id"] == root
    assert all(post["thread_ts"] == root for post in world.client.thread_posts[before:])
    assert len(world.client.upload_calls) == 1
    upload = world.client.upload_calls[0]
    assert upload["thread_ts"] == root and upload["channel"] == SCOPE["channel_id"]
    assert upload["filename"] == attachment.name and upload["file"] == blob
    parts = led.db.execute("SELECT kind,state,remote_id FROM notification_render_parts WHERE delivery_id=?",
                           (render["delivery_id"],)).fetchall()
    assert all(part["state"] == "delivered" for part in parts)
    assert next(part["remote_id"] for part in parts if part["kind"] == "thread") == root
    assert led.db.execute("SELECT remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (render["delivery_id"], f"attach:{aid:04d}")).fetchone()[0].startswith("F_SYNTHETIC")
    tokens = [token for token, ctx in token_map(spec).items() if ctx["action"] == "drugsearch"]
    assert tokens
    for token in tokens:
        pin = world.reg.token(token)
        assert pin["message_id"] == pin["verified_card_message_id"] == card["message_id"]
        assert pin["verified_thread_id"] == root

    restored = registry.Registry(world.dirs["state"], scope=SCOPE)
    restored._data["parts"].pop(spec["delivery_id"], None)
    for token in tokens:
        restored._data["tokens"][token].pop("verified_thread_id", None)
        restored._data["tokens"][token].pop("verified_card_message_id", None)
    replacement = DeliveryWorker(sender=world.sender, settings=SCOPE, root=str(world.root), reg=restored,
                                 worker_id=registry.new_worker_id(), log=lambda *_args, **_kwargs: None)
    before = len(world.client.thread_posts)
    asyncio.run(replacement._resume_parts(spec))
    assert len(world.client.thread_posts) == before
    assert len(world.client.upload_calls) == 1
    assert all(restored.token(token)["verified_thread_id"] == root for token in tokens)

    # A presentation refresh retains the card reply and source root.
    with led.db:
        specs = []
        notify_cards._issue_render(led.db, card_id, SLACK, notify_cards.time.time(), specs, force=True)
    notify_cards._publish_specs(led.db, notify_cards.notify_dirs(str(world.root)), specs, notify_cards.time.time())
    update = json.loads(_latest_render(led, card_id)["spec_json"])
    assert update["op"] == "update"
    assert update["delivery"]["message_id"] == card["message_id"]
    assert update["delivery"]["thread_id"] == root
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.thread_posts) == before
    assert len(world.client.upload_calls) == 1
    assert _latest_render(led, card_id)["state"] == "delivered"
    assert any(method == "update" and call["ts"] == card["message_id"] for method, call in world.client.calls)
    assert all(post["thread_ts"] == root for post in world.client.thread_posts)


@pytest.mark.parametrize("action", ["meds", "drugsearch"])
@pytest.mark.parametrize("binding", ["matching", "missing", "foreign_card"])
def test_drug_followup_binds_source_root_and_clicked_card(tmp_path, action, binding):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind=action)
        root = "1790000000.000999"
        pin = {**reg.token(TOKEN), "card_key": "synthetic-signal", "kind": "signal",
               "verified_thread_id": root}
        if binding != "missing":
            pin["verified_card_message_id"] = TS if binding == "matching" else "1790000000.000888"
        reg.put_tokens({TOKEN: pin})
        origin = {**{key: actions._settings[key] for key in SCOPE}, "message_id": TS}
        env = {"command_id": "synthetic-drug", "request_id": "synthetic-drug"}
        actor = "slack:T_SYNTHETIC:U_OPERATOR"
        await actions._queue_followup(env, origin, actor, TOKEN, "U_OPERATOR")
        nav_token = "e" * 32
        nav = {key: pin[key] for key in ("card_key", "kind", "project_id", "channel_id", "team_id", "message_id")}
        nav.update(action="meds", actor=actor, ephemeral=True)
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied", action="body",
               body="完全合成の薬剤詳細", token_ctx={nav_token: nav}, navigation=[{
                   "id": "meds", "ui": "button", "label": "次へ", "token": nav_token}])
        await actions.sweep_followups()
        threaded = [message for message in app.client.messages if message.get("thread_ts")]
        if binding == "matching":
            assert threaded and all(message["thread_ts"] == root for message in threaded)
            nav_pin = registry.Registry(dirs["state"], scope={key: actions._settings[key] for key in SCOPE}).token(nav_token)
            assert nav_pin["verified_thread_id"] == root
            assert nav_pin["verified_card_message_id"] == TS
        else:
            assert not threaded
            assert "完全合成の薬剤詳細" not in "".join(message["text"] for message in app.client.messages)
            assert reg.token(nav_token) is None
    asyncio.run(scenario())


def test_card_reply_itself_cannot_prove_signal_body_delivery(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    world = _mkworld(led)
    from slack_card_testkit import _spec
    spec = _spec()
    root, card = "1790000000.000999", TS
    spec["delivery"]["thread_id"] = root
    spec["parts"]["action_rows"][0][0]["id"] = "drugsearch"
    spec["parts"]["manifest"] = [{"kind": "body_part", "part_id": "body:0001"}]
    world.reg.put_tokens({TOKEN: {**token_map(spec)[TOKEN], "message_id": card, "team_id": SCOPE["team_id"]}})
    aid = envelopes.part_attempt_id(spec["delivery_id"], "body:0001")
    world.worker._pin_active_thread({"spec": spec}, {"card_message_id": card}, {
        aid: [{"phase": "result", "result": "delivered", "remote_id": card}]})
    assert "verified_thread_id" not in world.reg.token(TOKEN)
