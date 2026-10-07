"""Patient signals use proven source threads and keep their existing controls."""
import json
import itertools

import pytest

import notify_cards
import notify_transport
from adapters.common.spec import validate
from notify_testkit import (CFG, NOW, _begin, _dispatch, _intent,
                            _latest_render, _receipt, _seed_thread,
                            _settle_bodies, _signal_row, led)

__all__ = ["led"]
_SOURCE_SEQ = itertools.count(8001, 3)


def delivered_source(led, *, card_id=1, mids=(100, 101), cfg=CFG):
    """Real synthetic original-card grant, transport receipt and thread receipt."""
    pid = led.db.execute("SELECT project_id FROM messages WHERE message_id=?", (mids[0],)).fetchone()[0]
    serial = next(_SOURCE_SEQ)
    source = _intent(led, pid=pid, payload={"message_ids": list(mids)})
    _dispatch(led, source, cfg)
    render = _latest_render(led, card_id)
    scope = notify_cards.delivery_scope(cfg)
    version = 2 if scope.get("transport") == "slack" else 1
    root = "1790000000.000001" if version == 2 else "original-root"
    thread = root if version == 2 else "original-thread"
    from notify_testkit import _uuid
    identity = {"version": version, **scope, "delivery_id": render["delivery_id"],
                "render_rev": render["render_rev"], "payload_hash": render["payload_hash"],
                "route_epoch": 1, "correlation": render["correlation"]}
    assert notify_transport.apply_transport_begin(led, {
        **identity, "op": "transport_begin", "command_id": _uuid(serial),
        "attempt_id": f"{serial:016x}", "worker_id": "bb" * 8}, cfg, now=NOW)["granted"]
    assert notify_transport.apply_transport_receipt(led, {
        **identity, "op": "transport_receipt", "command_id": _uuid(serial + 1),
        "attempt_id": f"{serial:016x}", "result": "delivered", "message_id": root}, cfg, now=NOW)["applied"]
    assert notify_transport.apply_part_receipt(led, {
        **identity, "op": "part_receipt", "command_id": _uuid(serial + 2),
        "attempt_id": "p:" + render["delivery_id"].replace("-", "") + ":thread",
        "part_id": "thread", "result": "delivered", "remote_id": thread}, cfg, now=NOW)["applied"]
    _settle_bodies(led, render)
    return source


def _signal(led):
    _signal_row(led, "synthetic-key", mids=[101])
    event = _intent(led, "signal", payload={"signal_keys": ["synthetic-key"], "project_id": 1})
    _dispatch(led, event)
    return event, dict(led.db.execute("SELECT * FROM notification_cards WHERE kind='signal'").fetchone())


def test_signal_reuses_origin_thread_and_keeps_controls(led):
    _seed_thread(led)
    delivered_source(led)
    _, card = _signal(led)
    render = _latest_render(led, card["card_id"])
    spec = json.loads(render["spec_json"])
    validate(spec)
    assert spec["parts"]["source_thread"] is True
    assert spec["delivery"]["thread_id"] == "original-thread"
    assert "message_id" not in spec["delivery"]
    assert {b["id"] for row in spec["parts"]["action_rows"] for b in row} >= {"ack", "assign", "dismiss", "request"}
    assert _begin(led, render, n=8100)["granted"]
    assert _receipt(led, render, f"{8100:016x}", message_id="signal-reply", n=8101)["applied"]
    _settle_bodies(led, render)
    # A later presentation update edits this reply, without creating a nested thread.
    specs = []
    with led.db:
        notify_cards._issue_render(led.db, card["card_id"], CFG, NOW + 1, specs, force=True)
    assert specs[0]["op"] == "update"
    assert specs[0]["delivery"]["message_id"] == "signal-reply"
    assert specs[0]["delivery"]["thread_id"] == "original-thread"


@pytest.mark.parametrize("fault", ["missing", "coverage", "deleted", "scope", "foreign", "partial"])
def test_unproven_signal_target_holds_without_channel_spec(led, fault):
    _seed_thread(led)
    if fault != "missing":
        delivered_source(led)
    with led.db:
        if fault == "coverage":
            led.db.execute("UPDATE notification_intent_cards SET coverage='[999]' ")
        if fault == "deleted":
            led.db.execute("UPDATE notification_cards SET thread_state='deleted'")
        if fault == "scope":
            led.db.execute("UPDATE notification_cards SET channel_id='unrelated'")
        if fault == "foreign":
            led.db.execute("UPDATE messages SET project_id=2 WHERE message_id=101")
        if fault == "partial":
            led.db.execute("UPDATE messages SET body_state='partial' WHERE message_id=101")
    event, card = _signal(led)
    assert _latest_render(led, card["card_id"]) is None
    row = led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
    assert row["state"] == "pending" and json.loads(row["progress"])["thread_hold"] == "source_thread_not_ready"


@pytest.mark.parametrize("op", ["create", "update"])
def test_changed_thread_denies_already_sealed_signal(led, op):
    _seed_thread(led)
    delivered_source(led)
    _, card = _signal(led)
    render = _latest_render(led, card["card_id"])
    if op == "update":
        assert _begin(led, render, n=8210)["granted"]
        assert _receipt(led, render, f"{8210:016x}", message_id="signal-reply", n=8211)["applied"]
        _settle_bodies(led, render)
        with led.db:
            notify_cards._issue_render(led.db, card["card_id"], CFG, NOW + 1, [], force=True)
        render = _latest_render(led, card["card_id"])
        assert render["op"] == "update"
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='changed-thread' WHERE kind='thread'")
    result = _begin(led, render, n=8200)
    assert not result["granted"]
    assert result["error"] == "denied_source_thread_changed"


def test_held_signal_resumes_same_event_after_original_thread_receipt(led):
    _seed_thread(led)
    event, card = _signal(led)
    assert _latest_render(led, card["card_id"]) is None
    delivered_source(led, card_id=2)
    assert _dispatch(led, event)["dispatched"]
    render = _latest_render(led, card["card_id"])
    assert json.loads(render["spec_json"])["delivery"]["thread_id"] == "original-thread"
    _dispatch(led, event)
    assert led.db.execute("SELECT count(*) FROM notification_renders WHERE card_id=?", (card["card_id"],)).fetchone()[0] == 1


def test_lineworks_signal_is_explicitly_held_without_retry_or_fallback(led):
    _seed_thread(led)
    _signal_row(led, "synthetic-key", mids=[100])
    event = _intent(led, "signal", payload={"signal_keys": ["synthetic-key"], "project_id": 1})
    cfg = {"notify": {"interactive": "lineworks", "lineworks": {
        "profile": "synthetic", "application_id": "1", "team_id": "2", "channel_id": "3"}},
        "signals": {"notify": True}}
    assert _dispatch(led, event, cfg)["held"] == "lineworks_thread_unsupported"
    row = led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "lineworks_thread_unsupported"
    assert led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["unchanged", "message_deleted", "thread_deleted", "thread_changed", "evidence_unknown"])
def test_delivered_signal_revoke_uses_sealed_remote_binding_after_source_changes(led, fault):
    _seed_thread(led)
    delivered_source(led)
    _, card = _signal(led)
    created = _latest_render(led, card["card_id"])
    assert _begin(led, created, n=9100)["granted"]
    assert _receipt(led, created, f"{9100:016x}", message_id="signal-reply", n=9101)["applied"]
    _settle_bodies(led, created)
    specs = []
    with led.db:
        if fault == "message_deleted":
            led.db.execute("UPDATE messages SET body_state='deleted',body_text='' WHERE message_id=101")
        elif fault == "thread_deleted":
            led.db.execute("UPDATE notification_cards SET thread_state='deleted' WHERE kind='thread'")
        elif fault == "thread_changed":
            led.db.execute("UPDATE notification_cards SET thread_id='changed-thread' WHERE kind='thread'")
        elif fault == "evidence_unknown":
            led.db.execute("UPDATE artifacts SET content='{}' WHERE kind='signal_v1'")
        notify_cards.revoke_card(led.db, card["card_id"], NOW + 1)
        notify_cards._issue_render(led.db, card["card_id"], CFG, NOW + 1, specs, force=True)
    assert specs[0]["op"] == "revoke"
    assert specs[0]["delivery"]["message_id"] == "signal-reply"
    assert specs[0]["delivery"]["thread_id"] == "original-thread"
    assert [p["kind"] for p in specs[0]["parts"]["manifest"]] == ["card"]
    revoked = _latest_render(led, card["card_id"])
    assert _begin(led, revoked, n=9200)["granted"]
    assert _receipt(led, revoked, f"{9200:016x}", message_id="signal-reply", n=9201)["applied"]
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?", (revoked["delivery_id"],)).fetchone()[0] == "delivered"


@pytest.mark.parametrize("fault,error", [("channel_id", "scope_mismatch"), ("payload_hash", "hash_mismatch"), ("render_rev", "rev_mismatch"), ("route_epoch", "epoch_mismatch")])
def test_signal_revoke_keeps_sealed_identity_and_scope_gates(led, fault, error):
    from notify_testkit import SCOPE, _uuid
    _seed_thread(led)
    delivered_source(led)
    _, card = _signal(led)
    created = _latest_render(led, card["card_id"])
    assert _begin(led, created, n=9300)["granted"]
    assert _receipt(led, created, f"{9300:016x}", message_id="signal-reply", n=9301)["applied"]
    _settle_bodies(led, created)
    specs = []
    with led.db:
        notify_cards.revoke_card(led.db, card["card_id"], NOW + 1)
        notify_cards._issue_render(led.db, card["card_id"], CFG, NOW + 1, specs, force=True)
    render = _latest_render(led, card["card_id"])
    request = {"version": 1, "op": "transport_begin", "command_id": _uuid(9400),
               "attempt_id": f"{9400:016x}", "worker_id": "bb" * 8,
               "delivery_id": render["delivery_id"], "render_rev": render["render_rev"],
               "payload_hash": render["payload_hash"], "route_epoch": 1, **SCOPE}
    request[fault] = {"channel_id": "unrelated", "payload_hash": "0" * 64,
                      "render_rev": render["render_rev"] + 1, "route_epoch": 2}[fault]
    result = notify_transport.apply_transport_begin(led, request, CFG, now=NOW + 1)
    assert not result["granted"] and result["error"] == "denied_" + error
