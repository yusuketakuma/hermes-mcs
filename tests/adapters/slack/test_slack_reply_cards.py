"""Slack reply cards (layout 4, notify.reply_cards): each later reply gets
its own card inside the root card's thread; existing cards keep collecting.
Synthetic ledger and fake Slack client only."""
import asyncio
import json

import pytest

import notify_cards
import notify_spool
from notify_testkit import NOW, _dispatch, _intent, _msg, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]

REPLY = {**SLACK, "notify": {**SLACK["notify"], "reply_cards": True}}


def _cards(led):
    return [dict(r) for r in led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id")]


def _spec(led, card_id):
    row = led.db.execute("SELECT spec_json FROM notification_renders WHERE card_id=? "
                         "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()
    return json.loads(row[0])


def _deliver(world, led):
    asyncio.run(_granted_card(world.worker, led, world.root))


def test_late_reply_gets_its_own_card_in_the_root_thread(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led), REPLY, now=clock[0])["dispatched"]
    world = _mkworld(led)
    _deliver(world, led)
    (root,) = _cards(led)
    assert root["layout"] == notify_cards.CARD_LAYOUT and root["delivery_state"] == "delivered"
    assert root["thread_id"] == root["message_id"]
    before = len(world.client.thread_posts)

    clock[0] += 3600
    _msg(led, 202, parent=100, body="合成の後から届いた返信", ts=int(clock[0]))
    intent = _intent(led, payload={"message_ids": [202]})
    assert _dispatch(led, intent, REPLY, now=clock[0])["dispatched"]
    notify_spool.publish_flags(REPLY, str(world.root))
    _deliver(world, led)

    root, reply = _cards(led)
    assert reply["card_key"].endswith("|reply:202")
    assert json.loads(reply["anchor_key"]) == {"root": 100, "source": 202}
    assert reply["layout"] == notify_cards.CARD_LAYOUT and reply["delivery_state"] == "delivered"
    # Reply cards remain inside the verified root thread.
    assert reply["thread_id"] == root["message_id"] != reply["message_id"]
    posts = world.client.thread_posts[before:]
    card_posts = [p for p in posts if p.get("blocks")]
    assert len(card_posts) == 1 and card_posts[0]["thread_ts"] == root["message_id"]
    assert not any(p.get("reply_broadcast") for p in posts)
    assert not any("新しい返信をカードに追加しました。" in (p.get("text") or "") for p in posts)

    spec = _spec(led, reply["card_id"])
    assert spec["parts"]["source_thread"] is True
    assert [p["original"] for p in spec["parts"]["slack"]["face_posts"]] == ["合成の後から届いた返信"]
    root_spec = _spec(led, root["card_id"])
    texts = [c.get("text") or "" for c in root_spec["parts"]["containers"]]
    assert "↳ 返信 1件を保存済み" in texts
    assert "合成の後から届いた返信" not in json.dumps(root_spec, ensure_ascii=False)
    # the stamp row on each card aims at the post that card shows
    root_content = notify_cards.card_content(led.db, root, cfg=REPLY)
    reply_content = notify_cards.card_content(led.db, reply, cfg=REPLY)
    assert root_content["shown"] == [100] and reply_content["shown"] == [202]
    assert notify_cards._stamp_target(led.db, reply, reply_content)[0] == 202
    assert notify_cards._stamp_target(led.db, root, root_content)[0] == 100


def test_reply_card_waits_until_the_root_thread_exists(led):
    _seed_thread(led, mids=(100, 101))
    assert _dispatch(led, _intent(led), REPLY, now=NOW)["dispatched"]
    root, reply = _cards(led)
    assert reply["card_key"].endswith("|reply:101")
    # no render for the reply until its root card has a delivered thread
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE card_id=?",
                          (reply["card_id"],)).fetchone()[0] == 0
    progress = led.db.execute("SELECT progress FROM notify_outbox").fetchone()[0]
    assert json.loads(progress)["thread_hold"] == "source_thread_not_ready"


def test_existing_layout_three_root_keeps_identity_and_replies_use_its_thread(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK, now=clock[0])["dispatched"]
    world = _mkworld(led)
    _deliver(world, led)
    (root,) = _cards(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET layout=3 WHERE card_id=?", (root["card_id"],))
    clock[0] += 3600
    _msg(led, 202, parent=100, body="合成の返信", ts=int(clock[0]))
    intent = _intent(led, payload={"message_ids": [202]})
    _dispatch(led, intent, REPLY, now=clock[0])
    notify_spool.publish_flags(REPLY, str(world.root))
    _deliver(world, led)
    root, reply = _cards(led)
    assert root["layout"] == 3 and root["message_id"] == root["thread_id"]
    assert reply["card_key"].endswith("|reply:202") and reply["thread_id"] == root["message_id"]
    assert reply["message_id"] != root["message_id"] and reply["delivery_state"] == "delivered"
    assert "合成の返信" not in json.dumps(_spec(led, root["card_id"])["parts"]["containers"], ensure_ascii=False)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (intent["event_id"],)).fetchone()[0] == "accepted"
    assert not any(p.get("reply_broadcast") for p in world.client.thread_posts)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_reply_card_switch_must_be_boolean(value):
    import mcs_config
    assert any("reply_cards" in e for e in mcs_config.validate_notify({"reply_cards": value}))


def test_a_layout_four_root_keeps_reply_cards_after_the_switch_is_off(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    assert _dispatch(led, _intent(led), REPLY, now=clock[0])["dispatched"]
    # A deployed layout-four root keeps its contract when the switch changes.
    with led.db:
        led.db.execute("UPDATE notification_cards SET layout=4")
    clock[0] += 3600
    _msg(led, 202, parent=100, body="合成の返信", ts=int(clock[0]))
    _dispatch(led, _intent(led, payload={"message_ids": [202]}), SLACK, now=clock[0])
    root, reply = _cards(led)
    assert root["layout"] == 4 and reply["card_key"].endswith("|reply:202")
    assert reply["layout"] == notify_cards.CARD_LAYOUT


def test_slack_reply_cards_stay_enabled_with_threads_disabled():
    off = {**REPLY, "notify": {**REPLY["notify"], "card_thread": False}}
    assert notify_cards.reply_cards_on(REPLY) is True
    assert notify_cards.reply_cards_on(off) is True


def test_each_layout_four_card_acts_on_and_tracks_only_its_own_post(led, monkeypatch):
    import notify_action_context
    import notify_render
    clock = [NOW]
    monkeypatch.setattr(notify_cards.time, "time", lambda: clock[0])
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led), REPLY, now=clock[0])
    clock[0] += 60
    _msg(led, 201, parent=100, body="合成の返信1", ts=int(clock[0]))
    _dispatch(led, _intent(led, payload={"message_ids": [201]}), REPLY, now=clock[0])
    root, first = _cards(led)
    assert notify_action_context.render_context(led.db, root)["source_message_id"] == 100
    assert notify_action_context.render_context(led.db, first)["source_message_id"] == 201
    before = {c["card_id"]: notify_render.source_fingerprint(led.db, c) for c in (root, first)}
    # a later reply never changes what the earlier cards render or act on
    clock[0] += 60
    _msg(led, 202, parent=100, body="合成の返信2", ts=int(clock[0]))
    assert {c["card_id"]: notify_render.source_fingerprint(led.db, c)
            for c in (root, first)} == before


def test_slack_reply_cards_stay_enabled_when_option_is_off():
    import re
    from pathlib import Path
    source = Path(notify_cards.__file__).read_text(encoding="utf-8")
    assert re.search(r"^REPLY_CARDS_DEFAULT = True$", source, re.M)
    off = {**REPLY, "notify": {**REPLY["notify"], "reply_cards": False}}
    assert notify_cards.reply_cards_on(off) is True


@pytest.mark.parametrize('layout', [1, 2, 3, 4])
def test_slack_legacy_root_renders_only_parent(led, layout):
    import notify_render
    from notify_parts import _thread_plan_ids
    _seed_thread(led, mids=(100,))
    _msg(led, 201, parent=100, body="SYNTHETIC-REPLY-ONLY")
    _dispatch(led, _intent(led, payload={"message_ids": [100, 201]}), REPLY, now=NOW)
    root = _cards(led)[0]
    card = {**dict(root), "layout": layout, "transport": "slack"}
    content = notify_render.card_content(led.db, card, cfg=REPLY)
    assert content["shown"] == [100]
    assert "SYNTHETIC-REPLY-ONLY" not in json.dumps(content, ensure_ascii=False)
    assert _thread_plan_ids(led.db, card, content["shown"]) == [100]


def test_unmarked_queued_legacy_slack_render_is_not_granted(led):
    import notify_transport
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led), REPLY, now=NOW)
    render = led.db.execute('SELECT * FROM notification_renders ORDER BY render_rev DESC LIMIT 1').fetchone()
    spec = json.loads(render['spec_json'])
    assert spec['parts']['slack_thread_policy'] == 1
    req = {**spec['delivery'], 'transport': 'slack', 'delivery_id': render['delivery_id'],
           'payload_hash': render['payload_hash'], 'render_rev': render['render_rev']}
    assert notify_transport._begin_check(led.db, req, REPLY) is None
    del spec['parts']['slack_thread_policy']
    led.db.execute('UPDATE notification_renders SET spec_json=? WHERE delivery_id=?',
                   (json.dumps(spec), render['delivery_id']))
    led.db.commit()
    assert notify_transport._begin_check(led.db, req, REPLY) == 'slack_thread_policy_changed'
    row = led.db.execute('SELECT state FROM notification_renders WHERE delivery_id=?',
                         (render['delivery_id'],)).fetchone()
    assert row['state'] == 'queued'


@pytest.mark.parametrize("blocking_state", [None, "granted", "unknown", "seeded", "unproven", "no_reason"])
def test_old_unsent_policy_is_denied_and_resealed_in_one_transaction(led, blocking_state):
    import hashlib
    from adapters.common.envelopes import canonical
    from mcs_util import payload_hash
    import notify_transport

    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led), REPLY, now=NOW)
    render = led.db.execute('SELECT * FROM notification_renders ORDER BY render_rev DESC LIMIT 1').fetchone()
    spec = json.loads(render['spec_json'])
    del spec['parts']['slack_thread_policy']
    if blocking_state in ('seeded', 'no_reason'):
        spec['parts']['manifest'].append({'part_id': 'attachment:synthetic', 'kind': 'attachment_part',
                                          'unavailable': True})
    visible = {key: spec['parts'][key] for key in ('containers', 'footer', 'action_rows')}
    for key in ('preview_text', 'processing_display_version'):
        if key in spec['parts']:
            visible[key] = spec['parts'][key]
    body = canonical(visible)
    spec['parts']['manifest'][0].update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
    digest = payload_hash(spec)
    led.db.execute('UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?',
                   (json.dumps(spec), digest, render['delivery_id']))
    if blocking_state in ('seeded', 'unproven', 'no_reason'):
        led.db.execute(
            "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,error_code,updated_at) "
            "VALUES(?,?,?,99,'not_sent',?,?)",
            (render['delivery_id'], 'attachment:synthetic', 'attachment_part',
             None if blocking_state == 'no_reason' else 'attachment_unavailable', NOW))
    if blocking_state in ('granted', 'unknown'):
        led.db.execute(
            "INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,state,worker_id,created_at) "
            "VALUES(?,?,?,?,?,?)", ('0000000000008888', render['delivery_id'],
                                      '00000000-0000-4000-8000-000000008888', blocking_state, 'aa' * 8, NOW))
    led.db.commit()
    req = {**spec['delivery'], 'transport': 'slack', 'delivery_id': render['delivery_id'],
           'payload_hash': digest, 'render_rev': render['render_rev'], 'version': 1,
           'op': 'transport_begin', 'command_id': '00000000-0000-4000-8000-000000009999',
           'attempt_id': '0000000000009999', 'worker_id': 'bb' * 8}
    result = notify_transport.apply_transport_begin(led, req, REPLY, now=NOW)
    assert result['granted'] is False
    assert result.get('error') == 'denied_slack_thread_policy_changed', result
    rows = led.db.execute('SELECT state,spec_json FROM notification_renders ORDER BY render_rev').fetchall()
    if blocking_state in ('granted', 'unknown', 'unproven', 'no_reason'):
        assert [row['state'] for row in rows] == ['queued']
    else:
        assert [row['state'] for row in rows] == ['cancelled', 'queued']
        assert json.loads(rows[-1]['spec_json'])['parts']['slack_thread_policy'] == 1
    assert notify_transport.apply_transport_begin(led, req, REPLY, now=NOW) == result
    assert led.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == len(rows)
