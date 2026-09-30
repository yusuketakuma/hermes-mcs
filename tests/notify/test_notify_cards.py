"""Interactive notification cards — durable ledger, dispatch, grant,
settle and operator-resolve contracts. Synthetic fixtures + a temp DB
only; no Discord, no network, no real MCS data (plan RC02-09,13,21,
23-25)."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

import notify_cards
import notify_reconcile
import notify_render
import notify_transport
import notify_cmds
from notify_testkit import (
    _settle_bodies,
    CFG, NOW, ORIGIN, SCOPE, _begin, _card, _dispatch, _extract, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _signal_row, _token_for, _uuid, led,
)

__all__ = ["led"]  # shared isolated-ledger fixture

NO_THREAD_CFG = {"notify": {k: v for k, v in CFG["notify"].items()
                            if k != "card_thread"},
                 "signals": CFG["signals"]}
CFG_OFF = {"notify": {"interactive": "off", "route_epoch": 1,
                      "discord": CFG["notify"]["discord"]}}


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


# ---------- dispatch / seal / freeze (RC02, RC03) ----------

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
    assert out.get("resealed") or not out.get("dispatched")
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


# ---------- begin / grant (RC06, RC07, RC24) ----------

def _deliverable(led, tmp_path, cfg=CFG):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev, cfg=cfg)
    return _latest_render(led)


def test_begin_grants_once_and_replays(led, tmp_path):
    render = _deliverable(led, tmp_path)
    r = _begin(led, render)
    assert r["granted"] and r["attempt_state"] == "granted"
    assert r["correlation"] == render["correlation"]
    again = _begin(led, render)              # same command_id
    assert again["granted"] and again["attempt_id"] == r["attempt_id"]
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_delivery_attempts"
    ).fetchone()["c"] == 1
    # a DIFFERENT begin for the same delivery is denied, durably
    r2 = _begin(led, render, n=2)
    assert not r2["granted"] and r2["error"] == "denied_not_queued"
    row = led.db.execute(
        "SELECT state,error_code FROM notification_delivery_attempts "
        "WHERE begin_command_id=?", (_uuid(2),)).fetchone()
    assert row["state"] == "not_sent" and row["error_code"].startswith(
        "denied_")


def test_begin_denies_bad_hash_rev_epoch_scope(led, tmp_path):
    render = _deliverable(led, tmp_path)
    for i, mut in enumerate(
            ({"payload_hash": "0" * 64}, {"render_rev": 9},
             {"route_epoch": 7}, {"channel_id": "chX"})):
        req = {"version": 1, "op": "transport_begin",
               "command_id": _uuid(10 + i), "attempt_id": f"{9 + i:016x}",
               "worker_id": "cc" * 8, "delivery_id": render["delivery_id"],
               "render_rev": render["render_rev"],
               "payload_hash": render["payload_hash"], "route_epoch": 1,
               **SCOPE, **mut}
        r = notify_transport.apply_transport_begin(led, req, CFG, now=NOW)
        assert not r["granted"], mut
        assert r["error"].startswith("denied_")


def test_begin_denies_when_interactive_off(led, tmp_path):
    render = _deliverable(led, tmp_path)
    r = _begin(led, render, cfg=CFG_OFF)
    assert not r["granted"] and r["error"] == "denied_interactive_off"


def test_begin_unknown_delivery(led):
    r = notify_transport.apply_transport_begin(led, {
        "version": 1, "op": "transport_begin", "command_id": _uuid(3),
        "attempt_id": "f" * 16, "worker_id": "cc" * 8,
        "delivery_id": _uuid(99), "render_rev": 1,
        "payload_hash": "0" * 64, "route_epoch": 1, **SCOPE},
        CFG, now=NOW)
    assert not r["granted"] and r["error"] == "denied_unknown_delivery"


# ---------- settle (RC08) ----------

def test_receipt_delivered_binds_message_and_accepts_intent(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    r = _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    assert r["applied"]
    card = _card(led)
    assert card["delivery_state"] == "delivered"
    assert card["message_id"] == "m-9"
    ev = led.db.execute("SELECT state,accepted_ref FROM notify_outbox"
                        ).fetchone()
    assert ev["state"] == "accepted" and ev["accepted_ref"] == "cards:1"
    # the result file the plugin consumes exists via drain path; the
    # render row itself carries the durable verdict
    assert _latest_render(led)["state"] == "delivered"


def test_receipt_echo_mismatch_rejected(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(9),
           "attempt_id": "0" * 15 + "1",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": "f" * 64, "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "result": "delivered", "message_id": "m-9"}
    r = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
    assert not r["applied"] and r["error"] == "payload_hash_mismatch"


def test_receipt_not_sent_requeues_render(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    r = _receipt(led, render, "0" * 15 + "1", result="not_sent",
                 message_id=None, n=9)
    # not_sent requires an error_code — missing one is invalid input
    assert not r["applied"] and r["error"] == "error_code_required"
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(9),
           "attempt_id": "0" * 15 + "1",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "result": "not_sent", "error_code": "channel_not_found"}
    r = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
    assert r["applied"]
    # the card goes back to pending and a successor render is issued
    card = _card(led)
    assert card["delivery_state"] == "pending"
    nxt = _latest_render(led)
    assert nxt["render_rev"] == 2 and nxt["state"] == "queued"
    # the unbound coverage row stays pending on the new render
    ic = led.db.execute(
        "SELECT state,delivery_id,required_render_rev FROM "
        "notification_intent_cards").fetchone()
    assert ic["state"] == "pending" and ic["delivery_id"] == \
        nxt["delivery_id"]


def test_receipt_conflicting_verdict_rejected(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    # a second receipt flipping the verdict is a conflict, never a
    # silent overwrite
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(10),
           "attempt_id": "0" * 15 + "1",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "result": "not_sent", "error_code": "late_claim"}
    r = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
    assert not r["applied"] and r["conflict"]
    assert _card(led)["delivery_state"] == "delivered"


def test_unknown_blocks_resend_until_resolved(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(9),
           "attempt_id": "0" * 15 + "1",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "result": "unknown"}
    r = notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)
    assert r["applied"]
    assert _card(led)["delivery_state"] == "delivery_unknown"
    # no successor render while the truth is unknown
    assert _latest_render(led)["render_rev"] == 1
    # and a fresh begin on a re-issued render cannot happen: the
    # unsettled attempt still owns the card
    notify_cards.sweep(led, CFG)
    assert _latest_render(led)["render_rev"] == 1


# ---------- notification actions (RC09) ----------

def _delivered_card(led, tmp_path, cfg=CFG):
    render = _deliverable(led, tmp_path, cfg=cfg)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    return _card(led), spec



def test_ack_assign_persist_and_defer_retired(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    assert "defer" not in {b["id"] for row in spec["parts"]["action_rows"]
                           for b in row}
    for action in ("ack", "assign"):
        tok = _token_for(spec, action)
        req = _notif(tok, actor="discord:1001")
        req["origin"] = dict(ORIGIN, message_id="m-9")
        r = notify_cards.apply_notification(led, req, CFG, now=NOW)
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
    tok = notify_cards._mint_token(
        led.db, card["card_id"], "defer", None,
        {"source_gen": card["source_generation"]}, NOW)
    led.db.commit()
    before = _latest_render(led)["render_rev"]
    r = notify_cards.apply_notification(
        led, {**_notif(tok), "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "action_retired"
    assert led.db.execute("SELECT state FROM notification_triage"
                          ).fetchone()["state"] == "assigned"
    assert _latest_render(led)["render_rev"] == before + 1


def test_body_action_returns_full_text(led, tmp_path):
    """📄本文表示 is a view action: it answers the untruncated text of
    the shown set and mutates nothing — no render, no triage rows.
    The button ships only where no companion thread carries the body
    (card_thread off here), so exercise that surface."""
    long_body = "詳細な記録。" * 40           # ~200 chars > MAX_SNIPPET
    _patient(led, 1)
    _msg(led, 100, 1, body=long_body)
    _msg(led, 101, 1, parent=100, body="短い返信")
    ev = _intent(led)
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    tok = _token_for(spec, "body")
    n_renders = led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders").fetchone()["c"]
    req = _notif(tok)
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_cards.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    assert r["title"].endswith("本文")
    assert long_body in r["body"] and "短い返信" in r["body"]
    # view-only: no re-render is issued and nothing is mutated
    assert "delivery_id" not in r
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders"
    ).fetchone()["c"] == n_renders
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_triage"
    ).fetchone()["c"] == 0


def test_card_renders_headers_not_bodies(led, tmp_path):
    """Thread cards show only per-message header lines (time + sender) —
    no body text at all; 📄本文表示 serves the untruncated set. Pages
    still pack by rendered length under the Components-V2 ceiling."""
    _patient(led, 1)
    body = "記録の本文です。" * 40
    _msg(led, 100, 1, body=body)
    for m in range(101, 114):                # 13 msgs > PAGE_THREAD
        _msg(led, m, 1, parent=100, body=body)
    ev = _intent(led, payload={"message_ids": list(range(100, 114))})
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    assert c["pages"] > 1                    # PAGE_THREAD count cap
    seen = set()
    for p in range(c["pages"]):
        card["ui_state"] = json.dumps({"page": p})
        c = notify_render._card_content(led.db, card)
        assert c["page"] == p
        total = (notify_render._blocks_len(c["containers"])
                 + notify_render._blocks_len(c["footer"]))
        assert total <= 4000, (p, total)     # hard Discord ceiling
        joined = "\n".join(b.get("text") or "" for b in c["containers"])
        assert "記録の本文です" not in joined  # no body text on the card
        assert "職員" in joined               # sender headers remain
        seen.update(c["shown"])
    assert seen == set(range(100, 114))      # nothing dropped


def test_card_body_oversized_headers_and_full_action(led, tmp_path):
    """A huge message renders as a bare header line on the card;
    📄本文表示 answers with the full body (bounded by BODY_MAX_CHARS
    with an explicit omission marker)."""
    _patient(led, 1)
    long_body = "長い記録。" * 1400            # ~7000 chars > BODY_MAX
    _msg(led, 100, 1, body=long_body)
    _msg(led, 101, 1, parent=100, body="短い")
    ev = _intent(led)
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    card = _card(led)
    c = notify_render._card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    total = (notify_render._blocks_len(c["containers"])
             + notify_render._blocks_len(c["footer"]))
    assert total <= 4000
    assert set(c["shown"]) == {100, 101}
    assert "長い記録" not in joined and "短い" not in joined
    assert "職員" in joined                   # header lines only
    # the button still serves the untruncated set (BODY_MAX_CHARS cap)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    req = _notif(_token_for(spec, "body"))
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_cards.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    assert "（省略" in r["body"] and "長い記録。" in r["body"]
    assert len(r["body"]) <= notify_render.BODY_MAX_CHARS + 80


def test_card_page_indicator_shows_position(led, tmp_path):
    """F05: a multi-page card must display order and count, not just
    nav buttons — the reader must see which page they are on."""
    _patient(led, 1)
    body = "記録の本文です。" * 40
    _msg(led, 100, 1, body=body)
    for m in range(101, 114):
        _msg(led, m, 1, parent=100, body=body)
    ev = _intent(led, payload={"message_ids": list(range(100, 114))})
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    assert c["pages"] > 1
    ftxt = "\n".join(b.get("text") or "" for b in c["footer"])
    assert "ページ" in ftxt and "全14件" in ftxt
    # last page shows the final range
    card["ui_state"] = json.dumps({"page": c["pages"] - 1})
    c = notify_render._card_content(led.db, card)
    ftxt = "\n".join(b.get("text") or "" for b in c["footer"])
    assert f"{c['pages']}/{c['pages']} ページ" in ftxt
    # single-page card carries no page line at all
    _patient(led, 2)
    _msg(led, 200, 2)
    ev2 = _intent(led, pid=2, payload={"message_ids": [200]})
    _dispatch(led, ev2)
    card2 = dict(led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id DESC LIMIT 1"
        ).fetchone())
    c2 = notify_render._card_content(led.db, card2)
    assert c2["pages"] == 1
    assert "ページ" not in "\n".join(
        b.get("text") or "" for b in c2["footer"])


def test_fit_item_field_shrinks_as_last_resort(led):
    """Pathological input (huge field value, no shrinkable text) still
    fits the page budget — an over-budget item must never make the
    whole spec fail validation and render no card at all."""
    blocks = [{"type": "field", "name": "患者",
               "value": "名前" * 4000}]
    out = notify_render._fit_item(blocks)
    assert notify_render._blocks_len(out) <= notify_render.PAGE_TEXT_BUDGET
    assert "省略" in out[0]["value"]


# ---------- structured-data block on cards ----------


def test_card_thread_shows_structured_lines(led, tmp_path):
    """A message renders its structured block under a header, not its body."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛", "悪寒"],
                        "rx_actions": [{"action": "start",
                                        "ctx": "オキシコドン"}]})
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    texts = [b.get("text") or "" for b in c["containers"]]
    struct = [t for t in texts if t.startswith("📋 構造化")]
    assert struct and "症状" in struct[0] and "疼痛" in struct[0]
    # the header line remains alongside the structured block; the raw
    # body itself stays off the card (📄本文表示 serves it)
    assert any("職員" in t for t in texts) and not any(
        "本文" in t for t in texts)


def test_card_thread_structured_per_message(led, tmp_path):
    """Structured data binds to its own message — a second message's
    extraction must not bleed into the first message's block."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]})
    _extract(led, 101, {"v": 1, "symptoms": ["悪寒"]})
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    for p in range(9):
        card["ui_state"] = json.dumps({"page": p})
        c = notify_render._card_content(led.db, card)
        joined = "\n".join(b.get("text") or "" for b in c["containers"])
        if 100 in c["shown"]:
            assert "疼痛" in joined
        if 101 in c["shown"]:
            assert "悪寒" in joined
        if c["pages"] == p + 1:
            break


def test_card_stale_and_bad_extraction_not_shown(led, tmp_path):
    """An artifact bound to an older body revision (hash mismatch) or
    malformed content is never rendered as current structured data."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]}, stale=True)
    _extract(led, 101, None)                      # malformed JSON
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "📋 構造化" not in joined and "疼痛" not in joined
    # the card still renders the message headers (bodies stay off-card)
    assert "職員" in joined and "本文" not in joined


def test_card_deleted_message_hides_structured_data(led, tmp_path):
    """A deleted message shows （削除済み） — its extraction must never
    leak through the 📋 block even if the artifact's hash still matches
    (the body_state gate lives in the artifact SQL itself)."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]})
    led.db.execute(
        "UPDATE messages SET body_state='deleted', body_text='' "
        "WHERE message_id=100")        # hash unchanged — worst case
    led.db.commit()
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "（削除済み）" in joined
    assert "📋 構造化" not in joined and "疼痛" not in joined


def test_card_sender_tag_shows_time_profession_org(led, tmp_path):
    """Thread card header lines carry write date+time plus the sender's
    profession/organization; missing fields leave no dangling parens."""
    _patient(led, 1)
    _msg(led, 100, 1, prof="看護師", org="訪問看護ステーションX")
    _msg(led, 101, 1, parent=100)
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render._card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "09-24 08:" in joined
    assert "職員（看護師・訪問看護ステーションX）" in joined
    assert "職員（）" not in joined and "（・" not in joined


def test_body_manifest_shows_sender_metadata(led, tmp_path):
    """The 📄本文表示 manifest header carries the same sender tag."""
    _patient(led, 1)
    _msg(led, 100, 1, prof="薬剤師", org="薬局Y")
    _msg(led, 101, 1, parent=100)
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    title, text = notify_render._card_body_text(
        led.db, card, {"shown": "[100, 101]"})
    assert "09-24 08:" in text
    assert "職員（薬剤師・薬局Y）: 本文" in text
    assert "職員: 本文" in text


def test_signal_quote_shows_sender_metadata(led, tmp_path):
    """Signal cards' 最新言及 evidence quote carries the sender tag."""
    _patient(led, 1)
    _msg(led, 100, 1, body="退院後フォローの記録", prof="薬剤師",
         org="薬局Y")
    _signal_row(led, "sig-meta", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-meta"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    c = notify_render._card_content(led.db, _card(led))
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "薬剤師・薬局Y" in joined


def test_card_signal_structured_evidence(led, tmp_path):
    """Signal/digest cards show the evidence message's structured block
    (LLM summary labelled as such via the shared formatter)."""
    _patient(led, 1)
    _msg(led, 100, 1, body="退院後フォローの記録")
    _signal_row(led, "sig-x", mids=[100])
    _extract(led, 100, {"summary": "状態安定", "points": ["経過観察"]},
             kind="extract_llm")
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-x"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    card = _card(led)
    c = notify_render._card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "📋 構造化" in joined and "要約: 状態安定" in joined
    assert "退院後フォローの記録" in joined        # raw body still there


def test_body_action_signal_full_evidence(led, tmp_path):
    long_body = "退院後フォローの経過記録。" * 30
    _patient(led, 1)
    _msg(led, 100, 1, body=long_body)
    _signal_row(led, "sig-body", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-body"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    tok = _token_for(spec, "body")
    req = _notif(tok)
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_cards.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    # signal notice + the FULL evidence body — the card shows only a
    # 120-char snippet of the quote
    assert "note sig-body" in r["body"]
    assert long_body in r["body"]
    assert "患者A" in r["body"]


def test_action_scope_and_stale_source_rejected(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "assign")
    bad = _notif(tok)
    bad["origin"] = dict(ORIGIN, channel_id="other-ch")
    bad["command_id"] = f"{tok}:{'cd' * 8}"
    r = notify_cards.apply_notification(led, bad, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "scope_mismatch"
    # mutate source -> token's need_source_gen goes stale
    _msg(led, 103, 1, parent=100)
    notify_cards.sweep(led, CFG)
    good = _notif(tok)
    good["origin"] = dict(ORIGIN, message_id="m-9")
    good["command_id"] = f"{tok}:{'ef' * 8}"
    r = notify_cards.apply_notification(led, good, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "stale_source"
    # a fresh token still works
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (_latest_render(led)["delivery_id"] + ".json")).read_text())
    r = notify_cards.apply_notification(
        led, {**_notif(_token_for(spec2, "assign")),
              "command_id": f"{_token_for(spec2, 'assign')}:{'ef' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "applied"


def test_unknown_and_expired_tokens(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    r = notify_cards.apply_notification(
        led, _notif("f" * 32), CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "unknown_token"
    tok = _token_for(spec, "assign")
    led.db.execute("UPDATE notification_action_tokens SET expires_at=? "
                   "WHERE token=?", (NOW - 1, tok))
    led.db.commit()
    r = notify_cards.apply_notification(led, _notif(tok), CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "token_expired"


# ---------- digest card + NULL project (RC13) ----------

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


# ---------- signal card + member anchor (RC13 cont.) ----------

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


# ---------- sweep / revoke (RC23) ----------

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


# ---------- card_resolve (RC25) ----------

def _resolve(led, render, attempt_id, result="mark_not_sent",
             n=30, evidence=None, scope=None):
    return notify_transport.apply_card_resolve(led, {
        "version": 1, "cmd": "ops.card_resolve", "command_id": _uuid(n),
        "actor": "op-user", "human_confirmed": True,
        "reason": "operator verified",
        "delivery_id": render["delivery_id"], "attempt_id": attempt_id,
        "result": result, **(scope or SCOPE),
        "evidence": evidence or {
            "method": "journal_check", "worker_stopped": True,
            "proof": "no_journal_started", "ref": "journal:t-1"}},
        CFG, now=NOW)


def test_card_resolve_mark_not_sent_reissues(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    r = _resolve(led, render, "0" * 15 + "1")
    assert r["outcome"] == "applied" and r["attempt_state"] == "not_sent"
    assert r["projects"] == [1]
    assert r["scope"]["channel_id"] == "ch1"
    att = led.db.execute(
        "SELECT state FROM notification_delivery_attempts"
    ).fetchone()
    assert att["state"] == "not_sent"
    # successor render issued — resolve itself never sends
    nxt = _latest_render(led)
    assert nxt["render_rev"] == 2 and nxt["state"] == "queued"


def test_card_resolve_requires_evidence_and_scope(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    # wrong channel — scope derived from the stored render, not input
    r = _resolve(led, render, "0" * 15 + "1", scope=dict(SCOPE,
                                                       channel_id="chX"))
    assert r["outcome"] == "rejected" and r["error"] == "scope_mismatch"
    # no proof the send never began -> cannot mark not_sent
    bad_ev = {"method": "guess", "worker_stopped": False,
              "proof": "no_journal_started", "ref": "x"}
    r = _resolve(led, render, "0" * 15 + "1", n=31, evidence=bad_ev)
    assert r["outcome"] == "rejected"
    # mark_delivered needs a message id
    r = _resolve(led, render, "0" * 15 + "1", n=32,
                 result="mark_delivered")
    assert r["outcome"] == "rejected" and r["error"] == "bad_message_id"
    # with evidence it lands and binds the message
    r = _resolve(led, render, "0" * 15 + "1", n=33,
                 result="mark_delivered",
                 evidence={"method": "channel_lookup",
                           "worker_stopped": True,
                           "proof": "n/a", "ref": "msg found"},
                 scope=None)
    # message_id required even when scope ok
    assert r["outcome"] == "rejected"
    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": _uuid(34), "actor": "op-user",
           "human_confirmed": True, "reason": "found in channel",
           "delivery_id": render["delivery_id"],
           "attempt_id": "0" * 15 + "1", "result": "mark_delivered",
           **SCOPE, "message_id": "m-found",
           "evidence": {"method": "channel_lookup",
                        "worker_stopped": True, "proof": "n/a",
                        "ref": "msg m-found visible"}}
    r = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["attempt_state"] == "delivered"
    assert _card(led)["message_id"] == "m-found"
    # replay is idempotent
    r2 = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    assert r2["outcome"] == "applied"


def test_card_resolve_enqueue_bypasses_project_rule(led, tmp_path):
    """ops.card_resolve carries no positive project_id — the common
    validator delegates to the dedicated one instead of rejecting the
    envelope (the operator CLI enqueues through mcs_requests.enqueue)."""
    import mcs_requests
    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": _uuid(41), "actor": "op-user",
           "human_confirmed": True, "reason": "verified in channel",
           "delivery_id": _uuid(2), "attempt_id": "x" * 16,
           "result": "mark_not_sent", **SCOPE,
           "evidence": {"method": "journal_check", "worker_stopped": True,
                        "proof": "no_journal_started", "ref": "j:t-1"}}
    assert mcs_requests.validate(req) is None
    # a project_id on the envelope is rejected — scope comes from the
    # stored render, not operator input
    assert mcs_requests.validate(dict(req, project_id=9)) \
        == "unknown_field"
    # and the command still requires the human gate
    assert mcs_requests.validate(
        dict(req, human_confirmed=False)) == "human_confirmation_required"
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    r = mcs_requests.enqueue(req, str(cmd_dir))
    assert r["outcome"] == "queued"


def test_card_resolve_human_gate_and_validation(led):
    bad = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": _uuid(1), "actor": "op",
           "delivery_id": _uuid(2), "attempt_id": "x" * 16,
           "result": "mark_not_sent", **SCOPE,
           "evidence": {"method": "m", "worker_stopped": True,
                        "proof": "no_journal_started", "ref": "r"}}
    r = notify_transport.apply_card_resolve(led, dict(bad, reason="ok"), CFG,
                                        now=NOW)
    assert r["outcome"] == "rejected" \
        and r["error"] == "human_confirmation_required"


def test_cmd_int_rejects_card_resolve(led, tmp_path):
    """The resolve path is operator-only — a plugin-origin cmd_int file
    carrying it must be refused, never applied."""
    root = tmp_path / "data"
    notify_cards.ensure_dirs(str(root))
    req = {"version": 1, "op": "card_resolve", "command_id": _uuid(1),
           "delivery_id": _uuid(2), "attempt_id": "x" * 16}
    path = root / "cmd_int" / "r.json"
    path.write_text(json.dumps(req))
    result = {"errors": []}
    notify_cmds.drain_int_commands(led, result, CFG, str(root))
    assert path.with_suffix(".json.invalid").exists() or not path.exists()
    assert led.db.execute(
        "SELECT COUNT(*) c FROM command_receipts").fetchone()["c"] == 0


# ---------- cmd_int drain + result files (RC05) ----------

def test_drain_int_two_pass_and_results(led, tmp_path):
    render = _deliverable(led, tmp_path)
    root = tmp_path / "data"
    begin_req = {"version": 1, "op": "transport_begin",
                 "command_id": _uuid(1), "attempt_id": "0" * 15 + "7",
                 "worker_id": "dd" * 8,
                 "delivery_id": render["delivery_id"],
                 "render_rev": 1,
                 "payload_hash": render["payload_hash"],
                 "route_epoch": 1, **SCOPE}
    rcpt_req = {"version": 1, "op": "transport_receipt",
                "command_id": _uuid(2), "attempt_id": "0" * 15 + "7",
                "delivery_id": render["delivery_id"],
                "render_rev": 1,
                "payload_hash": render["payload_hash"],
                "route_epoch": 1,
                "correlation": render["correlation"], **SCOPE,
                "result": "delivered", "message_id": "m-7"}
    int_dir = root / "cmd_int"
    for name, req in (("b.json", rcpt_req), ("a.json", begin_req)):
        (int_dir / name).write_text(json.dumps(req))
    result = {"errors": []}
    n = notify_cmds.drain_int_commands(led, result, CFG, str(root))
    assert n == 2
    # begins apply before receipts within one drain: the attempt row
    # exists when its receipt lands, so the begin grants AND the
    # receipt settles it delivered in the same pass — no orphaned
    # grant from an unknown_attempt reject.
    res_dir = root / "cmd_results"
    results = sorted(p.name for p in res_dir.iterdir())
    assert results == [_uuid(1) + ".json", _uuid(2) + ".json"]
    assert not list(int_dir.iterdir())
    row = led.db.execute(
        "SELECT state,message_id FROM notification_delivery_attempts "
        "WHERE attempt_id=?", ("0" * 15 + "7",)).fetchone()
    assert row["state"] == "delivered" and row["message_id"] == "m-7"
    assert _card(led)["delivery_state"] == "delivered"


def test_drain_int_receipts_first_settles_then_begin(led, tmp_path):
    """Real dependency order: a begin lands, gets granted; the NEXT
    drain sees the receipt first (pass 1) and settles before the
    queued interaction is applied (pass 2)."""
    render = _deliverable(led, tmp_path)
    root = tmp_path / "data"
    begin_req = {"version": 1, "op": "transport_begin",
                 "command_id": _uuid(1), "attempt_id": "0" * 15 + "7",
                 "worker_id": "dd" * 8,
                 "delivery_id": render["delivery_id"],
                 "render_rev": 1,
                 "payload_hash": render["payload_hash"],
                 "route_epoch": 1, **SCOPE}
    (root / "cmd_int" / "a.json").write_text(json.dumps(begin_req))
    notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    # now the receipt + an interaction arrive together
    rcpt_req = {"version": 1, "op": "transport_receipt",
                "command_id": _uuid(2), "attempt_id": "0" * 15 + "7",
                "delivery_id": render["delivery_id"],
                "render_rev": 1,
                "payload_hash": render["payload_hash"],
                "route_epoch": 1,
                "correlation": render["correlation"], **SCOPE,
                "result": "delivered", "message_id": "m-7"}
    spec = json.loads(
        (root / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    tok = _token_for(spec, "ack")
    notif = {"version": 1, "op": "notification",
             "command_id": f"{tok}:{'ee' * 8}", "actor": "nurse-1",
             "token": tok,
             "origin": dict(ORIGIN, message_id="m-7")}
    (root / "cmd_int" / "z.json").write_text(json.dumps(notif))
    (root / "cmd_int" / "b.json").write_text(json.dumps(rcpt_req))
    n = notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    assert n == 2
    assert _card(led)["delivery_state"] == "delivered"
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_acknowledgements"
    ).fetchone()["c"] == 1


def test_drain_int_backlog_never_orphans_a_begin_past_the_window(
        led, tmp_path):
    """With a backlog larger than one drain, a receipt that sorts into
    the window while its begin sorts past it must not be consumed as
    unknown_attempt — begins are picked from the wider scanned window."""
    render = _deliverable(led, tmp_path)
    root = tmp_path / "data"
    begin_req = {"version": 1, "op": "transport_begin",
                 "command_id": _uuid(1), "attempt_id": "0" * 15 + "7",
                 "worker_id": "dd" * 8,
                 "delivery_id": render["delivery_id"],
                 "render_rev": 1,
                 "payload_hash": render["payload_hash"],
                 "route_epoch": 1, **SCOPE}
    rcpt_req = {"version": 1, "op": "transport_receipt",
                "command_id": _uuid(2), "attempt_id": "0" * 15 + "7",
                "delivery_id": render["delivery_id"],
                "render_rev": 1,
                "payload_hash": render["payload_hash"],
                "route_epoch": 1,
                "correlation": render["correlation"], **SCOPE,
                "result": "delivered", "message_id": "m-7"}
    int_dir = root / "cmd_int"
    (int_dir / "a0.json").write_text(json.dumps(rcpt_req))
    for i in range(1, 6):
        (int_dir / f"a{i}.json").write_text(json.dumps(
            {"version": 1, "op": "bogus", "command_id": _uuid(10 + i)}))
    (int_dir / "z.json").write_text(json.dumps(begin_req))
    notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root),
                                   limit=2)
    row = led.db.execute(
        "SELECT state FROM notification_delivery_attempts "
        "WHERE attempt_id=?", ("0" * 15 + "7",)).fetchone()
    assert row is not None and row["state"] == "delivered"

def test_drain_int_validation_quarantine(led, tmp_path):
    root = tmp_path / "data"
    notify_cards.ensure_dirs(str(root))
    bad = root / "cmd_int" / "bad.json"
    bad.write_text(json.dumps({"version": 1, "op": "bogus"}))
    notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    assert bad.with_suffix(".json.invalid").exists()


# ---------- snapshot dirty flag (RC21) ----------

def test_snapshot_dirty_flag_lifecycle(led):
    _seed_thread(led)
    assert not notify_cards.snapshot_dirty(led)
    _dispatch(led, _intent(led))
    assert notify_cards.snapshot_dirty(led)
    notify_cards.clear_snapshot_dirty(led)
    assert not notify_cards.snapshot_dirty(led)


# ---------- notification_receipt reader (RC27) ----------

def test_notification_receipt_reader(led, tmp_path):
    import ledger as _ledger2
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "ack")
    cid = f"{tok}:{'ab' * 8}"
    notify_cards.apply_notification(
        led, {**_notif(tok), "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    assert _ledger2.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                     str(snap_dir))
    import mcs_view
    snap = snap_dir / "ledger-snapshot.db"
    view = mcs_view.View(str(snap))
    try:
        r = view.notification_receipt(cid)
        assert r["outcome"] == "applied" and r["action"] == "ack"
        # wrong hash -> conflict, never a fake result
        r = view.notification_receipt(cid, payload_hash="0" * 64)
        assert r["error"] == "command_id_conflict"
        # unknown id -> not processed (snapshot lag is not failure)
        r = view.notification_receipt(_uuid(77))
        assert r["outcome"] == "not_processed_or_not_in_snapshot"
        # a different actor sees nothing
        r = view.notification_receipt(cid, context={
            "actor": "someone-else", "application_id": "app1",
            "channel_id": "ch1", "projects": [1]})
        assert r["error"] == "actor_mismatch"
        # correct actor + scope passes
        r = view.notification_receipt(cid, context={
            **SCOPE, "actor": "nurse-1", "projects": [1]})
        assert r["outcome"] == "applied"
        # profile/guild are part of the delivery scope too — a caller
        # from another deployment profile sees nothing
        r = view.notification_receipt(cid, context={
            "actor": "nurse-1", "application_id": "app1",
            "channel_id": "ch1", "profile": "other",
            "projects": [1]})
        assert r["error"] == "scope_mismatch"
        r = view.notification_receipt(cid, context={
            "actor": "nurse-1", "application_id": "app1",
            "channel_id": "ch1", "profile": "mcs", "guild_id": "g9",
            "projects": [1]})
        assert r["error"] == "scope_mismatch"
        # a receipt whose projects exceed the caller's set is refused
        r = view.notification_receipt(cid, context={
            **SCOPE, "actor": "nurse-1", "projects": []})
        assert r["error"] == "project_scope_mismatch"
    finally:
        view.close()


def test_card_resolve_receipt_is_operator_only(led, tmp_path):
    import ledger as _ledger2
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    r = _resolve(led, render, "0" * 15 + "1")
    cid = _uuid(30)
    assert r["outcome"] == "applied"
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    assert _ledger2.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                     str(snap_dir))
    import mcs_view
    snap = snap_dir / "ledger-snapshot.db"
    view = mcs_view.View(str(snap))
    try:
        # operator context sees the resolve receipt
        r = view.notification_receipt(cid, context={
            **SCOPE, "actor": "op-user", "operator": True, "projects": [1]})
        assert r["outcome"] == "applied"
        # a non-operator plugin context is refused
        r = view.notification_receipt(cid, context={
            "actor": "op-user", "operator": False, "projects": [1]})
        assert r["error"] == "operator_only"
    finally:
        view.close()


# ---------- thread_receipt ----------

def test_thread_receipt_binds_once(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "thread_id": "th-1"}, CFG, now=NOW)
    assert r["applied"] and r["thread_state"] == "created"
    # a later thread_id can never rebind
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(6),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "thread_id": "th-2"}, CFG, now=NOW)
    assert r["thread_id"] == "th-1"
    # wrong message -> rejected, no state change
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(7),
        "delivery_id": render["delivery_id"], "message_id": "m-x",
        "thread_id": "th-3"}, CFG, now=NOW)
    assert not r["applied"]


# ---------- refresh (self-heal) ----------

def test_refresh_reissues_from_origin(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rev_before = _latest_render(led)["render_rev"]
    req = {"version": 1, "op": "refresh", "command_id": _uuid(40),
           "actor": "nurse-1",
           "origin": dict(ORIGIN, message_id="m-9")}
    r = notify_cards.apply_refresh(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied"
    assert _latest_render(led)["render_rev"] == rev_before + 1
    # unknown origin -> rejected
    r = notify_cards.apply_refresh(led, {
        "version": 1, "op": "refresh", "command_id": _uuid(41),
        "actor": "nurse-1", "origin": dict(ORIGIN, message_id="nope")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "card_not_found"


# ---------- adversarial fixes (post-D1 review) ----------

def _deliver_update(led, tmp_path, message_id="m-9"):
    """Deliver the create, drift the source, and return the UPDATE
    render row (still queued)."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id=message_id)
    _settle_bodies(led, render)
    _msg(led, 103, 1, parent=100)          # drift -> update render
    notify_cards.sweep(led, CFG)
    nxt = _latest_render(led)
    assert nxt["op"] == "update" and nxt["render_rev"] == 2
    return nxt


def test_page_nav_issues_new_render_and_tokens(led, tmp_path):
    """prev/next must produce a new revision/token/render — the plan's
    view-op contract (a nav that only bumps ui_revision leaves the
    card frozen and every other button dead on stale_ui)."""
    card, spec = _delivered_card(led, tmp_path)
    # single-page card mints no nav buttons — grow it to two pages
    assert all(b["id"] not in ("prev", "next")
               for row in spec["parts"]["action_rows"] for b in row)
    for m in range(110, 120):
        _msg(led, m, 1, parent=100)
    notify_cards.sweep(led, CFG)
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (_latest_render(led)["delivery_id"] + ".json")).read_text())
    assert spec2["parts"]["pages"] > 1
    # a card opens on its newest page — only 'prev' is actionable there
    tok = _token_for(spec2, "prev")
    r = notify_cards.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{'ab' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")}, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "page"
    # a successor UPDATE render carrying the new page + fresh tokens
    nxt = _latest_render(led)
    assert nxt["render_rev"] == spec2["render_rev"] + 1
    assert nxt["state"] == "queued" and nxt["op"] == "update"
    spec3 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (nxt["delivery_id"] + ".json")).read_text())
    assert spec3["parts"]["page"] == spec2["parts"]["page"] - 1
    assert spec3["ui_revision"] == spec2["ui_revision"] + 1
    # fresh tokens are minted — the consumed ones never recur
    nav3 = [b["token"] for row in spec3["parts"]["action_rows"]
            for b in row if b["id"] in ("prev", "next")]
    assert nav3 and tok not in nav3


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
    r = notify_cards.apply_refresh(led, {
        "version": 1, "op": "refresh", "command_id": _uuid(42),
        "actor": "nurse-1",
        "origin": dict(ORIGIN, message_id="m-9")}, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "card_revoked"
    # no fresh revoke render minted by the refresh itself
    assert _latest_render(led)["op"] != "revoke" or \
        _latest_render(led)["render_rev"] == 1


def test_request_and_dismiss_tokens_authorize_modal(led, tmp_path):
    """request/dismiss tokens authorize opening the modal — applied,
    modal flag, stored params echoed — while the actual human command
    arrives separately."""
    _patient(led, 1)
    _msg(led, 100, 1)
    _signal_row(led, "sig-modal", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-modal"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    render = _latest_render(led)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    # the render pins modal context — the plugin confirms against what
    # was shown, never a snapshot that may lag the render
    ctx = spec["parts"]["context"]
    assert ctx["project_id"] == 1
    assert ctx["source_message_id"] == 100
    assert ctx["source_hash"] == f"{100:064x}"
    assert ctx["signals"]["sig-modal"]["artifact_id"] > 0
    for i, action in enumerate(("request", "dismiss")):
        tok = _token_for(spec, action)
        r = notify_cards.apply_notification(
            led, {**_notif(tok, n=70 + i),
                  "command_id": f"{tok}:{(70 + i):016x}"},
            CFG, now=NOW)
        assert r["outcome"] == "applied", (action, r)
        assert r["action"] == action and r["modal"] is True
        assert isinstance(r["params"], dict)
    # nothing was mutated — no request row, signal still open
    assert led.db.execute(
        "SELECT COUNT(*) c FROM requests").fetchone()["c"] == 0


def test_signal_without_source_suppresses_request(led, tmp_path):
    """A signal whose evidence cannot pin a source message gets no
    request button — a permanently-failing button is worse than none."""
    _patient(led, 1)
    _signal_row(led, "sig-nosrc")          # evidence.message_ids empty
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-nosrc"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    render = _latest_render(led)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    actions = {b["id"] for row in spec["parts"]["action_rows"]
               for b in row}
    assert "request" not in actions
    assert "dismiss" in actions           # signal artifact pinnable


def test_gc_deletes_expired_tokens_and_old_specs(led, tmp_path):
    render = _deliverable(led, tmp_path)
    spec_path = (tmp_path / "data" / "discord_render"
                 / (render["delivery_id"] + ".json"))
    assert spec_path.exists()
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    # the card settled but its durable parts are pending — the spec
    # file is the only copy a restart-resume can replay, so gc keeps
    # it until the part plan reaches a terminal mix
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["spec_files"] == 0 and spec_path.exists()
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered' "
        "WHERE delivery_id=?", (render["delivery_id"],))
    notify_cards._update_parts_state(
        led.db, render["delivery_id"], NOW)
    led.db.commit()
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["spec_files"] >= 1 and not spec_path.exists()
    led.db.execute("UPDATE notification_action_tokens SET expires_at=?",
                   (NOW - 1,))
    led.db.commit()
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["tokens"] >= 1


def test_origin_must_match_bound_message(led, tmp_path):
    """A delivered card's buttons live on its bound message — a token
    replayed with a different message origin is refused even when the
    deployment scope matches (token alone resolves the card)."""
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "assign")
    r = notify_cards.apply_notification(
        led, {**_notif(tok),
              "origin": dict(ORIGIN, message_id="other-msg")},
        CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "origin_mismatch"
    # the bound message's own interaction still applies
    r = notify_cards.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{'cd' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "applied"


def test_command_id_must_derive_from_token(led, tmp_path):
    """The notification idempotency key is <token>:<actor_hash> — a
    prefix that is not the submitted token can never dedupe correctly."""
    req = {"version": 1, "op": "notification",
           "command_id": f"{'a' * 32}:{'b' * 16}", "actor": "nurse-1",
           "token": "c" * 32, "origin": ORIGIN}
    assert notify_cmds.validate_int(req) == "command_id_mismatch"
    req["command_id"] = f"{'c' * 32}:{'b' * 16}"
    assert notify_cmds.validate_int(req) is None


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


def test_failed_thread_keeps_body_button(led, tmp_path):
    """A card whose companion thread could not be created keeps the
    📄 button on the next render — the body has nowhere else to live."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "error_code": "http_403"}, CFG, now=NOW)
    assert r["thread_state"] == "failed"
    _msg(led, 103, 1, parent=100)          # drift -> update render
    notify_cards.sweep(led, CFG)
    upd = _latest_render(led)
    assert upd["op"] == "update"
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (upd["delivery_id"] + ".json")).read_text())
    ids = {b["id"] for row in spec["parts"]["action_rows"]
           for b in row}
    assert "body" in ids
    assert "thread_body" not in spec["parts"]


def test_created_thread_update_carries_body_not_button(led, tmp_path):
    """A card whose thread exists renders the body as thread_body —
    the update delivers it into the thread instead of minting 📄."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "thread_id": "th-1"}, CFG, now=NOW)
    assert r["thread_state"] == "created"
    _msg(led, 103, 1, parent=100)
    notify_cards.sweep(led, CFG)
    upd = _latest_render(led)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (upd["delivery_id"] + ".json")).read_text())
    assert spec["delivery"]["thread_id"] == "th-1"
    assert spec["parts"]["thread_body_parts"]   # durable part text (T7)
    assert [p for p in spec["parts"]["manifest"]
            if p["kind"] == "body_part"]
    ids = {b["id"] for row in spec["parts"]["action_rows"]
           for b in row}
    assert "body" not in ids


def test_gc_removes_old_cmd_results(led, tmp_path):
    res_dir = tmp_path / "data" / "cmd_results"
    res_dir.mkdir(parents=True)
    old = res_dir / "old.json"
    old.write_text("{}")
    fresh = res_dir / "fresh.json"
    fresh.write_text("{}")
    stale = notify_cards.TOKEN_WRITE_S + 100
    os.utime(old, (NOW - stale, NOW - stale))
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["result_files"] == 1
    assert not old.exists() and fresh.exists()


@pytest.mark.parametrize("raw", ["{not json", "null", "[]", "true", "7",
                                 "[" * 1100 + "]" * 1100],
                         ids=["syntax", "null", "array", "boolean", "number", "nested"])
def test_drain_quarantines_corrupt_command(led, tmp_path, raw):
    """Publication is atomic — a readable .json that fails to parse is
    permanently corrupt and must not be re-read every drain."""
    int_dir = tmp_path / "data" / "cmd_int"
    int_dir.mkdir(parents=True)
    bad = int_dir / "bad.json"
    bad.write_text(raw)
    res = {"errors": []}
    notify_cmds.drain_int_commands(led, res, CFG, str(tmp_path / "data"))
    assert not bad.exists()
    assert (int_dir / "bad.json.invalid").exists()
    # a second drain does not touch the quarantined file again
    res2 = {"errors": []}
    assert notify_cmds.drain_int_commands(
        led, res2, CFG, str(tmp_path / "data")) == 0


def test_notification_command_id_rejects_trailing_newline():
    token = "a" * 32
    req = {"version": 1, "op": "notification", "actor": "synthetic",
           "command_id": token + ":" + "b" * 16, "token": token,
           "origin": {"application_id": "app", "channel_id": "channel", "message_id": "message"}}
    assert notify_cmds.validate_int(req) is None
    req["command_id"] += "\n"
    assert notify_cmds.validate_int(req) == "bad_command_id"


# ---------- D4: sweep change detection without intents (RC19) ----------

def _deliver_card(led, cfg=CFG):
    """dispatch + begin + delivered receipt -> a card bound to m-1."""
    _dispatch(led, _intent(led), cfg)
    r = _latest_render(led)
    _begin(led, r)
    _receipt(led, r, f"{1:016x}", message_id="m-1")
    _settle_bodies(led, r)
    return r


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
    assert "（削除済み）" in json.dumps(r["spec_json"],
                                     ensure_ascii=False)


def test_sweep_detects_signal_lifecycle(led):
    """RC19 — resolve / dismiss / supersede transitions on a rendered
    signal re-render its card without a new intent."""
    _patient(led)
    _signal_row(led, "sig-1")
    _dispatch(led, _intent(led, kind="signal",
                           payload={"signal_keys": ["sig-1"],
                                    "project_id": 1}))
    r0 = _latest_render(led)
    assert _card(led)["kind"] == "signal"
    _begin(led, r0)
    _receipt(led, r0, f"{1:016x}", message_id="m-1")
    _settle_bodies(led, r0)

    # resolved — the evaluator's terminal transition
    _signal_row(led, "sig-1", state="resolved")
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
    _signal_row(led, "sig-1", state="dismissed")
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 3)
    r = _latest_render(led)
    assert r["render_rev"] == r0["render_rev"] + 3


def test_signals_notify_off_cancels_queued_signal_render(led):
    """signals.notify=false: a queued signal render is cancelled and
    unbound instead of sitting live forever (begin denies it with a
    non-final signal_notify_off), no new render is issued while off, and
    re-enabling issues a fresh render with a new delivery_id."""
    _patient(led)
    _signal_row(led, "sig-off")
    _dispatch(led, _intent(led, kind="signal",
                           payload={"signal_keys": ["sig-off"],
                                    "project_id": 1}))
    r0 = _latest_render(led)
    assert r0["state"] == "queued"
    off = {"notify": CFG["notify"], "signals": {"notify": False}}

    notify_cards.sweep(led, off, now=NOW + 1)
    r = _latest_render(led)
    assert r["delivery_id"] == r0["delivery_id"]
    assert r["state"] == "cancelled"
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_intent_cards "
        "WHERE delivery_id IS NOT NULL").fetchone()["c"] == 0

    # drift while off still issues nothing
    _signal_row(led, "sig-off", state="resolved")
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


# ---------- D4: ops.card_resolve idempotency (RC25) ----------

def test_card_resolve_reapply_is_idempotent(led):
    """RC25 — re-running the same resolve command replays the stored
    receipt; a second resolve with a different payload conflicts."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r0 = _latest_render(led)
    _begin(led, r0)                      # granted, unsettled
    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": _uuid(77), "actor": "op-user",
           "human_confirmed": True, "reason": "audit",
           "delivery_id": r0["delivery_id"], "attempt_id": f"{1:016x}",
           "result": "mark_delivered", "message_id": "m-9", **SCOPE,
           "evidence": {"method": "journal_review", "ref": "w1.jsonl"}}
    first = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    assert first["outcome"] == "applied"
    again = notify_transport.apply_card_resolve(led, dict(req), CFG,
                                            now=NOW + 1)
    assert again == first                     # stored receipt replay
    conflict = notify_transport.apply_card_resolve(
        led, {**req, "result": "mark_not_sent",
              "evidence": {"method": "journal_review", "ref": "w1.jsonl",
                           "worker_stopped": True,
                           "proof": "no_journal_started"}},
        CFG, now=NOW + 2)
    assert conflict["outcome"] == "rejected"
    assert conflict["error"] == "command_id_conflict"


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
    out = notify_cards.apply_refresh(led, {
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
        notify_cards._issue_render(led.db, card['card_id'], CFG, NOW, specs)
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


@pytest.mark.parametrize('bad_op', [[], {}])
def test_drain_quarantines_non_string_op_and_continues(led, tmp_path, bad_op):
    root = tmp_path / 'data'
    notify_cards.ensure_dirs(str(root))
    bad = root / 'cmd_int' / '00-invalid.json'
    bad.write_text(json.dumps({'version': 1, 'op': bad_op, 'command_id': _uuid(990)}))
    result = {}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 1
    assert not bad.exists()
    assert (root / 'cmd_int' / '00-invalid.json.invalid').exists()
    receipt = json.loads((root / 'cmd_results' / (_uuid(990) + '.json')).read_text())
    assert receipt['outcome'] == 'rejected'
    assert receipt['error'] == 'unknown_op'


def test_resolve_delivered_conflict_rejects_another_message(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, f'{1:016x}', message_id='original-message')
    req = {'version': 1, 'cmd': 'ops.card_resolve', 'command_id': _uuid(991),
           'actor': 'op-user', 'human_confirmed': True, 'reason': 'checked channel',
           'delivery_id': render['delivery_id'], 'attempt_id': f'{1:016x}',
           'result': 'mark_delivered', **SCOPE, 'message_id': 'another-message',
           'evidence': {'method': 'channel_lookup', 'ref': 'synthetic-proof'}}
    result = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    assert result['outcome'] == 'rejected'
    assert result['error'] == 'attempt_conflict'
    assert _card(led)['message_id'] == 'original-message'


# ---------- card_resolve vs the delivery journal ----------

def _journal(tmp_path, *rows, raw=b""):
    state = tmp_path / "data" / "discord_state"
    state.mkdir(parents=True, exist_ok=True)
    body = b"".join(json.dumps(r).encode() + b"\n" for r in rows) + raw
    (state / "journal-w1.jsonl").write_bytes(body)


def _rejected_no_reissue(led, r, error):
    assert r["outcome"] == "rejected" and r["error"] == error
    stored = json.loads(led.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE command_id=?",
        (r["command_id"],)).fetchone()["receipt_json"])
    assert stored["error"] == error
    assert led.db.execute(
        "SELECT state FROM notification_delivery_attempts"
    ).fetchone()["state"] == "granted"
    assert _latest_render(led)["render_rev"] == 1   # nothing reissued


def test_card_resolve_not_sent_rejected_when_journal_started(led, tmp_path):
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    _journal(tmp_path, {"attempt_id": aid, "phase": "started",
                        "delivery_id": render["delivery_id"]})
    _rejected_no_reissue(led, _resolve(led, render, aid),
                         "journal_contradicts_proof")


def test_card_resolve_not_sent_rejected_when_journal_delivered(
        led, tmp_path):
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    _journal(tmp_path,
             {"attempt_id": aid, "phase": "started",
              "delivery_id": render["delivery_id"]},
             {"attempt_id": aid, "phase": "result", "result": "delivered",
              "message_id": "m-7", "delivery_id": render["delivery_id"]})
    # even a remote_absent attestation cannot outrank a journaled send
    r = _resolve(led, render, aid, evidence={
        "method": "channel_lookup", "worker_stopped": True,
        "proof": "remote_absent", "ref": "channel:ch1"})
    _rejected_no_reissue(led, r, "journal_contradicts_proof")


def test_card_resolve_not_sent_rejected_when_journal_tainted(
        led, tmp_path):
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    # a corrupt middle line could be this attempt's 'started' row
    _journal(tmp_path, {"attempt_id": "other", "phase": "claimed"},
             raw=b"{corrupt\n")
    _rejected_no_reissue(led, _resolve(led, render, aid),
                         "journal_contradicts_proof")


def test_card_resolve_delivered_must_match_journal_message(led, tmp_path):
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    _journal(tmp_path,
             {"attempt_id": aid, "phase": "result", "result": "delivered",
              "message_id": "m-7", "delivery_id": render["delivery_id"]})
    req = {"version": 1, "cmd": "ops.card_resolve", "command_id": _uuid(60),
           "actor": "op-user", "human_confirmed": True,
           "reason": "checked channel",
           "delivery_id": render["delivery_id"], "attempt_id": aid,
           "result": "mark_delivered", **SCOPE, "message_id": "m-other",
           "evidence": {"method": "channel_lookup", "ref": "synthetic"}}
    r = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    _rejected_no_reissue(led, r, "message_id_mismatch")
    r = notify_transport.apply_card_resolve(
        led, dict(req, command_id=_uuid(61), message_id="m-7"), CFG,
        now=NOW)
    assert r["outcome"] == "applied"
    assert _card(led)["message_id"] == "m-7"



def test_card_resolve_delivered_accepts_idless_journal_result(led, tmp_path):
    """A delivered journal row without a message id cannot contradict
    the operator's id — but it still refutes mark_not_sent."""
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    _journal(tmp_path,
             {"attempt_id": aid, "phase": "started",
              "delivery_id": render["delivery_id"]},
             {"attempt_id": aid, "phase": "result", "result": "delivered",
              "delivery_id": render["delivery_id"]})
    r = _resolve(led, render, aid, evidence={
        "method": "channel_lookup", "worker_stopped": True,
        "proof": "remote_absent", "ref": "channel:ch1"})
    _rejected_no_reissue(led, r, "journal_contradicts_proof")
    req = {"version": 1, "cmd": "ops.card_resolve", "command_id": _uuid(63),
           "actor": "op-user", "human_confirmed": True,
           "reason": "checked channel",
           "delivery_id": render["delivery_id"], "attempt_id": aid,
           "result": "mark_delivered", **SCOPE, "message_id": "m-7",
           "evidence": {"method": "channel_lookup", "ref": "synthetic"}}
    r = notify_transport.apply_card_resolve(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied"
    assert _card(led)["message_id"] == "m-7"

def test_begin_denies_source_changed_and_reissues_at_once(led, tmp_path):
    """A source edit between issue and begin never posts the stale
    bytes, and the fresh render is published by the denial itself — no
    wait for the rotating sweep to reach the card."""
    _patient(led)
    _msg(led, 100)
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    led.db.execute("UPDATE messages SET content_hash=? "
                   "WHERE message_id=100", ("b" * 64,))
    led.db.commit()
    r = _begin(led, render)
    assert r["granted"] is False and r["error"] == "denied_source_changed"
    render_dir = tmp_path / "data" / "discord_render"
    assert led.db.execute(
        "SELECT state FROM notification_renders WHERE delivery_id=?",
        (render["delivery_id"],)).fetchone()["state"] == "cancelled"
    assert not (render_dir / (render["delivery_id"] + ".json")).exists()
    nxt = _latest_render(led)
    assert nxt["render_rev"] == 2 and nxt["state"] == "queued"
    assert (render_dir / (nxt["delivery_id"] + ".json")).exists()
    # a replayed begin is idempotent — no second reissue
    assert _begin(led, render)["error"] == "denied_source_changed"
    assert _latest_render(led)["render_rev"] == 2
    assert _begin(led, nxt, n=2)["granted"]


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


def test_body_replay_uses_live_source_and_revocation(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path, cfg=NO_THREAD_CFG)
    req = _notif(_token_for(spec, 'body'))
    req['origin'] = dict(ORIGIN, message_id='m-9')
    first = notify_cards.apply_notification(led, req, CFG, now=NOW)
    assert '本文' in first['body']
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE project_id=1")
    led.db.commit()
    second = notify_cards.apply_notification(led, req, CFG, now=NOW + 1)
    assert '（削除済み）' in second['body']
    assert ': 本文' not in second['body']
    notify_cards.revoke_card(led.db, card['card_id'], NOW + 2)
    led.db.commit()
    third = notify_cards.apply_notification(led, req, CFG, now=NOW + 3)
    assert third['outcome'] == 'rejected'
    assert 'body' not in third


def test_multipage_intent_plans_every_covered_body(led):
    """U03-F03: the card face opens on its last page, but the durable
    thread plan must carry the body of every message the intent covers —
    earlier pages included — in thread order."""
    _patient(led, 1)
    _msg(led, 100, 1, body="BODY-100")
    for m in range(101, 114):
        _msg(led, m, 1, parent=100, body=f"BODY-{m}")
    _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 114))}))
    spec = json.loads(_latest_render(led)["spec_json"])
    assert spec["parts"]["pages"] > 1
    assert spec["parts"]["page"] == spec["parts"]["pages"] - 1
    body = "".join(spec["parts"]["thread_body_parts"])
    positions = [body.index(f"BODY-{m}") for m in range(100, 114)]
    assert positions == sorted(positions)
    body_parts = [p for p in spec["parts"]["manifest"]
                  if p["kind"] == "body_part"]
    assert len(body_parts) == len(spec["parts"]["thread_body_parts"])


def test_body_text_not_retained_in_receipts_or_snapshot(led, tmp_path):
    """U03-F02: the 📄 result carries the body for its one delivery, but
    the durable command receipt (and so the published snapshot) must
    not keep a copy that outlives the source's deletion."""
    import ledger as _ledger2
    import mcs_view
    marker = "合成本文F02"
    _, spec = _delivered_card(led, tmp_path, cfg=NO_THREAD_CFG)
    did = _latest_render(led)["delivery_id"]
    frozen = _spec_json(led, did)
    assert frozen is not None and "本文" in frozen
    led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100",
                   (marker,))
    led.db.commit()
    req = {**_notif(_token_for(spec, "body")),
           "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_cards.apply_notification(led, req, CFG, now=NOW)
    assert first["outcome"] == "applied" and marker in first["body"]
    stored = led.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE command_id=?",
        (req["command_id"],)).fetchone()["receipt_json"]
    assert marker not in stored and json.loads(stored)["action"] == "body"
    led.db.execute("UPDATE messages SET body_state='deleted' "
                   "WHERE message_id=100")
    led.db.commit()
    # the settled delivered render's frozen copy goes with the next gc
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_json_cleared"] == 1
    assert _spec_json(led, did) is None
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    assert _ledger2.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                     str(snap_dir))
    view = mcs_view.View(str(snap_dir / "ledger-snapshot.db"))
    try:
        r = view.notification_receipt(req["command_id"])
        assert r["outcome"] == "applied" and "body" not in r
    finally:
        view.close()
    snap = sqlite3.connect(
        f"file:{snap_dir / 'ledger-snapshot.db'}?mode=ro", uri=True)
    try:
        assert snap.execute(
            "SELECT COUNT(*) FROM notification_renders WHERE "
            "spec_json IS NOT NULL").fetchone()[0] == 0
    finally:
        snap.close()
    # a re-click still answers from the live (now deleted) source
    again = notify_cards.apply_notification(led, req, CFG, now=NOW + 1)
    assert marker not in again["body"]


def test_notification_request_ids_get_fresh_results_without_duplicate_writes(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    req = _notif(_token_for(spec, 'ack'))
    req['origin'] = dict(ORIGIN, message_id='m-9')
    root = tmp_path / 'data'
    for number in (910, 911):
        req['request_id'] = _uuid(number)
        path = root / 'cmd_int' / f'{number}.json'
        path.write_text(json.dumps(req))
        notify_cmds.drain_int_commands(led, {}, CFG, str(root))
        result_path = root / 'cmd_results' / f'{req["request_id"]}.json'
        assert result_path.exists()
        out = json.loads(result_path.read_text())
        assert out['outcome'] == 'applied'
        assert out['request_id'] == req['request_id']
    assert led.db.execute('SELECT COUNT(*) FROM notification_acknowledgements').fetchone()[0] == 1


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
            notify_cards._complete_intent(led.db, 1, NOW + 1)


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
        notify_cards._update_parts_state(led.db, r["delivery_id"], NOW)
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


def _spec_json(led, delivery_id):
    return led.db.execute(
        "SELECT spec_json FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone()[0]


def _settle_parts(led, render):
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered' "
        "WHERE delivery_id=?", (render["delivery_id"],))
    notify_cards._update_parts_state(led.db, render["delivery_id"], NOW)
    led.db.commit()


def _delivered_create(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    return render


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


# ---------- thread task list + transitions ----------

def _request(led, pid=1, src_mid=100, title="依頼X", status="open",
             assignee=None, due=None, rev=1):
    rid = led.db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,assignee,due_date,status,revision,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, src_mid, "h" * 64, title, assignee, due, status,
         rev, NOW, NOW)).lastrowid
    led.db.commit()
    return rid


def _tasks_token(led, spec):
    """A 📋/☑ token as a card rendered before the task existed carries
    it (the button is only emitted while open tasks exist)."""
    ack = led.db.execute(
        "SELECT * FROM notification_action_tokens WHERE token=?",
        (_token_for(spec, "ack"),)).fetchone()
    tok = notify_cards._mint_token(
        led.db, ack["card_id"], "tasks", None,
        {"source_gen": ack["need_source_gen"],
         "manifest_id": ack["need_manifest_id"],
         "ui_rev": ack["need_ui_rev"]}, NOW)
    led.db.commit()
    return tok


def _tasks_click(led, spec, msg_id="m-9", suffix="ab"):
    """Each click needs a fresh command_id — a repeated one replays the
    stored receipt by design."""
    tok = _tasks_token(led, spec)
    return notify_cards.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{suffix * 8}",
              "origin": dict(ORIGIN, message_id=msg_id)},
        CFG, now=NOW)


def test_tasks_button_only_on_thread_cards(led, tmp_path):
    """☑ pins a task list to a thread — only thread cards with open
    tasks carry it."""
    card, spec = _delivered_card(led, tmp_path)
    ids = {b["id"] for row in spec["parts"]["action_rows"] for b in row}
    assert "tasks" not in ids                 # no open task yet
    _request(led, src_mid=101, title="返信タスク")
    notify_cards.sweep(led, CFG, now=NOW)
    render = _latest_render(led)
    spec1 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    button = next(b for row in spec1["parts"]["action_rows"] for b in row
                  if b["id"] == "tasks")
    assert button["label"] == "☑ タスク完了"
    _patient(led, 2)
    _msg(led, 200, 2)
    _signal_row(led, "sig-nt", pid=2, mids=[200])
    ev = _intent(led, kind="signal", pid=2,
                 payload={"signal_keys": ["sig-nt"], "project_id": 2,
                          "type": "med_followup"})
    _dispatch(led, ev)
    render = _latest_render(led, 2)
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    ids = {b["id"] for row in spec2["parts"]["action_rows"] for b in row}
    assert "tasks" not in ids


def test_tasks_view_lists_thread_requests(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)   # root 100 + reply 101
    rid1 = _request(led, src_mid=100, title="経過確認")
    rid2 = _request(led, src_mid=101, title="返信タスク",
                    status="in_progress", assignee="山田",
                    due="2026-10-01")
    _request(led, src_mid=100, title="取り下げ済", status="cancelled")
    _msg(led, 300, 1)
    _request(led, src_mid=300, title="別スレッド")
    _patient(led, 2)
    _msg(led, 400, 2)
    _request(led, pid=2, src_mid=400, title="別患者")
    r = _tasks_click(led, spec)
    assert r["outcome"] == "applied" and r["action"] == "tasks"
    ids = [t["request_id"] for t in r["tasks"]]
    assert ids == [rid1, rid2]          # open first; cancelled/other gone
    t1, t2 = r["tasks"]
    assert set(t1["transitions"]) == {"in_progress", "done"}
    assert set(t2["transitions"]) == {"done"}
    assert t2["assignee"] == "山田" and t2["due_date"] == "2026-10-01"
    for t in r["tasks"]:
        for tr in t["transitions"].values():
            ctx = r["token_ctx"].get(tr["token"])
            assert ctx and ctx["action"] == "task_status" \
                and ctx["project_id"] == 1


def test_task_status_transition_and_stale_reject(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rid = _request(led, src_mid=100, title="T")
    r = _tasks_click(led, spec)
    tr = r["tasks"][0]["transitions"]["in_progress"]
    # the click arrives on the ephemeral list message — not the card's
    # bound message — which the token's "ephemeral" flag legitimizes
    out = notify_cards.apply_notification(
        led, {**_notif(tr["token"]),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "applied" and out["status"] == "in_progress"
    row = led.db.execute(
        "SELECT status,revision FROM requests WHERE request_id=?",
        (rid,)).fetchone()
    assert row["status"] == "in_progress" and row["revision"] == 2
    # the same view token is now stale — rejected, never double-applied
    out2 = notify_cards.apply_notification(
        led, {**_notif(tr["token"], n=21),
              "command_id": f"{tr['token']}:{'cd' * 8}",
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out2["outcome"] == "rejected" and out2["error"] == "stale_task"


def test_task_status_done_absorbs_and_terminal_rejected(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rid = _request(led, src_mid=100)
    r = _tasks_click(led, spec)
    done_tok = r["tasks"][0]["transitions"]["done"]["token"]
    out = notify_cards.apply_notification(
        led, {**_notif(done_tok),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "applied" and out["status"] == "done"
    # a fresh list offers no transition for a done task
    r2 = _tasks_click(led, spec, suffix="cd")
    t = next(t for t in r2["tasks"] if t["request_id"] == rid)
    assert t["transitions"] == {}
    # defensive: a hand-minted token pinning the live revision still
    # refuses to move a terminal row (the table is the authority)
    rid2 = _request(led, src_mid=101, status="cancelled")
    tok = notify_cards._mint_token(
        led.db, card["card_id"], "task_status",
        {"request_id": rid2, "status": "done", "ephemeral": True},
        {"request_rev": 1}, NOW)
    led.db.commit()
    out2 = notify_cards.apply_notification(
        led, {**_notif(tok),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out2["outcome"] == "rejected" \
        and out2["error"] == "request_not_open"


def test_task_status_rejects_non_ephemeral_mismatch(led, tmp_path):
    """Card-bound tokens still require the bound message — only tokens
    minted for an ephemeral surface may arrive on another message."""
    card, spec = _delivered_card(led, tmp_path)
    tok = _tasks_token(led, spec)
    out = notify_cards.apply_notification(
        led, {**_notif(tok), "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "rejected" \
        and out["error"] == "origin_mismatch"


def test_health_cards_lists_unsettled_attempts_for_resolve(led, tmp_path):
    """The operator worklist: each unsettled attempt carries the exact
    scope ops.card_resolve validates against — check the channel, then
    submit the approval flow. Nothing is re-sent from here."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r = _latest_render(led)
    _begin(led, r, n=1)
    health = notify_cards.health_cards(led)
    assert health["attempts_unsettled"] == 1
    assert health["oldest_unsettled_age_s"] == 0.0   # pinned NOW clock
    w = health["unsettled"][0]
    assert w["attempt_id"] == f"{1:016x}"
    assert w["delivery_id"] == r["delivery_id"]
    assert w["state"] == "granted"
    assert w["channel_id"] == r["channel_id"]
    assert w["resolve_scope"] == notify_cards.stored_scope(r)
