"""Synthetic notification card grant, receipt and operator recovery contracts."""
from __future__ import annotations

import notify_recovery
import json
import sqlite3
import pytest
import notify_cards
import notify_scope
import notify_spool
import notify_actions
import notify_transport
import notify_cmds
from notify_testkit import (
    CFG, NOW, ORIGIN, SCOPE, _begin, _card, _dispatch, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _token_for, _uuid, led, pinned_clock,
)

from notify_card_test_support import (
    CFG_OFF, NO_THREAD_CFG, _deliverable, _delivered_card, _journal, _rejected_no_reissue, _resolve, _spec_json
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


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


def test_unknown_and_expired_tokens(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    r = notify_actions.apply_notification(
        led, _notif("f" * 32), CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "unknown_token"
    tok = _token_for(spec, "assign")
    led.db.execute("UPDATE notification_action_tokens SET expires_at=? "
                   "WHERE token=?", (NOW - 1, tok))
    led.db.commit()
    r = notify_actions.apply_notification(led, _notif(tok), CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "token_expired"


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
    r = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["attempt_state"] == "delivered"
    assert _card(led)["message_id"] == "m-found"
    # replay is idempotent
    r2 = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
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
    r = notify_recovery.apply_card_resolve(led, dict(bad, reason="ok"), CFG,
                                        now=NOW)
    assert r["outcome"] == "rejected" \
        and r["error"] == "human_confirmation_required"


def test_cmd_int_rejects_card_resolve(led, tmp_path):
    """The resolve path is operator-only — a plugin-origin cmd_int file
    carrying it must be refused, never applied."""
    root = tmp_path / "data"
    notify_spool.ensure_dirs(str(root))
    req = {"version": 1, "op": "card_resolve", "command_id": _uuid(1),
           "delivery_id": _uuid(2), "attempt_id": "x" * 16}
    path = root / "cmd_int" / "r.json"
    path.write_text(json.dumps(req))
    result = {"errors": []}
    notify_cmds.drain_int_commands(led, result, CFG, str(root))
    assert path.with_suffix(".json.invalid").exists() and not path.exists()
    assert led.db.execute(
        "SELECT COUNT(*) c FROM command_receipts").fetchone()["c"] == 0


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
    assert not list(int_dir.glob("*.json"))
    assert {p.name for p in int_dir.iterdir()} == {notify_cmds._DRAIN_CURSOR}
    assert set(json.loads((int_dir / notify_cmds._DRAIN_CURSOR).read_text())) == {"after"}
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
    notify_spool.ensure_dirs(str(root))
    bad = root / "cmd_int" / "bad.json"
    bad.write_text(json.dumps({"version": 1, "op": "bogus"}))
    notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    assert bad.with_suffix(".json.invalid").exists()


def test_notification_receipt_reader(led, tmp_path):
    import ledger as _ledger2
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "ack")
    cid = f"{tok}:{'ab' * 8}"
    notify_actions.apply_notification(
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


def test_command_id_must_derive_from_token(led, tmp_path):
    """The notification idempotency key is <token>:<actor_hash> — a
    prefix that is not the submitted token can never dedupe correctly."""
    req = {"version": 1, "op": "notification",
           "command_id": f"{'a' * 32}:{'b' * 16}", "actor": "nurse-1",
           "token": "c" * 32, "origin": ORIGIN}
    assert notify_cmds.validate_int(req) == "command_id_mismatch"
    req["command_id"] = f"{'c' * 32}:{'b' * 16}"
    assert notify_cmds.validate_int(req) is None


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
    first = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
    assert first["outcome"] == "applied"
    again = notify_recovery.apply_card_resolve(led, dict(req), CFG,
                                            now=NOW + 1)
    assert again == first                     # stored receipt replay
    conflict = notify_recovery.apply_card_resolve(
        led, {**req, "result": "mark_not_sent",
              "evidence": {"method": "journal_review", "ref": "w1.jsonl",
                           "worker_stopped": True,
                           "proof": "no_journal_started"}},
        CFG, now=NOW + 2)
    assert conflict["outcome"] == "rejected"
    assert conflict["error"] == "command_id_conflict"


@pytest.mark.parametrize('bad_op', [[], {}])
def test_drain_quarantines_non_string_op_and_continues(led, tmp_path, bad_op):
    root = tmp_path / 'data'
    notify_spool.ensure_dirs(str(root))
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
    result = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
    assert result['outcome'] == 'rejected'
    assert result['error'] == 'attempt_conflict'
    assert _card(led)['message_id'] == 'original-message'


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
    r = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
    _rejected_no_reissue(led, r, "message_id_mismatch")
    r = notify_recovery.apply_card_resolve(
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
    r = notify_recovery.apply_card_resolve(led, req, CFG, now=NOW)
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


def test_body_replay_uses_live_source_and_revocation(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path, cfg=NO_THREAD_CFG)
    req = _notif(_token_for(spec, 'body'))
    req['origin'] = dict(ORIGIN, message_id='m-9')
    first = notify_actions.apply_notification(led, req, CFG, now=NOW)
    assert '本文' in first['body']
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE project_id=1")
    led.db.commit()
    second = notify_actions.apply_notification(led, req, CFG, now=NOW + 1)
    assert '（削除された投稿）' in second['body']
    assert ': 本文' not in second['body']
    notify_cards.revoke_card(led.db, card['card_id'], NOW + 2)
    led.db.commit()
    third = notify_actions.apply_notification(led, req, CFG, now=NOW + 3)
    assert third['outcome'] == 'rejected'
    assert 'body' not in third


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
    first = notify_actions.apply_notification(led, req, CFG, now=NOW)
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
    again = notify_actions.apply_notification(led, req, CFG, now=NOW + 1)
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


def test_health_cards_lists_unsettled_attempts_for_resolve(led, tmp_path):
    """The operator worklist: each unsettled attempt carries the exact
    scope ops.card_resolve validates against — check the channel, then
    submit the approval flow. Nothing is re-sent from here."""
    _seed_thread(led)
    _dispatch(led, _intent(led))
    r = _latest_render(led)
    _begin(led, r, n=1)
    health = notify_scope.health_cards(led.db)
    assert health["attempts_unsettled"] == 1
    assert health["oldest_unsettled_age_s"] == 0.0   # pinned NOW clock
    w = health["unsettled"][0]
    assert w["attempt_id"] == f"{1:016x}"
    assert w["delivery_id"] == r["delivery_id"]
    assert w["state"] == "granted"
    assert w["channel_id"] == r["channel_id"]
    assert w["resolve_scope"] == notify_scope.stored_scope(r)


@pytest.mark.parametrize("phase,result", [("started", None), ("result", "delivered")])
@pytest.mark.parametrize("proof", ["remote_absent", "api_rejected", "no_journal_started"])
def test_resolve_never_overrides_part_send_evidence(led, tmp_path, phase, result, proof):
    render = _deliverable(led, tmp_path)
    aid = f"{1:016x}"
    _begin(led, render)
    _journal(tmp_path, {"attempt_id": "p:" + render["delivery_id"].replace("-", "") + ":body:0001", "phase": phase, "result": result,
                        "delivery_id": render["delivery_id"], "part_id": "body:0001",
                        "message_id": "synthetic-part"})
    receipt = _resolve(led, render, aid, evidence={
        "method": "synthetic_check", "worker_stopped": True,
        "proof": proof, "ref": "synthetic"})
    _rejected_no_reissue(led, receipt, "journal_contradicts_proof")
