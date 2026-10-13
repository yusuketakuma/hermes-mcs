"""Shared synthetic restore-reconcile world for the notify tests — a
temp ledger with seed/dispatch/read helpers plus the journal phases a
real worker records.  Not a test module; its Discord ``CFG`` ids differ
from notify_testkit's."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import ledger as _ledger
import notify_cards
from adapters.common import envelopes, journal, registry

NOW = 1_790_000_000.0
CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                  "operator": "op-user", "card_thread": True,
                  "discord": {"profile": "mcs", "application_id": "1",
                              "guild_id": "7", "channel_id": "42"}},
       "signals": {"notify": True}}


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    notify_cards.ensure_dirs(str(data))
    led = _ledger.Ledger(str(data / "ledger.db"))

    def _seed(pid=1):
        led.db.execute(
            "INSERT INTO patients(project_id,patient_name,is_archived)"
            " VALUES(?,?,0)", (pid, "患者A"))
        for m in (100, 101):
            led.db.execute(
                "INSERT INTO messages(message_id,project_id,sender_name,"
                "posted_at,posted_at_ts,body_text,content_hash,"
                "body_state,parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (m, pid, "職員", f"2026-09-24T08:{m % 60:02d}",
                 int(NOW) + m, "本文", f"{m:064x}", "full", None))
        led.db.commit()

    def _dispatch(pid=1):
        eid = led.outbox_add(
            "new_messages", pid, {"message_ids": [100, 101]})
        ev = dict(led.db.execute(
            "SELECT * FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone())
        notify_cards.dispatch_intent(led, ev, CFG, now=NOW)
        return eid

    def _render():
        return dict(led.db.execute(
            "SELECT * FROM notification_renders "
            "ORDER BY render_rev DESC LIMIT 1").fetchone())

    def _card(card_id=1):
        return dict(led.db.execute(
            "SELECT * FROM notification_cards WHERE card_id=?",
            (card_id,)).fetchone())

    def _attempt(aid):
        r = led.db.execute(
            "SELECT * FROM notification_delivery_attempts "
            "WHERE attempt_id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def _spec(delivery_id):
        p = data / "discord_render" / (delivery_id + ".json")
        return json.loads(p.read_text()) if p.exists() else None

    def _holds():
        return [dict(r) for r in led.db.execute(
            "SELECT * FROM notification_restore_holds").fetchall()]

    yield SimpleNamespace(
        led=led, data=data, seed=_seed, dispatch=_dispatch,
        render=_render, card=_card, attempt=_attempt, spec=_spec,
        holds=_holds)
    led.close()


def _journal_claim(state_dir, worker_id, spec):
    """Journal the phases a real worker records for a claim."""
    aid = registry.new_attempt_id()
    claim = {"attempt_id": aid, "worker_id": worker_id,
             "spec": spec,
             "payload_hash": envelopes.payload_hash(spec)}
    journal.append(state_dir, worker_id, {
        "phase": "claimed", "attempt_id": aid,
        "delivery_id": spec["delivery_id"],
        "render_rev": spec["render_rev"], "op": spec["op"]})
    journal.append(state_dir, worker_id, {
        "phase": "begin", "attempt_id": aid,
        "delivery_id": spec["delivery_id"],
        "begin_envelope": envelopes.transport_begin(claim),
        "receipt_envelope": envelopes.transport_receipt(claim, "unknown")})
    return aid


def _journal_result(state_dir, worker_id, aid, delivery_id,
                    result, message_id=None, error_code=None,
                    started=True):
    if started:
        journal.append(state_dir, worker_id, {
            "phase": "started", "attempt_id": aid,
            "delivery_id": delivery_id})
    journal.append(state_dir, worker_id, {
        "phase": "result", "attempt_id": aid,
        "delivery_id": delivery_id, "result": result,
        "message_id": message_id, "error_code": error_code})
