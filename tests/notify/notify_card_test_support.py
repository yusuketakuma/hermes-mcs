"""Shared synthetic card lifecycle setup without collected tests."""
from __future__ import annotations

import notify_recovery
import notify_parts
import json
import notify_cards
import notify_action_tokens
import notify_actions
from notify_testkit import (
    _settle_bodies,
    CFG, NOW, ORIGIN, SCOPE, _begin, _card, _dispatch, _intent,
    _latest_render, _msg, _notif, _receipt, _seed_thread,
    _token_for, _uuid,
)

NO_THREAD_CFG = {"notify": {k: v for k, v in CFG["notify"].items()
                            if k != "card_thread"},
                 "signals": CFG["signals"]}


CFG_OFF = {"notify": {"interactive": "off", "route_epoch": 1,
                      "discord": CFG["notify"]["discord"]}}


def _deliverable(led, tmp_path, cfg=CFG):
    _seed_thread(led)
    ev = _intent(led)
    _dispatch(led, ev, cfg=cfg)
    return _latest_render(led)


def _delivered_card(led, tmp_path, cfg=CFG):
    render = _deliverable(led, tmp_path, cfg=cfg)
    _begin(led, render, cfg=cfg)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9", cfg=cfg)
    _settle_bodies(led, render)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    return _card(led), spec


def _resolve(led, render, attempt_id, result="mark_not_sent",
             n=30, evidence=None, scope=None):
    return notify_recovery.apply_card_resolve(led, {
        "version": 1, "cmd": "ops.card_resolve", "command_id": _uuid(n),
        "actor": "op-user", "human_confirmed": True,
        "reason": "operator verified",
        "delivery_id": render["delivery_id"], "attempt_id": attempt_id,
        "result": result, **(scope or SCOPE),
        "evidence": evidence or {
            "method": "journal_check", "worker_stopped": True,
            "proof": "no_journal_started", "ref": "journal:t-1"}},
        CFG, now=NOW)


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


def _deliver_card(led, cfg=CFG):
    """dispatch + begin + delivered receipt -> a card bound to m-1."""
    _dispatch(led, _intent(led), cfg)
    r = _latest_render(led)
    _begin(led, r)
    _receipt(led, r, f"{1:016x}", message_id="m-1")
    _settle_bodies(led, r)
    return r


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


def _spec_json(led, delivery_id):
    return led.db.execute(
        "SELECT spec_json FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone()[0]


def _settle_parts(led, render):
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered' "
        "WHERE delivery_id=?", (render["delivery_id"],))
    notify_parts.update_state(led.db, render["delivery_id"], NOW)
    led.db.commit()


def _delivered_create(led, tmp_path):
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    return render


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
    tok = notify_action_tokens.mint(
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
    return notify_actions.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{suffix * 8}",
              "origin": dict(ORIGIN, message_id=msg_id)},
        CFG, now=NOW)
