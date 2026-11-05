"""Synthetic notification card dispatch, sweep and maintenance contracts."""
from __future__ import annotations

import notify_parts
import json
import os
import pytest
import notify_cards
import notify_card_renders
import notify_scope
import notify_spool
import notify_action_tokens
import notify_actions
import notify_reconcile
import notify_transport
from notify_testkit import (
    _settle_bodies,
    CFG, NOW, ORIGIN, SCOPE, _begin, _card, _dispatch, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _signal_row, _token_for, _uuid, led, pinned_clock,
)
from notify_testkit import delivered_source

from notify_card_test_support import (
    CFG_OFF, _deliver_card, _deliver_update, _deliverable, _delivered_card, _delivered_create, _settle_parts, _spec_json
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock", "v1_thread_bodies")


def test_dispatch_seals_and_publishes(led, tmp_path):
    _seed_thread(led)
    ev = _intent(led)
    out = _dispatch(led, ev)
    assert out["dispatched"] and out["cards"] == 1
    batch = led.db.execute(
        "SELECT * FROM notification_intent_batches WHERE event_id=?",
        (ev["event_id"],)).fetchone()
    assert batch is not None
    # the frozen payload is immutable even if notify_outbox mutates
    led.db.execute("UPDATE notify_outbox SET payload='{}' WHERE event_id=?",
                   (ev["event_id"],))
    card = _card(led)
    assert card["kind"] == "thread" and card["delivery_state"] == "pending"
    render = _latest_render(led)
    assert render["state"] == "queued" and render["spec_published"] == 1
    spec_file = (tmp_path / "data" / "discord_render"
                 / (render["delivery_id"] + ".json"))
    spec = json.loads(spec_file.read_text())
    assert spec["schema"] == "mcs-card-render/v1"
    assert spec["op"] == "create" and spec["render_rev"] == 1
    assert spec["delivery"]["route_epoch"] == 1
    assert len(spec["delivery"]["correlation"]) == 32
    # intent stays pending until its cards are delivered
    ev2 = led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                         (ev["event_id"],)).fetchone()
    assert ev2["state"] == "pending"


def test_intent_fanout_two_roots(led):
    _seed_thread(led, root=100, mids=(100,))
    _msg(led, 200, 1)                     # a second thread root
    ev = _intent(led, payload={"message_ids": [100, 200]})
    out = _dispatch(led, ev)
    assert out["cards"] == 2
    rows = led.db.execute(
        "SELECT * FROM notification_intent_cards WHERE event_id=?",
        (ev["event_id"],)).fetchall()
    assert len(rows) == 2 and all(r["state"] == "pending" for r in rows)


def test_dispatch_idempotent_reseal(led):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    out = _dispatch(led, ev)
    assert out["resealed"] is True and out["dispatched"] is False
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_cards").fetchone()["c"] == 1
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders").fetchone()["c"] == 1


@pytest.mark.parametrize("kind", ["thread", "signal_off"])
def test_sealed_reentry_reseats_pending_intent(led, kind):
    """U03-F01: a sealed intent still pending on re-entry (cards not yet
    delivered, or signals switched off) is re-examined after RESEAT_S,
    never left due on every flush."""
    if kind == "thread":
        _seed_thread(led)
        ev = _intent(led)
        cfg = CFG
    else:
        _patient(led, 1)
        _signal_row(led, "sig-r", pid=1)
        ev = _intent(led, kind="signal", pid=1,
                     payload={"signal_keys": ["sig-r"], "project_id": 1,
                              "type": "med_followup"})
        cfg = {"notify": CFG["notify"], "signals": {"notify": False}}
    _dispatch(led, ev)                        # sealed at NOW
    later = NOW + notify_cards.RESEAT_S + 5
    led.db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=?",
                   (later - 1, ev["event_id"]))
    led.db.commit()
    _dispatch(led, ev, cfg=cfg, now=later)
    row = led.db.execute("SELECT state,next_try FROM notify_outbox "
                         "WHERE event_id=?", (ev["event_id"],)).fetchone()
    assert row["state"] == "pending"
    assert row["next_try"] == later + notify_cards.RESEAT_S


def test_kill_switch_unsealed_reverts_sealed_stays(led):
    _seed_thread(led)
    ev1 = _intent(led)
    _dispatch(led, ev1)                       # sealed
    ev2 = _intent(led, payload={"message_ids": [100]})
    assert notify_cards.revert_to_text(led, ev2["event_id"])
    assert not notify_cards.revert_to_text(led, ev1["event_id"])  # sealed
    r2 = led.db.execute("SELECT route FROM notify_outbox WHERE event_id=?",
                        (ev2["event_id"],)).fetchone()
    assert r2["route"] == "text"
    r1 = led.db.execute("SELECT route FROM notify_outbox WHERE event_id=?",
                        (ev1["event_id"],)).fetchone()
    assert r1["route"] == "interactive"


def test_ack_assign_persist_and_defer_retired(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    assert "defer" not in {b["id"] for row in spec["parts"]["action_rows"]
                           for b in row}
    for action in ("ack", "assign"):
        tok = _token_for(spec, action)
        req = _notif(tok, actor="discord:1001")
        req["origin"] = dict(ORIGIN, message_id="m-9")
        r = notify_actions.apply_notification(led, req, CFG, now=NOW)
        assert r["outcome"] == "applied", (action, r)
        assert r["action"] == action
        # applied state must re-render immediately — a click can't wait
        # for the next tick sweep to become visible on the card
        assert r.get("delivery_id"), action
    tri = led.db.execute("SELECT * FROM notification_triage").fetchone()
    assert tri["state"] == "assigned" and tri["owner"] == "discord:1001"
    ack = led.db.execute(
        "SELECT actor,manifest_id FROM notification_acknowledgements"
    ).fetchone()
    assert ack["actor"] == "discord:1001"
    latest = _latest_render(led)
    assert latest["op"] == "update" and latest["state"] == "queued"
    latest_spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (latest["delivery_id"] + ".json")).read_text())
    footer = " ".join(
        b.get("text", "") for b in latest_spec["parts"]["footer"])
    assert "✅ 確認: <@1001>" in footer and "👤 担当: <@1001>" in footer
    # a ⏸ button still on an already-posted card answers, changes no
    # triage state and refreshes the card to the current buttons
    _begin(led, latest, n=2)
    _receipt(led, latest, f"{2:016x}", message_id="m-9", n=10)
    _settle_bodies(led, latest)
    tok = notify_action_tokens.mint(
        led.db, card["card_id"], "defer", None,
        {"source_gen": card["source_generation"]}, NOW)
    led.db.commit()
    before = _latest_render(led)["render_rev"]
    r = notify_actions.apply_notification(
        led, {**_notif(tok), "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "action_retired"
    assert led.db.execute("SELECT state FROM notification_triage"
                          ).fetchone()["state"] == "assigned"
    assert _latest_render(led)["render_rev"] == before + 1


def test_digest_card_null_project(led, tmp_path):
    _patient(led, 1, "患者A")
    _patient(led, 2, "患者B")
    for i, k in enumerate(("sig-a", "sig-b")):
        _signal_row(led, k, pid=1 + i)
    ev = _intent(led, kind="signal", pid=None,
                 payload={"digest": True, "type": "signal_digest",
                          "signal_keys": ["sig-a", "sig-b"],
                          "text": "2件"})
    out = _dispatch(led, ev)
    assert out["cards"] == 1
    card = _card(led)
    assert card["kind"] == "digest" and card["project_id"] is None
    render = _latest_render(led)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    assert spec["kind"] == "digest"
    manifest = led.db.execute(
        "SELECT shown,digest FROM notification_view_manifests").fetchone()
    assert manifest["digest"] == 1
    assert json.loads(manifest["shown"]) == ["sig-a", "sig-b"]


def test_digest_sealed_intent_not_merged(led):
    _patient(led, 1)
    _signal_row(led, "sig-x")
    ev = _intent(led, kind="signal", pid=None,
                 payload={"digest": True, "type": "signal_digest",
                          "signal_keys": ["sig-x"], "text": "1件"})
    _dispatch(led, ev)                        # sealed
    # a new digest member must NOT fold into the sealed intent
    import mcs_signals
    added, merged = mcs_signals._digest_add(
        led, "sig-y", NOW, {"notify_cooldown_d": 0.0}, 24)
    assert added == 1 and not merged
    rows = led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='signal' "
        "ORDER BY event_id").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[1]["payload"])["signal_keys"] == ["sig-y"]


def test_signal_card_member_lookup_no_fork(led):
    _patient(led, 1)
    for k in ("sig-m1", "sig-m2"):
        _signal_row(led, k, pid=1)
    ev1 = _intent(led, kind="signal", pid=1,
                  payload={"signal_keys": ["sig-m1"], "project_id": 1,
                           "type": "med_followup"})
    _dispatch(led, ev1)
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_cards").fetchone()["c"] == 1
    # a later intent naming a member of the same merged unit must land
    # on the same card — never fork a second signal card
    ev2 = _intent(led, kind="signal", pid=1,
                  payload={"signal_keys": ["sig-m1", "sig-m2"],
                           "project_id": 1, "type": "med_followup"})
    _dispatch(led, ev2)
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_cards").fetchone()["c"] == 1
    card = _card(led)
    anchor = json.loads(card["anchor_key"])
    assert set(anchor["signal_keys"]) == {"sig-m1", "sig-m2"}


def test_sweep_detects_edit_and_reissues(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rev_before = _latest_render(led)["render_rev"]
    led.db.execute("UPDATE messages SET body_text='編集後', "
                   "content_hash='hx' WHERE message_id=100")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    nxt = _latest_render(led)
    assert nxt["render_rev"] == rev_before + 1 and nxt["op"] == "update"
    card = _card(led)
    assert card["source_generation"] == 2


def test_sweep_revokes_archived_card(led):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    card = _card(led)
    assert card["delivery_state"] == "revoked"
    # never delivered -> nothing on Discord to delete: the queued create
    # is cancelled outright, no revoke render is issued
    r = _latest_render(led)
    assert r["render_rev"] == 1 and r["state"] == "cancelled"


def test_sweep_skips_content_for_settled_revoked_cards(
        led, tmp_path, monkeypatch):
    """Revoked cards stay in sweep's rotation forever; once nothing can
    be rendered (never delivered, or the delete already landed) the
    thread content must not be recomputed every tick."""
    calls = []
    real = notify_card_renders.card_content
    monkeypatch.setattr(notify_card_renders, "card_content",
                        lambda db, card, **kwargs: calls.append(card["card_id"])
                        or real(db, card, **kwargs))
    # (1) revoked before delivery: no message_id -> op None
    _seed_thread(led)
    _dispatch(led, _intent(led))
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    calls.clear()
    notify_cards.sweep(led, CFG)
    assert calls == []
    assert _latest_render(led)["render_rev"] == 1


def test_sweep_skips_content_after_revoke_delivered(
        led, tmp_path, monkeypatch):
    _delivered_card(led, tmp_path)
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    rv = _latest_render(led)
    assert rv["op"] == "revoke"
    _begin(led, rv, n=2)
    _receipt(led, rv, "0" * 15 + "2", message_id="m-9", n=10)
    assert _latest_render(led)["state"] == "delivered"
    calls = []
    real = notify_card_renders.card_content
    monkeypatch.setattr(notify_card_renders, "card_content",
                        lambda db, card, **kwargs: calls.append(card["card_id"])
                        or real(db, card, **kwargs))
    _msg(led, 105, 1, parent=100)            # drift is irrelevant now
    notify_cards.sweep(led, CFG)
    assert calls == []
    assert _latest_render(led)["render_rev"] == rv["render_rev"]


def test_queued_render_cancelled_when_stale(led):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    r1 = _latest_render(led)
    assert r1["state"] == "queued"
    _msg(led, 104, 1, parent=100)            # content drifts pre-send
    notify_cards.sweep(led, CFG)
    r1 = led.db.execute(
        "SELECT state FROM notification_renders WHERE render_rev=1"
    ).fetchone()
    assert r1["state"] == "cancelled"
    r2 = _latest_render(led)
    assert r2["render_rev"] == 2 and r2["state"] == "queued"


def test_snapshot_dirty_flag_lifecycle(led):
    _seed_thread(led)
    assert not notify_cards.snapshot_dirty(led)
    _dispatch(led, _intent(led))
    assert notify_cards.snapshot_dirty(led)
    notify_cards.clear_snapshot_dirty(led, notify_cards.snapshot_dirty(led))
    assert not notify_cards.snapshot_dirty(led)


def test_refresh_reissues_from_origin(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rev_before = _latest_render(led)["render_rev"]
    req = {"version": 1, "op": "refresh", "command_id": _uuid(40),
           "actor": "nurse-1",
           "origin": dict(ORIGIN, message_id="m-9")}
    r = notify_actions.apply_refresh(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied"
    assert _latest_render(led)["render_rev"] == rev_before + 1
    # unknown origin -> rejected
    r = notify_actions.apply_refresh(led, {
        "version": 1, "op": "refresh", "command_id": _uuid(41),
        "actor": "nurse-1", "origin": dict(ORIGIN, message_id="nope")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "card_not_found"


def test_update_and_revoke_specs_carry_message_identity(led, tmp_path):
    """update/revoke must name the bound message + thread so the worker
    aims without any registry lookup (registry loss must not orphan)."""
    nxt = _deliver_update(led, tmp_path)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (nxt["delivery_id"] + ".json")).read_text())
    assert spec["delivery"]["message_id"] == "m-9"
    # archive -> revoke render also carries it
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    r3 = _latest_render(led)
    assert r3["op"] == "revoke"
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (r3["delivery_id"] + ".json")).read_text())
    assert spec["delivery"]["message_id"] == "m-9"


def test_revoke_undelivered_card_completes_intent(led, tmp_path):
    """A card revoked before its first delivery must not leave the
    owning intent pending forever — coverage suppresses, intent ends."""
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    card = _card(led)
    assert card["delivery_state"] == "revoked"
    # never delivered -> no Discord message to delete -> no render
    assert _latest_render(led)["render_rev"] == 1
    ic = led.db.execute(
        "SELECT state FROM notification_intent_cards").fetchone()
    assert ic["state"] == "suppressed"
    ev2 = led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                         (ev["event_id"],)).fetchone()
    assert ev2["state"] == "suppressed"


def test_update_404_unbinds_and_recreates(led, tmp_path):
    """A proven message-gone on update must not livelock PATCHes —
    unbind, mark message_deleted, and the successor render is a fresh
    create."""
    nxt = _deliver_update(led, tmp_path)
    _begin(led, nxt, n=2)
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(9),
           "attempt_id": f"{2:016x}",
           "delivery_id": nxt["delivery_id"],
           "render_rev": nxt["render_rev"],
           "payload_hash": nxt["payload_hash"], "route_epoch": 1,
           "correlation": nxt["correlation"], **SCOPE,
           "result": "not_sent", "error_code": "http_404"}
    r = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
    assert r["applied"]
    card = _card(led)
    assert card["delivery_state"] == "message_deleted"
    assert card["message_id"] is None
    # the auto-issued successor re-creates instead of patching a ghost
    r3 = _latest_render(led)
    assert r3["render_rev"] == 3 and r3["op"] == "create"
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (r3["delivery_id"] + ".json")).read_text())
    assert "message_id" not in spec["delivery"]


def test_resend_budget_suspends_until_epoch_bump(led, tmp_path):
    """MAX_RESEND real not_sents suspend auto-retry as update_failed —
    a permanent Discord fault must not spin renders forever."""
    _deliverable(led, tmp_path)
    for i in range(1, notify_cards.MAX_RESEND + 1):
        r = _latest_render(led)
        assert r["state"] == "queued"
        _begin(led, r, n=10 + i)
        req = {"version": 1, "op": "transport_receipt",
               "command_id": _uuid(20 + i),
               "attempt_id": f"{10 + i:016x}",
               "delivery_id": r["delivery_id"],
               "render_rev": r["render_rev"],
               "payload_hash": r["payload_hash"], "route_epoch": 1,
               "correlation": r["correlation"], **SCOPE,
               "result": "not_sent", "error_code": "channel_not_found"}
        out = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
        assert out["applied"]
    card = _card(led)
    assert card["delivery_state"] == "update_failed"
    # suspended: no auto-successor while the epoch is unchanged
    assert _latest_render(led)["render_rev"] == notify_cards.MAX_RESEND
    notify_cards.sweep(led, CFG)
    assert _latest_render(led)["render_rev"] == notify_cards.MAX_RESEND
    # a route_epoch bump (operator changed routing config) re-opens it
    cfg2 = dict(CFG, notify=dict(CFG["notify"], route_epoch=2))
    notify_cards.sweep(led, cfg2)
    nxt = _latest_render(led)
    assert nxt["render_rev"] == notify_cards.MAX_RESEND + 1


@pytest.mark.parametrize("code", ["ratelimited", "rate_limited", "http_429"])
def test_rate_limited_not_sents_do_not_burn_retry_budget(led, tmp_path, code):
    """A 429 proves nothing was written — a burst of them must not park a
    never-delivered card as update_failed, or it is never sent again."""
    _deliverable(led, tmp_path)
    for i in range(1, notify_cards.MAX_RESEND + 2):
        r = _latest_render(led)
        assert r["state"] == "queued"
        _begin(led, r, n=10 + i)
        req = {"version": 1, "op": "transport_receipt",
               "command_id": _uuid(20 + i),
               "attempt_id": f"{10 + i:016x}",
               "delivery_id": r["delivery_id"],
               "render_rev": r["render_rev"],
               "payload_hash": r["payload_hash"], "route_epoch": 1,
               "correlation": r["correlation"], **SCOPE,
               "result": "not_sent", "error_code": code}
        assert notify_transport.apply_transport_receipt(
            led, req, CFG, now=NOW)["applied"]
    assert _card(led)["delivery_state"] == "pending"
    assert _latest_render(led)["render_rev"] == notify_cards.MAX_RESEND + 2


def test_denied_begins_do_not_burn_retry_budget(led, tmp_path):
    """denied_* begins are authorization outcomes, not send failures —
    they must never count toward MAX_RESEND."""
    render = _deliverable(led, tmp_path)
    for i in range(notify_cards.MAX_RESEND + 2):
        r = _begin(led, render, n=50 + i, cfg=CFG_OFF)
        assert r["error"] == "denied_interactive_off"
    card = _card(led)
    assert card["delivery_state"] == "pending"   # not update_failed
    r = _begin(led, render, n=60)
    assert r["granted"]


def test_refresh_on_revoked_card_rejected(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    notify_cards.revoke_card(led.db, card["card_id"], NOW)
    led.db.commit()
    before = dict(_latest_render(led))
    r = notify_actions.apply_refresh(led, {
        "version": 1, "op": "refresh", "command_id": _uuid(42),
        "actor": "nurse-1",
        "origin": dict(ORIGIN, message_id="m-9")}, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "card_revoked"
    # no fresh render minted by the refresh itself
    assert dict(_latest_render(led)) == before


def test_signal_without_source_holds_before_publishing_actions(led, tmp_path):
    """A source-less signal is held: no channel post or unusable modal token."""
    _patient(led, 1)
    _signal_row(led, "sig-nosrc")
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-nosrc"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    assert _latest_render(led) is None
    held = led.db.execute("SELECT state,progress FROM notify_outbox WHERE event_id=?",
                          (ev["event_id"],)).fetchone()
    assert held["state"] == "pending"
    assert json.loads(held["progress"])["thread_hold"] == "source_thread_not_ready"
    assert led.db.execute("SELECT COUNT(*) FROM notification_action_tokens").fetchone()[0] == 0
    assert not list((tmp_path / "data" / "discord_render").glob("*.json"))


def test_origin_must_match_bound_message(led, tmp_path):
    """A delivered card's buttons live on its bound message — a token
    replayed with a different message origin is refused even when the
    deployment scope matches (token alone resolves the card)."""
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "assign")
    r = notify_actions.apply_notification(
        led, {**_notif(tok),
              "origin": dict(ORIGIN, message_id="other-msg")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "origin_mismatch"
    # the bound message's own interaction still applies
    r = notify_actions.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{'cd' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "applied"


def test_message_gone_unbind_clears_thread(led, tmp_path):
    """A proven-gone bound message orphans its thread — the binding must
    not carry into the re-created card's next delivery."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "thread_id": "th-1"}, CFG, now=NOW)
    assert r["thread_state"] == "created"
    _msg(led, 103, 1, parent=100)          # drift -> update render
    notify_cards.sweep(led, CFG)
    upd = _latest_render(led)
    assert upd["op"] == "update"
    _begin(led, upd, n=61)
    r = notify_transport.apply_transport_receipt(led, {
        "version": 1, "op": "transport_receipt", "command_id": _uuid(62),
        "attempt_id": f"{61:016x}", "delivery_id": upd["delivery_id"],
        "render_rev": upd["render_rev"],
        "payload_hash": upd["payload_hash"], "route_epoch": 1,
        "correlation": upd["correlation"], **SCOPE,
        "result": "not_sent", "error_code": "http_404"}, CFG, now=NOW)
    assert r["applied"], r
    card = _card(led)
    assert card["delivery_state"] == "message_deleted"
    assert card["message_id"] is None
    assert card["thread_id"] is None and card["thread_state"] == "none"


def test_gc_removes_old_cmd_results(led, tmp_path):
    res_dir = tmp_path / "data" / "cmd_results"
    res_dir.mkdir(parents=True)
    old = res_dir / "old.json"
    old.write_text("{}")
    fresh = res_dir / "fresh.json"
    fresh.write_text("{}")
    stale = notify_action_tokens.TOKEN_WRITE_S + 100
    os.utime(old, (NOW - stale, NOW - stale))
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["result_files"] == 1
    assert not old.exists() and fresh.exists()


def test_gc_cmd_results_picks_by_age_not_name(led, tmp_path):
    """More than `limit` fresh files sorting before an old one must not
    hide it from GC."""
    res_dir = tmp_path / "data" / "cmd_results"
    res_dir.mkdir(parents=True)
    for i in range(5):
        (res_dir / f"0000-{i}.json").write_text("{}")
    old = res_dir / "ffff.json"
    old.write_text("{}")
    stale = notify_action_tokens.TOKEN_WRITE_S + 100
    os.utime(old, (NOW - stale, NOW - stale))
    out = notify_cards.gc(led, CFG, now=NOW, limit=2)
    assert out["result_files"] == 1
    assert not old.exists()
    assert len(list(res_dir.iterdir())) == 5


def test_sweep_detects_source_delete(led):
    """RC19 — an upstream message delete (body_state flip) re-renders
    the bound card with no new intent."""
    _seed_thread(led)
    r0 = _deliver_card(led)
    led.db.execute(
        "UPDATE messages SET body_state='deleted',content_hash=? "
        "WHERE message_id=101", ("d" * 64,))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 1
    assert r["op"] == "update"
    assert "（削除された投稿）" in json.dumps(r["spec_json"],
                                     ensure_ascii=False)


def test_sweep_detects_signal_lifecycle(led):
    """RC19 — resolve / dismiss / supersede transitions on a rendered
    signal re-render its card without a new intent."""
    _seed_thread(led)
    _signal_row(led, "sig-1", mids=[100])
    event = _intent(led, kind="signal",
                           payload={"signal_keys": ["sig-1"],
                                    "project_id": 1})
    _dispatch(led, event)
    delivered_source(led, card_id=2)
    _dispatch(led, event)
    r0 = _latest_render(led)
    assert _card(led)["kind"] == "signal"
    _begin(led, r0)
    _receipt(led, r0, f"{1:016x}", message_id="m-1")
    _settle_bodies(led, r0)

    # resolved — the evaluator's terminal transition
    _signal_row(led, "sig-1", state="resolved", mids=[100])
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 1
    assert r["op"] == "update"

    # superseded — a fresh open row on the same key
    _signal_row(led, "sig-1", state="open", mids=[100, 101])
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 2)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 2

    # dismissed — a human-gated transition row
    _signal_row(led, "sig-1", state="dismissed", mids=[100])
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 3)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 3


def test_signals_notify_off_cancels_queued_signal_render(led):
    """signals.notify=false: a queued signal render is cancelled and
    unbound instead of sitting live forever (begin denies it with a
    non-final signal_notify_off), no new render is issued while off, and
    re-enabling issues a fresh render with a new delivery_id."""
    _seed_thread(led)
    _signal_row(led, "sig-off", mids=[100])
    event = _intent(led, kind="signal",
                           payload={"signal_keys": ["sig-off"],
                                    "project_id": 1})
    _dispatch(led, event)
    delivered_source(led, card_id=2)
    _dispatch(led, event)
    r0 = _latest_render(led)
    assert r0["state"] == "queued"
    off = {"notify": CFG["notify"], "signals": {"notify": False}}

    notify_cards.sweep(led, off, now=NOW + 1)
    r = _latest_render(led)
    assert r["delivery_id"] == r0["delivery_id"]
    assert r["state"] == "cancelled"
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_intent_cards "
        "WHERE card_id=1 AND delivery_id IS NOT NULL").fetchone()["c"] == 0

    # drift while off still issues nothing
    _signal_row(led, "sig-off", state="resolved", mids=[100])
    led.db.commit()
    notify_cards.sweep(led, off, now=NOW + 2)
    assert _latest_render(led)["delivery_id"] == r0["delivery_id"]

    notify_cards.sweep(led, CFG, now=NOW + 3)
    r1 = _latest_render(led)
    assert r1["delivery_id"] != r0["delivery_id"]
    assert r1["state"] == "queued"
    assert r1["render_rev"] == r0["render_rev"] + 1
    assert _begin(led, r1)["granted"] is True


def test_signals_notify_off_leaves_thread_cards_alone(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r0 = _latest_render(led)
    notify_cards.sweep(led, {"notify": CFG["notify"],
                             "signals": {"notify": False}}, now=NOW + 1)
    assert _latest_render(led)["state"] == "queued"
    assert _latest_render(led)["delivery_id"] == r0["delivery_id"]


def test_sweep_defer_until_reopens_card(led):
    """RC19 — a deferred triage flips back to open at defer_until and
    the footer change re-renders the card."""
    _seed_thread(led)
    r0 = _deliver_card(led)
    led.db.execute(
        "INSERT INTO notification_triage(card_id,owner,defer_until,"
        "state,revision,last_actor,updated_at) "
        "VALUES(1,NULL,?,'deferred',1,'discord:1',?)",
        (NOW + 5, NOW))
    led.db.commit()
    # deferring itself is a footer change — the card re-renders to show
    # the hold before the deadline
    notify_cards.sweep(led, CFG, now=NOW + 1)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 1
    assert "保留中" in json.dumps(r["spec_json"], ensure_ascii=False)
    # past the deadline the hold lifts — another render drops the marker
    notify_cards.sweep(led, CFG, now=NOW + 10)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 2
    assert "保留中" not in json.dumps(r["spec_json"], ensure_ascii=False)
    tri = led.db.execute(
        "SELECT state FROM notification_triage WHERE card_id=1"
    ).fetchone()
    assert tri["state"] == "open"


@pytest.mark.parametrize('phase', ['queued', 'sending', 'delivered'])
def test_new_intent_with_unchanged_card_eventually_completes(led, tmp_path, phase):
    render = _deliverable(led, tmp_path)
    if phase != 'queued':
        _begin(led, render)
    if phase == 'delivered':
        _receipt(led, render, f'{1:016x}')
        _settle_bodies(led, render)
    # A later batch may cover a message already present on the thread card.
    event = _intent(led, payload={'message_ids': [101]})
    _dispatch(led, event)
    if phase == 'queued':
        _begin(led, render)
    if phase != 'delivered':
        _receipt(led, render, f'{1:016x}')
        _settle_bodies(led, render)
        # the covering update waits for the parts; a later sweep issues it
        notify_cards.sweep(led, CFG, now=NOW + 1)
    latest = _latest_render(led)
    if latest['state'] == 'queued':
        _begin(led, latest, n=2)
        _receipt(led, latest, f'{2:016x}', n=10)
        _settle_bodies(led, latest)
    assert led.db.execute('SELECT state FROM notify_outbox WHERE event_id=?',
                          (event['event_id'],)).fetchone()['state'] == 'accepted'


def test_bounded_sweep_visits_unchanged_cards_fairly(led, tmp_path):
    _delivered_card(led, tmp_path)
    _msg(led, 200)
    ev = _intent(led, payload={'message_ids': [200]})
    _dispatch(led, ev)
    second = _latest_render(led, 2)
    _begin(led, second, n=2)
    _receipt(led, second, f'{2:016x}', message_id='m-2', n=10)
    _settle_bodies(led, second)
    led.db.execute("UPDATE messages SET body_text='変更',content_hash=? WHERE message_id=200",
                   ('f' * 64,))
    led.db.commit()
    notify_cards.sweep(led, CFG, limit=1, now=NOW + 10)
    notify_cards.sweep(led, CFG, limit=1, now=NOW + 20)
    assert _latest_render(led, 2)['render_rev'] > second['render_rev']


def test_old_route_epoch_cannot_grant_and_is_reissued(led, tmp_path):
    render = _deliverable(led, tmp_path)
    cfg2 = dict(CFG, notify=dict(CFG['notify'], route_epoch=2))
    denied = _begin(led, render, cfg=cfg2)
    assert not denied['granted']
    notify_cards.sweep(led, cfg2, now=NOW + 1)
    replacement = _latest_render(led)
    assert replacement['route_epoch'] == 2
    assert replacement['state'] == 'queued'
    assert replacement['delivery_id'] != render['delivery_id']


@pytest.mark.parametrize('field', ['profile', 'guild_id'])
def test_refresh_rejects_other_deployment_scope(led, tmp_path, field):
    _delivered_card(led, tmp_path)
    rev = _latest_render(led)['render_rev']
    origin = dict(ORIGIN, message_id='m-9')
    origin[field] = 'other'
    out = notify_actions.apply_refresh(led, {
        'version': 1, 'op': 'refresh', 'command_id': _uuid(900),
        'actor': 'nurse-1', 'origin': origin}, CFG, now=NOW)
    assert out['outcome'] == 'rejected'
    assert _latest_render(led)['render_rev'] == rev


def test_failed_revoke_stays_revoked_after_retry_exhaustion(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    notify_cards.revoke_card(led.db, card['card_id'], NOW)
    led.db.commit()
    # Emit the first delete; subsequent not_sent receipts retry it.
    specs = []
    with led.db:
        notify_cards.issue_render(led.db, card['card_id'], CFG, NOW, specs)
    for i in range(notify_cards.MAX_RESEND):
        render = _latest_render(led)
        assert render['op'] == 'revoke'
        _begin(led, render, n=100 + i)
        out = notify_transport.apply_transport_receipt(led, {
            'version': 1, 'op': 'transport_receipt', 'command_id': _uuid(200 + i),
            'attempt_id': f'{100 + i:016x}', 'delivery_id': render['delivery_id'],
            'render_rev': render['render_rev'], 'payload_hash': render['payload_hash'],
            'route_epoch': 1, 'correlation': render['correlation'], **SCOPE,
            'result': 'not_sent', 'error_code': 'http_403'}, CFG, now=NOW)
        assert out['applied']
    assert _card(led)['delivery_state'] == 'revoked'
    exhausted = _latest_render(led)['render_rev']
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert _latest_render(led)['render_rev'] == exhausted
    # Explicit routing recovery still retries a DELETE, never an UPDATE.
    cfg2 = dict(CFG, notify=dict(CFG['notify'], route_epoch=2))
    notify_cards.sweep(led, cfg2, now=NOW + 2)
    assert _latest_render(led)['op'] == 'revoke'
    assert _latest_render(led)['state'] == 'queued'


def test_success_resets_consecutive_resend_budget(led, tmp_path):
    _deliverable(led, tmp_path)
    for i in range(1, notify_cards.MAX_RESEND):
        render = _latest_render(led)
        _begin(led, render, n=400 + i)
        notify_transport.apply_transport_receipt(led, {
            'version': 1, 'op': 'transport_receipt', 'command_id': _uuid(500 + i),
            'attempt_id': f'{400 + i:016x}', 'delivery_id': render['delivery_id'],
            'render_rev': render['render_rev'], 'payload_hash': render['payload_hash'],
            'route_epoch': 1, 'correlation': render['correlation'], **SCOPE,
            'result': 'not_sent', 'error_code': 'http_503'}, CFG, now=NOW)
    render = _latest_render(led)
    _begin(led, render, n=450)
    _receipt(led, render, f'{450:016x}')
    _settle_bodies(led, render)
    led.db.execute("UPDATE messages SET body_text='追記',content_hash=? WHERE message_id=100",
                   ('e' * 64,))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    update = _latest_render(led)
    _begin(led, update, n=451)
    notify_transport.apply_transport_receipt(led, {
        'version': 1, 'op': 'transport_receipt', 'command_id': _uuid(551),
        'attempt_id': f'{451:016x}', 'delivery_id': update['delivery_id'],
        'render_rev': update['render_rev'], 'payload_hash': update['payload_hash'],
        'route_epoch': 1, 'correlation': update['correlation'], **SCOPE,
        'result': 'not_sent', 'error_code': 'http_503'}, CFG, now=NOW + 2)
    assert _card(led)['delivery_state'] == 'delivered'
    assert _latest_render(led)['state'] == 'queued'
    assert _latest_render(led)['render_rev'] > update['render_rev']


def test_interactive_acceptance_queues_ready_attachments_once(led, tmp_path):
    render = _deliverable(led, tmp_path)
    synthetic = tmp_path / 'synthetic.txt'
    synthetic.write_text('synthetic attachment')
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,state,downloaded_at) "
        "VALUES(100,'f1','synthetic.txt',?,'downloaded',?)", (str(synthetic), NOW - 1))
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,state) "
        "VALUES(101,'f2','pending.txt','pending')")
    led.db.commit()
    _begin(led, render)
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind='attachment_followup'").fetchone()
    _receipt(led, render, f'{1:016x}')
    for _ in range(2):
        rows = led.db.execute(
            "SELECT payload,route,state FROM notify_outbox WHERE kind='attachment_followup'").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]['payload'])['message_id'] == 100
        assert rows[0]['route'] == 'text' and rows[0]['state'] == 'pending'
        # Repeated completion or worker receipts must never resend the attachment.
        with led.db:
            notify_cards.complete_intent(led.db, 1, NOW + 1)


def test_bounded_gc_advances_past_deleted_terminal_spec_files(led, tmp_path):
    first = _deliverable(led, tmp_path)
    _begin(led, first)
    _receipt(led, first, f'{1:016x}')
    _msg(led, 200)
    _dispatch(led, _intent(led, payload={'message_ids': [200]}))
    second = _latest_render(led, 2)
    _begin(led, second, n=2)
    _receipt(led, second, f'{2:016x}', n=10, message_id='m-2')
    paths = [tmp_path / 'data' / 'discord_render' / (r['delivery_id'] + '.json')
             for r in (first, second)]
    assert all(path.exists() for path in paths)
    # parts pending keeps each spec replayable for restart-resume —
    # settle the plans so gc can advance (limit=1 covers one file per pass)
    for r in (first, second):
        led.db.execute(
            "UPDATE notification_render_parts SET state='delivered' "
            "WHERE delivery_id=?", (r["delivery_id"],))
        notify_parts.update_state(led.db, r["delivery_id"], NOW)
    led.db.commit()
    notify_cards.gc(led, CFG, now=NOW + 10, limit=1)
    notify_cards.gc(led, CFG, now=NOW + 20, limit=1)
    assert not any(path.exists() for path in paths)


def test_gc_eligible_payload_is_not_starved_by_unsettled_card(led, tmp_path):
    first = _deliverable(led, tmp_path)
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", ('e' * 64,))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert led.db.execute('SELECT state FROM notification_renders WHERE delivery_id=?',
                          (first['delivery_id'],)).fetchone()[0] == 'cancelled'
    # A second card's cancelled payload has a delivered successor and is eligible.
    _msg(led, 200)
    _dispatch(led, _intent(led, payload={'message_ids': [200]}))
    old_second = _latest_render(led, 2)
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=200", ('d' * 64,))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 2)
    second = _latest_render(led, 2)
    _begin(led, second, n=2)
    _receipt(led, second, f'{2:016x}', message_id='m-2', n=10)
    assert notify_cards.gc(led, CFG, now=NOW + 3, limit=1)['spec_json_cleared'] == 1
    assert led.db.execute('SELECT spec_json FROM notification_renders WHERE delivery_id=?',
                          (old_second['delivery_id'],)).fetchone()[0] is None
    assert led.db.execute('SELECT spec_json FROM notification_renders WHERE delivery_id=?',
                          (first['delivery_id'],)).fetchone()[0] is not None


def test_gc_keeps_delivered_body_until_parts_settle(led, tmp_path):
    render = _delivered_create(led, tmp_path)
    assert json.loads(_spec_json(led, render["delivery_id"])
                      )["parts"]["thread_body_parts"]
    # thread/body parts still pending — the body stays replayable
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_json_cleared"] == 0
    assert "本文" in _spec_json(led, render["delivery_id"])
    parts_before = led.db.execute(
        "SELECT part_id,payload_sha256,state FROM notification_render_parts "
        "WHERE delivery_id=? ORDER BY part_id",
        (render["delivery_id"],)).fetchall()
    _settle_parts(led, render)
    assert notify_cards.gc(led, CFG, now=NOW + 2)["spec_json_cleared"] == 1
    row = led.db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()
    assert row["spec_json"] is None and row["state"] == "delivered"
    assert (row["payload_hash"], row["correlation"]) == (
        render["payload_hash"], render["correlation"])
    assert [(p[0], p[1]) for p in led.db.execute(
        "SELECT part_id,payload_sha256 FROM notification_render_parts "
        "WHERE delivery_id=? ORDER BY part_id",
        (render["delivery_id"],)).fetchall()] == [
        (p[0], p[1]) for p in parts_before]
    # a repeated gc is a no-op
    assert notify_cards.gc(led, CFG, now=NOW + 3)["spec_json_cleared"] == 0


def test_gc_keeps_delivered_body_while_card_attempt_unsettled(led, tmp_path):
    render = _delivered_create(led, tmp_path)
    _settle_parts(led, render)
    _msg(led, 103, 1, parent=100)          # drift -> update render
    notify_cards.sweep(led, CFG)
    nxt = _latest_render(led)
    assert nxt["op"] == "update"
    assert _begin(led, nxt, n=2)["granted"]
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_json_cleared"] == 0
    assert _spec_json(led, render["delivery_id"]) is not None
    _receipt(led, nxt, f"{2:016x}", result="unknown", n=10)
    assert notify_cards.gc(led, CFG, now=NOW + 2)["spec_json_cleared"] == 0
    assert _spec_json(led, render["delivery_id"]) is not None


def test_gc_keeps_delivered_body_under_restore_hold(led, tmp_path):
    render = _delivered_create(led, tmp_path)
    _settle_parts(led, render)
    led.db.execute(
        "INSERT INTO notification_restore_holds(card_id,delivery_id,"
        "reason,held_at) VALUES(1,?,'synthetic',?)",
        (render["delivery_id"], NOW))
    led.db.commit()
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_json_cleared"] == 0
    assert _spec_json(led, render["delivery_id"]) is not None
    notify_cards.release_holds(led.db, card_id=1, now=NOW + 2)
    led.db.commit()
    assert notify_cards.gc(led, CFG, now=NOW + 3)["spec_json_cleared"] == 1
    assert _spec_json(led, render["delivery_id"]) is None


def test_card_updates_revokes_and_reconciles_after_body_gc(led, tmp_path):
    render = _delivered_create(led, tmp_path)
    _settle_parts(led, render)
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_json_cleared"] == 1
    _msg(led, 103, 1, parent=100)          # stale card after gc
    notify_cards.sweep(led, CFG)
    nxt = _latest_render(led)
    assert nxt["op"] == "update" and nxt["render_rev"] == 2
    spec = json.loads((tmp_path / "data" / "discord_render"
                       / (nxt["delivery_id"] + ".json")).read_text())
    assert spec["delivery"]["message_id"] == "m-9"
    assert _begin(led, nxt, n=2)["granted"]
    _receipt(led, nxt, f"{2:016x}", message_id="m-9", n=10)
    card = _card(led)
    assert card["applied_render_rev"] == 2
    assert card["delivery_state"] == "delivered"
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    r3 = _latest_render(led)
    assert r3["op"] == "revoke" and r3["state"] == "queued"
    assert _begin(led, r3, n=3)["granted"]
    _receipt(led, r3, f"{3:016x}", message_id="m-9", n=11)
    assert _latest_render(led)["state"] == "delivered"
    out = notify_reconcile.reconcile_after_restore(led, CFG, now=NOW + 5)
    assert not out["held"]
    assert not led.db.execute(
        "SELECT 1 FROM notification_restore_holds").fetchone()


def test_switched_away_transport_leaves_live_health(led, tmp_path,
                                                   monkeypatch):
    """Regression: after notify.interactive moved from discord to slack,
    a discord attempt left 'unknown' and its held event kept health
    degraded forever. They stay recorded (never auto-settled) but count
    only as retired."""
    import health_report
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev)
    _begin(led, _latest_render(led), n=1)
    led.db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL "
                   "WHERE event_id=?", (ev["event_id"],))
    led.db.execute("INSERT OR IGNORE INTO notification_intent_batches("
                   "event_id,frozen_payload,payload_hash,route_epoch,"
                   "sealed_at,transport,scope_json) VALUES(?,?,?,?,?,?,?)",
                   (ev["event_id"], "{}", "h", 1, NOW, "discord", "{}"))
    led.db.commit()
    slack = {"notify": {**CFG["notify"], "interactive": "slack"}}
    assert notify_scope.health_cards(led.db, CFG)["attempts_unsettled"] == 1
    h = notify_scope.health_cards(led.db, slack)
    assert (h["attempts_unsettled"], h["retired_unsettled"]) == (0, 1)
    assert h["unsettled"] == []
    assert [i["transport"] for i in h["retired_items"]] == ["discord"]
    assert h["retired_items"][0]["resolve_scope"]["channel_id"]
    # a claim left on the retired transport is disclosed, not an error
    root = notify_spool.data_root(led)
    notify_spool.ensure_dirs(root)
    claim = os.path.join(notify_spool.notify_dirs(root)["discord_render"],
                         "x.json.claimed")
    open(claim, "w").close()
    os.utime(claim, (NOW - 3600, NOW - 3600))
    for cfg, live in ((slack, 0), (CFG, 1)):
        result = {"errors": []}
        fixed = notify_spool.recover(led, cfg, result)
        assert fixed["missing_claimed"] == live
        assert bool(result["errors"]) == bool(live)
    os.remove(claim)
    assert notify_scope.health_cards(led.db)["attempts_unsettled"] == 1
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "h.json"))
    for cfg, held, retired in ((CFG, 1, 0), (slack, 0, 1)):
        health = health_report.build_health(led.db, {"errors": [], "notify": {}}, "ok", cfg=cfg)
        assert (health["notify"]["held"],
                health["notify"]["retired_held"]) == (held, retired)
        assert (health["notify"]["state"] == "incomplete") == bool(held)
    assert led.db.execute("SELECT state FROM notification_delivery_attempts"
                          ).fetchone()[0] == "granted"
