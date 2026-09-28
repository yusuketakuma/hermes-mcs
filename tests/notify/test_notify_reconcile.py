"""Restore-time delivery reconciliation — journal vs restored DB.

A DB restore rewinds card/render/attempt rows while external effects
(sent cards, journaled phases, spec files) persist. Before senders may
resume, the independent delivery journal is compared against the
restored DB: verified delivered effects stay delivered without a
duplicate, provable not_sent resumes, and anything unverifiable is
held fail-closed. All fixtures are synthetic; no real services.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _mcs_path  # noqa: F401

import ledger as _ledger
import notify_cards
import notify_reconcile
import notify_transport

from hermes_plugin.mcs_delivery import envelopes, journal, registry

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


def _attempt_row(led, aid, delivery_id, state="granted"):
    led.db.execute(
        """INSERT INTO notification_delivery_attempts(
             attempt_id,delivery_id,begin_command_id,state,
             error_code,worker_id,created_at,finished_at)
           VALUES(?,?,?,?,NULL,'w1',?,NULL)""",
        (aid, delivery_id, str(__import__("uuid").uuid4()), state, NOW))
    led.db.commit()


# ---------- marker ----------

def test_restore_pending_marker_blocks_begins(world):
    world.seed()
    world.dispatch()
    spec = world.spec(world.render()["delivery_id"])
    notify_cards.mark_restored(str(world.data), backup_path="b.db",
                               by="test")
    req = envelopes.transport_begin(
        {"attempt_id": registry.new_attempt_id(), "worker_id": "w" + "0" * 15,
         "spec": spec, "payload_hash": envelopes.payload_hash(spec)})
    out = notify_transport.apply_transport_begin(world.led, req, CFG)
    assert out["granted"] is False
    assert out["error"] == "denied_restore_pending"
    # no attempt row minted — a restore denial is not a send attempt
    assert world.led.db.execute(
        "SELECT COUNT(*) c FROM notification_delivery_attempts"
        ).fetchone()["c"] == 0
    # spec file survives — post-reconcile the same render can grant
    assert (world.data / "discord_render"
            / (spec["delivery_id"] + ".json")).exists()


def test_flags_publish_restore_pending(world):
    notify_cards.mark_restored(str(world.data), by="test")
    notify_cards.publish_flags(CFG, str(world.data))
    flags = json.loads(
        (world.data / "flags" / "notify.json").read_text())
    assert flags["restore_pending"] is True
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    notify_cards.publish_flags(CFG, str(world.data))
    flags = json.loads(
        (world.data / "flags" / "notify.json").read_text())
    assert flags["restore_pending"] is False


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"{}", b'[{"x":' * 1200],
                         ids=["syntax", "array", "empty", "recursive"])
def test_invalid_restore_marker_keeps_writers_and_senders_held(world, raw):
    path = world.data / notify_cards.RESTORE_MARKER
    path.write_bytes(raw)
    assert notify_cards.restore_pending(str(world.data)) is not None
    assert notify_cards.restore_awaiting_consent(str(world.data)) is not None
    with pytest.raises(ValueError, match="restore_marker_unreadable"):
        notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert path.read_bytes() == raw


def test_unreadable_restore_marker_is_not_absent(world):
    path = world.data / notify_cards.RESTORE_MARKER
    path.mkdir()
    assert notify_cards.restore_pending(str(world.data)) is not None
    assert notify_cards.restore_awaiting_consent(str(world.data)) is not None


@pytest.mark.parametrize("raw", [b"[]", b"null", b"1", b"[" * 1200, b"\xff"],
                         ids=["array", "null", "number", "recursive", "encoding"])
def test_flags_repaired_after_invalid_json_shape(world, raw):
    path = world.data / "flags" / "notify.json"
    path.write_bytes(raw)
    assert notify_cards.publish_flags(CFG, str(world.data)) is True
    flags = json.loads(path.read_bytes())
    assert flags["interactive"] is True
    assert notify_cards.publish_flags(CFG, str(world.data)) is False


def test_unreadable_journal_keeps_restore_hold(world):
    notify_cards.mark_restored(str(world.data))
    (world.data / "discord_state" / "journal-w1.jsonl").mkdir()
    with pytest.raises(OSError):
        notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert notify_cards.restore_pending(str(world.data)) is not None


def test_unattributed_corrupt_journal_cannot_release_restore(world):
    notify_cards.mark_restored(str(world.data))
    (world.data / "discord_state" / "journal-w1.jsonl").write_text("{broken\n")
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert rep["journal_incomplete"] is True
    assert notify_cards.restore_pending(str(world.data)) is not None


def test_corrupt_part_journal_does_not_prove_non_delivery(world):
    world.seed()
    world.dispatch()
    render = world.render()
    aid = f"p:{render['delivery_id'].replace('-', '')}:body:0001"
    state = world.data / "discord_state"
    journal.append(str(state), "w1", {
        "phase": "begin", "attempt_id": aid, "part_id": "body:0001",
        "delivery_id": render["delivery_id"]})
    with (state / "journal-w1.jsonl").open("a") as stream:
        stream.write("{broken\n")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    row = next(v for v in rep["verdicts"] if v["attempt_id"] == aid)
    assert row["verdict"] == "held"
    assert notify_cards.restore_pending(str(world.data)) is not None


# ---------- reconcile: delivered effect lost by restore ----------

def test_lost_delivered_attempt_holds_scope(world):
    """Backup predates the send; journal proves it delivered. The
    restored queued render must be held, the pending outbox event
    quarantined, and no re-send may occur."""
    world.seed()
    eid = world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data), backup_path="b.db")

    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert verdict["detail"] == "attempt_lost:delivered"
    # render held, card unknown, outbox event quarantined
    assert world.render()["state"] == "held"
    assert world.card()["delivery_state"] == "delivery_unknown"
    st = world.led.db.execute(
        "SELECT state,next_try FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()
    assert st["state"] == "failed" and st["next_try"] is None
    assert world.holds()
    # marker cleared only after the receipt was written
    assert notify_cards.restore_pending(str(world.data)) is None
    receipt = json.loads(
        (world.data / "restore_reconcile.json").read_text())
    assert receipt["held"]
    # a begin against the held render is denied — no send possible
    req = envelopes.transport_begin(
        {"attempt_id": registry.new_attempt_id(), "worker_id": "w" + "0" * 15,
         "spec": spec, "payload_hash": envelopes.payload_hash(spec)})
    out = notify_transport.apply_transport_begin(world.led, req, CFG)
    assert out["granted"] is False


def test_rebind_delivered_releases_hold(world):
    """Operator verifies the remote receipt and rebinds the card —
    delivered stays delivered, hold releases, nothing resends."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    notify_reconcile.reconcile_after_restore(world.led, CFG)

    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": str(__import__("uuid").uuid4()),
           "actor": "op-user", "human_confirmed": True,
           "reason": "remote message confirmed on channel",
           "delivery_id": render["delivery_id"],
           "attempt_id": aid,
           "result": "mark_delivered",
           "profile": "mcs", "application_id": "1", "guild_id": "7",
           "channel_id": "42", "message_id": "999001",
           "evidence": {"method": "remote_receipt", "ref": "msg:999001"}}
    out = notify_transport.apply_card_resolve(world.led, req, CFG)
    assert out["outcome"] == "applied"
    card = world.card()
    assert card["delivery_state"] == "delivered"
    assert card["message_id"] == "999001"
    assert all(h["released_at"] is not None for h in world.holds())


def test_lost_not_sent_attempt_resumes(world):
    """Journal proves the send never committed — the pending intent may
    redispatch without a hold."""
    world.seed()
    eid = world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "not_sent", error_code="http_403")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "not_sent"
    assert world.render()["state"] == "queued"
    st = world.led.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()
    assert st["state"] != "failed"
    assert not world.holds()


def test_lost_unknown_attempt_holds(world):
    """started without result — honestly unknown; hold, never resend."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    journal.append(state, "w1", {
        "phase": "started", "attempt_id": aid,
        "delivery_id": render["delivery_id"]})
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert world.render()["state"] == "held"


# ---------- reconcile: attempt row survives, receipt missing ----------

def test_present_attempt_catches_up_from_journal(world):
    """Restore kept the granted attempt but lost the settlement — the
    journal's factual result is re-applied via the normal receipt path."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"])
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "settled"
    assert verdict["detail"] == "delivered"
    assert world.attempt(aid)["state"] == "delivered"
    assert world.card()["delivery_state"] == "delivered"
    assert not world.holds()


def test_present_attempt_settles_unknown_from_started(world):
    """Attempt survived granted; journal shows started without result —
    the receipt settles it honestly unknown, never resends."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"])
    journal.append(state, "w1", {
        "phase": "started", "attempt_id": aid,
        "delivery_id": render["delivery_id"]})
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "settled"
    assert verdict["detail"] == "unknown"
    assert world.attempt(aid)["state"] == "unknown"
    assert world.card()["delivery_state"] == "delivery_unknown"


def test_present_attempt_pre_http_settles_not_sent(world):
    """Attempt survived granted; journal proves HTTP never began —
    settle not_sent so the card may re-issue."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"])
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "settled"
    assert verdict["detail"] == "not_sent"
    assert world.attempt(aid)["state"] == "not_sent"


def test_present_attempt_terminal_consistent(world):
    """DB already settled delivered with the same message — consistent,
    no-op."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"],
                 state="delivered")
    world.led.db.execute(
        "UPDATE notification_delivery_attempts SET message_id='999001'"
        " WHERE attempt_id=?", (aid,))
    world.led.db.commit()
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "consistent"
    assert not world.holds()


def test_present_attempt_terminal_conflict_holds(world):
    """Journal and DB disagree on a terminal fact — hold, never pick a
    side silently."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"],
                 state="delivered")
    world.led.db.execute(
        "UPDATE notification_delivery_attempts SET message_id='999001'"
        " WHERE attempt_id=?", (aid,))
    world.led.db.commit()
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="888002")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert world.holds()


def test_journal_less_attempt_is_held(world):
    """DB claims a granted attempt the journal never saw — the journal
    may be lost; hold the scope."""
    world.seed()
    world.dispatch()
    render = world.render()
    aid = registry.new_attempt_id()
    _attempt_row(world.led, aid, render["delivery_id"])
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert verdict["detail"] == "unknown_attempt"
    assert world.render()["state"] == "held"


def test_tainted_journal_upgrades_pre_http(world):
    """A corrupt line inside the journal file makes 'no started row'
    unprovable — pre_http promotes to hold, not not_sent."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    # corrupt a line mid-file
    path = Path(state) / "journal-w1.jsonl"
    path.write_text(path.read_text() + '{broken json\n')
    journal.append(state, "w1", {
        "phase": "claimed", "attempt_id": "other",
        "delivery_id": "x"})
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert "journal_corrupt" in verdict["detail"]


def test_scope_mismatch_receipt_is_held(world):
    """A journal receipt envelope whose scope disagrees with the stored
    render fails _receipt_check — hold instead of applying."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _attempt_row(world.led, aid, render["delivery_id"])
    # tamper the stored scope after the journal was written
    world.led.db.execute(
        "UPDATE notification_renders SET channel_id='999' "
        "WHERE delivery_id=?", (render["delivery_id"],))
    world.led.db.commit()
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    verdict = {v["attempt_id"]: v for v in rep["verdicts"]}[aid]
    assert verdict["verdict"] == "held"
    assert "scope" in verdict["detail"] or "mismatch" in verdict["detail"]


def test_held_card_blocks_reissue(world):
    """An active restore hold stops _issue_render from minting a new
    queued render even when drift would otherwise demand one."""
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    notify_reconcile.reconcile_after_restore(world.led, CFG)
    # drift the card — without the hold a new render would issue
    notify_cards.sweep(world.led, CFG)
    assert world.render()["state"] == "held"


def test_reconcile_idempotent(world):
    world.seed()
    world.dispatch()
    render = world.render()
    spec = world.spec(render["delivery_id"])
    state = str(world.data / "discord_state")
    aid = _journal_claim(state, "w1", spec)
    _journal_result(state, "w1", aid, render["delivery_id"],
                    "delivered", message_id="999001")
    notify_cards.mark_restored(str(world.data))
    rep1 = notify_reconcile.reconcile_after_restore(world.led, CFG)
    rep2 = notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert rep1["verdicts"] == rep2["verdicts"]
    assert len(world.holds()) == 1
