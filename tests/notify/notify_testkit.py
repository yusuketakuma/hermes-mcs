"""Shared synthetic fixtures for the notify/plugin card test family —
the canonical Discord runner config, a temp ledger fixture, and the
dispatch/grant/receipt drivers over the real notify runner.  Not a test
module (no ``test_`` prefix); sibling files import it via the tests/
sys.path bootstrap."""
from __future__ import annotations

import json

import pytest

import ledger as _ledger
import notify_cards
import notify_transport

NOW = 1_790_000_000.0
CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                  "operator": "op-user", "card_thread": True,
                  "discord": {"profile": "mcs", "application_id": "app1",
                              "guild_id": "g1", "channel_id": "ch1"}},
       "signals": {"notify": True}}
SCOPE = {"profile": "mcs", "application_id": "app1",
         "guild_id": "g1", "channel_id": "ch1"}
ORIGIN = {**SCOPE, "message_id": "mid-1"}
# Canonical Discord worker settings for the notify/plugin test family —
# test_perf_cards shares this dict (tests/plugin/discord_testkit.py
# keeps its own copy).
SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7",
            "allowed_user_ids": {"1001"}, "allowed_chat_ids": {"42"},
            "project_ids": {1}}


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"[:36]


@pytest.fixture
def led(tmp_path):
    db_path = tmp_path / "data" / "ledger.db"
    (tmp_path / "data").mkdir()
    led = _ledger.Ledger(str(db_path))
    yield led
    led.close()


def _patient(led, pid=1, name="患者A", archived=0):
    led.db.execute(
        "INSERT INTO patients(project_id,patient_name,is_archived) "
        "VALUES(?,?,?)", (pid, name, archived))


def _msg(led, mid, pid=1, parent=None, body="本文", ts=None,
         sender="職員", prof="", org=""):
    ts = ts if ts is not None else int(NOW) + mid
    led.db.execute(
        "INSERT INTO messages(message_id,project_id,sender_name,"
        "profession,organization,posted_at,posted_at_ts,body_text,"
        "content_hash,body_state,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, sender, prof, org, f"2026-09-24T08:{mid % 60:02d}",
         ts, body, f"{mid:064x}", "full", parent))
    led.db.commit()


def _intent(led, kind="new_messages", pid=1, payload=None):
    payload = payload or {"message_ids": [100, 101]}
    eid = led.outbox_add(kind, pid, payload)
    return led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                          (eid,)).fetchone()


def _seed_thread(led, pid=1, root=100, mids=(100, 101)):
    _patient(led, pid)
    for m in mids:
        _msg(led, m, pid, parent=root if m != root else None)


def _dispatch(led, ev, cfg=CFG, now=NOW):
    return notify_cards.dispatch_intent(led, dict(ev), cfg, now=now)


def _card(led, card_id=1):
    return dict(led.db.execute(
        "SELECT * FROM notification_cards WHERE card_id=?",
        (card_id,)).fetchone())


def _latest_render(led, card_id=1):
    return led.db.execute(
        "SELECT * FROM notification_renders WHERE card_id=? "
        "ORDER BY render_rev DESC LIMIT 1", (card_id,)).fetchone()


def _begin(led, render, n=1, worker="bb" * 8, cfg=CFG):
    return notify_transport.apply_transport_begin(led, {
        "version": 1, "op": "transport_begin", "command_id": _uuid(n),
        "attempt_id": f"{n:016x}", "worker_id": worker,
        "delivery_id": render["delivery_id"],
        "render_rev": render["render_rev"],
        "payload_hash": render["payload_hash"], "route_epoch": 1,
        **SCOPE}, cfg, now=NOW)


def _settle_bodies(led, render):
    """The companion-thread body chunks and attachments of a delivered
    render land — an update waits while they are still pending."""
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered',"
        "remote_id=COALESCE(remote_id,'r-'||part_id) WHERE delivery_id=? "
        "AND kind IN ('body_part','attachment_part') AND state='pending'",
        (render["delivery_id"],))
    led.db.commit()


def _receipt(led, render, attempt_id, result="delivered",
             message_id="m-1", error_code=None, n=9):
    req = {"version": 1, "op": "transport_receipt", "command_id": _uuid(n),
           "attempt_id": attempt_id, "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "result": result, "message_id": message_id}
    if error_code is not None:
        req["error_code"] = error_code
    return notify_transport.apply_transport_receipt(led, req, CFG, now=NOW)


def _signal_row(led, key, pid=1, state="open", stype="med_followup",
                mids=None):
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,"
        "model,meta,created_at) VALUES('signal_v1',?,?,?,'test',?,?)",
        (pid, None, json.dumps({"type": stype, "state": state,
                                "project_id": pid, "severity": "info",
                                "note": f"note {key}",
                                "evidence":
                                    {"message_ids": mids or []}}),
         json.dumps({"key": key, "type": stype}), NOW))


def _notif(token, actor="nurse-1", n=20):
    return {"version": 1, "op": "notification",
            "command_id": f"{token}:{'ab' * 8}", "actor": actor,
            "token": token, "origin": ORIGIN}


def _token_for(spec, action):
    return next(b["token"] for row in spec["parts"]["action_rows"]
                for b in row if b["id"] == action)


def _extract(led, mid, content, kind="extract_v1", stale=False):
    """An extraction artifact bound to the message's current body
    revision — the same meta.hash gate the text notify_flush enforces."""
    h = led.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()["content_hash"]
    meta = {"hash": "0" * 64} if stale else {"hash": h}
    body = (json.dumps(content, ensure_ascii=False)
            if content is not None else "{bad json")
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,"
        "model,meta,created_at) VALUES(?,?,?,?,'test',?,?)",
        (kind, 1, mid, body, json.dumps(meta), NOW))
    led.db.commit()
