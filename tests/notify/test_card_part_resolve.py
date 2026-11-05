"""Human-bound recovery of unknown parts, with synthetic ledger and journals."""
import notify_recovery
import json
from pathlib import Path

import pytest

import mcs_requests
import notify_spool
import notify_transport as transport
from notify_testkit import (CFG, NOW, _begin, _dispatch, _intent, _latest_render,

                            _part_receipt, _receipt, _seed_thread, _uuid, led as led)

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


def prepare(led, kind="body_part"):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, f"{1:016x}")["applied"]
    parts = json.loads(render["spec_json"])["parts"]["manifest"]
    target = next(p for p in parts if p["kind"] == kind)
    for part in parts[1:]:
        assert _part_receipt(led, render, part["part_id"],
                             result="unknown" if part == target else "delivered",
                             remote_id=None if part == target else "synthetic-" + part["part_id"])["applied"]
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], target["part_id"])
    assert "error" not in plan
    return {**plan, "command_id": _uuid(910), "actor": "synthetic-operator",
            "human_confirmed": True, "reason": "合成の配送確認",
            "result": "mark_delivered", "remote_id": "verified-synthetic-post",
            "evidence": {"method": "synthetic_lookup", "ref": "synthetic-proof", "worker_stopped": True}}


def test_plan_is_read_only_and_resolution_replay_is_idempotent(led):
    req = prepare(led)
    before = led.db.total_changes
    assert notify_recovery.card_part_resolve_plan(led, req["delivery_id"], req["part_id"])["expected_state"] == "unknown"
    assert led.db.total_changes == before
    assert mcs_requests.validate(req) is None
    receipt = notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "delivered"
    assert notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 2) == receipt
    assert notify_recovery.apply_card_part_resolve(led, {**req, "reason": "changed"}, CFG, NOW + 2)["error"] == "command_id_conflict"


@pytest.mark.parametrize("field,value", [("manifest_sha256", "a" * 64), ("payload_sha256", "b" * 64),
                                         ("attempt_id", "wrong-attempt"), ("channel_id", "wrong-channel")])
def test_stale_plan_never_settles_a_part(led, field, value):
    req = prepare(led)
    result = notify_recovery.apply_card_part_resolve(led, {**req, field: value}, CFG, NOW + 1)
    assert result["error"] == "stale_part_plan"
    assert notify_recovery.card_part_resolve_plan(led, req["delivery_id"], req["part_id"])["expected_state"] == "unknown"


def journal(led, req, result, remote_id=None):
    from pathlib import Path
    root = Path(notify_spool.notify_dirs(notify_spool.data_root(led))["discord_state"])
    (root / "journal-synthetic.jsonl").write_text(json.dumps({
        "attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
        "part_id": req["part_id"], "phase": "result", "result": result,
        "remote_id": remote_id}) + "\n")


def test_part_journal_delivered_cannot_be_overruled(led):
    req = prepare(led)
    journal(led, req, "delivered", "actual-post")
    assert notify_recovery.apply_card_part_resolve(led, req, CFG, NOW)["error"] == "remote_id_mismatch"
    req.update(command_id=_uuid(911), result="mark_not_sent",
               evidence={**req["evidence"], "proof": "remote_absent"})
    assert notify_recovery.apply_card_part_resolve(led, req, CFG, NOW)["error"] == "journal_contradicts_proof"


def test_part_not_sent_replans_only_missing_parts(led):
    req = prepare(led)
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "remote_absent"})
    receipt = notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied"
    latest = _latest_render(led)
    assert latest["delivery_id"] != req["delivery_id"]
    parts = json.loads(latest["spec_json"])["parts"]["manifest"]
    assert any(p["kind"] == "body_part" and p.get("prior_remote_id") for p in parts)


def test_lineworks_remote_absence_needs_risk_acceptance(led):
    req = prepare(led)
    req.update(transport="lineworks", team_id="synthetic-team", result="mark_not_sent",
               evidence={**req["evidence"], "proof": "remote_absent"})
    del req["guild_id"]
    assert notify_recovery.validate_card_part_resolve(req) == "duplicate_risk_acknowledgement_required"
    req["evidence"]["duplicate_risk_accepted"] = True
    assert notify_recovery.validate_card_part_resolve(req) is None


@pytest.mark.parametrize("blocked", ["restore", "update_failed"])
def test_part_resolution_does_not_release_independent_holds(led, blocked):
    req = prepare(led)
    if blocked == "restore":
        with led.db:
            led.db.execute("INSERT INTO notification_restore_holds(card_id,reason,held_at) VALUES(1,'synthetic_restore',?)", (NOW,))
    else:
        with led.db:
            led.db.execute("UPDATE notification_cards SET delivery_state='update_failed'")
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "remote_absent"})
    receipt = notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert receipt["outcome"] == "applied"
    assert _latest_render(led)["delivery_id"] == req["delivery_id"]


def test_command_intake_and_operator_receipt_scope(led, monkeypatch):
    from pathlib import Path
    import job_ops
    import mcs_util
    import mcs_view
    req = prepare(led)
    monkeypatch.setattr(mcs_util, "load_config", lambda *a, **kw: CFG)
    command_dir = Path(notify_spool.data_root(led)) / "cmd"
    command_dir.mkdir()
    command = command_dir / "synthetic-resolution.json"
    command.write_text(json.dumps(req))
    result = {"errors": []}
    job_ops.drain_commands(led, result, str(command_dir))
    assert not command.exists() and result["ops_commands"] == 1
    assert result["errors"] == []
    receipt = json.loads(led.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                      (req["command_id"],)).fetchone()[0])
    context = {**receipt["scope"], "actor": req["actor"], "projects": [1], "operator": False}
    assert mcs_view.View._scope_notification_receipt(receipt, context)["error"] == "operator_only"
    context["operator"] = True
    assert mcs_view.View._scope_notification_receipt(receipt, context)["outcome"] == "applied"


def test_unknown_card_remote_absent_can_be_human_resolved(led):
    from notify_testkit import SCOPE
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={'message_ids':[100]}))
    render = _latest_render(led)
    assert _begin(led, render)['granted']
    assert _receipt(led, render, f'{1:016x}', result='unknown', message_id=None)['applied']
    req={'version':1,'cmd':'ops.card_resolve','command_id':_uuid(987),
         'actor':'synthetic-review','human_confirmed':True,'reason':'synthetic remote absence confirmed',
         'delivery_id':render['delivery_id'],'attempt_id':f'{1:016x}',
         'result':'mark_not_sent',**SCOPE,
         'evidence':{'method':'synthetic_lookup','ref':'synthetic-only','worker_stopped':True,'proof':'remote_absent'}}
    result=notify_recovery.apply_card_resolve(led,req,CFG,NOW+1)
    assert result['outcome']=='applied', result


@pytest.mark.parametrize("proof", ["remote_absent", "api_rejected"])
def test_lineworks_part_evidence_limit_matches_proof(led, proof):
    from adapters.common import envelopes
    from notify_lineworks_testkit import LINEWORKS, MID, _apply, _claim, _render
    render = _render(led)
    claim = _claim(render)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(claim, "delivered", message_id=MID))["applied"]
    target = next(p for p in claim["spec"]["parts"]["manifest"] if p["kind"] == "body_part")
    for part in claim["spec"]["parts"]["manifest"][1:]:
        unknown = part == target
        assert _apply(led, envelopes.part_receipt(
            claim, part, "unknown" if unknown else "delivered",
            remote_id=None if unknown else "lw:synthetic:" + part["part_id"]))["applied"]
    req = {**notify_recovery.card_part_resolve_plan(led, render["delivery_id"], target["part_id"]),
           "command_id": _uuid(915), "actor": "synthetic-operator",
           "human_confirmed": True, "reason": "合成の配送確認", "result": "mark_not_sent",
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-proof",
                        "worker_stopped": True, "proof": proof}}
    if proof == "remote_absent":
        req["evidence"]["duplicate_risk_accepted"] = True
    else:
        from pathlib import Path
        root = Path(notify_spool.notify_dirs(notify_spool.data_root(led))["lineworks_state"])
        (root / "journal-synthetic.jsonl").write_text(json.dumps({
            "attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
            "part_id": req["part_id"], "phase": "result", "result": "not_sent"}) + "\n")
    receipt = notify_recovery.apply_card_part_resolve(led, req, LINEWORKS, NOW + 1)
    assert receipt["outcome"] == "applied"
    assert ("evidence_limit" in receipt) == (proof == "remote_absent")
    assert notify_recovery.apply_card_part_resolve(led, req, LINEWORKS, NOW + 2) == receipt


def test_settlement_rejection_rolls_back_partial_changes_and_consumes_command(led, monkeypatch):
    from pathlib import Path
    import job_ops
    import mcs_util
    req = prepare(led)
    before = [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts")]
    renders = led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0]
    calls = []

    def reject(db, *args, **kwargs):
        calls.append(True)
        db.execute("UPDATE notification_render_parts SET state='delivered'")
        return {"applied": False, "error": "part_conflict"}

    monkeypatch.setattr(transport, "settle_part_receipt", reject)
    monkeypatch.setattr(mcs_util, "load_config", lambda *a, **kw: CFG)
    command_dir = Path(notify_spool.data_root(led)) / "cmd"
    command_dir.mkdir()
    command = command_dir / "synthetic-resolution.json"
    command.write_text(json.dumps(req))
    result = {"errors": []}
    job_ops.drain_commands(led, result, str(command_dir))
    assert not command.exists()
    assert result["errors"] == ["cmd_invalid: part_settle_rejected"]
    receipt = notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 2)
    assert receipt["outcome"] == "rejected" and receipt["error"] == "part_settle_rejected"
    assert calls == [True]
    assert [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts")] == before
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == renders


def test_settlement_storage_failure_still_propagates_and_rolls_back(led, monkeypatch):
    import sqlite3
    req = prepare(led)
    before = [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts")]

    def fail(db, *args, **kwargs):
        db.execute("UPDATE notification_render_parts SET state='delivered'")
        raise sqlite3.OperationalError("synthetic storage failure")

    monkeypatch.setattr(transport, "settle_part_receipt", fail)
    with pytest.raises(sqlite3.OperationalError, match="synthetic storage failure"):
        notify_recovery.apply_card_part_resolve(led, req, CFG, NOW + 1)
    assert [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts")] == before
    assert led.db.execute("SELECT 1 FROM command_receipts WHERE command_id=?", (req["command_id"],)).fetchone() is None


def preflight_recovery(led, monkeypatch, *, error="edit_target_unverified", missing_spec=True, sibling=False,
                       repeated=False, cancelled=False, legacy_original=False):
    from adapters.common import envelopes
    import notify_cards
    import notify_parts
    import present_slack
    from notify_testkit import _attach
    from slack_testkit import SLACK

    monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)
    _seed_thread(led)
    _attach(led, 100, name="synthetic.pdf")
    if sibling:
        sibling_id = _attach(led, 101, name="synthetic-second.pdf")
        # Slack's parent render contains only the root post's attachments.
        with led.db:
            led.db.execute("UPDATE attachments SET message_id=100 WHERE attachment_id=?", (sibling_id,))
    caption = {"text": "synthetic original description"}
    monkeypatch.setattr(notify_parts, "_attachment_captions",
                        lambda db, card, attachments: {a["attachment_id"]: caption["text"] for a in attachments})
    if legacy_original:
        build = notify_parts.build_manifest
        def legacy_manifest(*args, **kwargs):
            parts, hold = build(*args, **kwargs)
            parts["manifest"] = [part for part in parts["manifest"] if part["kind"] != "file_group"]
            parts.get("slack", {}).pop("file_group", None)
            if parts.get("slack") == {}:
                parts.pop("slack")
            return parts, hold
        with monkeypatch.context() as patch:
            patch.setattr(notify_parts, "build_manifest", legacy_manifest)
            assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    else:
        assert _dispatch(led, _intent(led), SLACK)["dispatched"]

    def settle(render, rejection=None):
        spec = json.loads(render["spec_json"])
        claim = {"spec": spec, "attempt_id": f"{render['render_rev']:016x}",
                 "worker_id": "b" * 16, "payload_hash": render["payload_hash"]}
        assert transport.apply_transport_begin(led, envelopes.transport_begin(claim), SLACK, now=NOW)["granted"]
        assert transport.apply_transport_receipt(
            led, envelopes.transport_receipt(claim, "delivered", message_id="1790000000.000001"),
            SLACK, now=NOW)["applied"]
        target = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
        for part in spec["parts"]["manifest"][1:]:
            unknown = rejection is not None and part == target
            remote = ("1790000000.000001" if part["kind"] in ("thread", "file_group") else
                      "F_SYNTHETIC_" + str(part["attachment_id"]) if part["kind"] == "attachment_part" else
                      f"1790000001.{part['index']:06d}")
            assert transport.apply_part_receipt(
                led, envelopes.part_receipt(claim, part, "unknown" if unknown else "delivered",
                                            remote_id=None if unknown else remote,
                                            error_code=rejection if unknown else None), SLACK, now=NOW)["applied"]
        return claim, target

    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))

    def issue_current():
        candidates = []
        with led.db:
            assert notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, candidates, force=True)
        notify_spool.publish_specs(led.db, dirs, candidates, NOW + 1)
        return _latest_render(led)

    original = _latest_render(led)
    settle(original)
    if repeated:
        settle(issue_current())
    if cancelled:
        unused = issue_current()
        with led.db:
            notify_cards._cancel_render(led.db, unused["delivery_id"], NOW + 1)
    caption["text"] = "synthetic rejected description"
    rejected = issue_current()
    claim, target = settle(rejected, error)
    assert target["edit_only"] is True
    req = {**notify_recovery.card_part_resolve_plan(led, rejected["delivery_id"], target["part_id"]),
           "command_id": _uuid(990), "actor": "synthetic-operator", "human_confirmed": True,
           "reason": "合成の送信前拒否を確認し、現在内容の説明更新を承認", "result": "mark_not_sent",
           "evidence": {"method": "synthetic_local_journal", "ref": "synthetic-proof",
                        "worker_stopped": True, "proof": "local_preflight_rejected"}}
    identity = {"v": 1, "worker_id": "b" * 16, "ts": NOW,
                "attempt_id": req["attempt_id"], "delivery_id": req["delivery_id"],
                "part_id": req["part_id"], "kind": "attachment_part"}
    rows = [{**identity, "phase": "started", "receipt_envelope": envelopes.part_receipt(claim, target, "unknown")},
            {**identity, "phase": "result", "result": "unknown", "remote_id": None, "error_code": error,
             "receipt_envelope": envelopes.part_receipt(claim, target, "unknown", error_code=error)},
            {**identity, "phase": "receipt", "result": "unknown"}]
    path = Path(dirs["slack_state"]) / "journal-synthetic-preflight.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    if missing_spec:
        with led.db:
            # GC removes full specs while retaining the existing hash-bound identity witnesses.
            for saved in led.db.execute("SELECT delivery_id FROM notification_renders").fetchall():
                witness = notify_parts.attachment_identity_witness(led.db, saved["delivery_id"])
                raw = json.dumps({"attachment_identity": witness}) if witness is not None else None
                led.db.execute("UPDATE notification_renders SET spec_json=?,spec_published=0 WHERE delivery_id=?",
                               (raw, saved["delivery_id"]))
        for spec_path in Path(dirs["slack_render"]).glob("*.json"):
            spec_path.unlink()
    caption["text"] = "synthetic current description"
    return req, original, target, path, rows


def recovery_state(led):
    tables = ("notification_cards", "notification_renders", "notification_render_parts",
              "notification_view_manifests", "notification_action_tokens", "notification_intent_cards",
              "notification_delivery_attempts", "notification_restore_holds")
    return {name: [tuple(row) for row in led.db.execute("SELECT * FROM " + name)] for name in tables}


@pytest.mark.parametrize("original_collected", [False, True])
def test_manually_confirmed_caption_edit_keeps_sealed_upload_for_next_render(led, monkeypatch, original_collected):
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    with led.db:
        if original_collected:
            import notify_parts
            witness = notify_parts.attachment_identity_witness(led.db, original["delivery_id"])
            assert witness is not None
            led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                           (json.dumps({"attachment_identity": witness}), original["delivery_id"]))
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       ("synthetic changed body", "c" * 64))
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "applied"
    latest = _latest_render(led)
    assert latest["delivery_id"] != req["delivery_id"]
    assert latest["state"] == "queued"
    part = next(p for p in json.loads(latest["spec_json"])["parts"]["manifest"]
                if p["part_id"] == target["part_id"])
    assert part["prior_remote_id"] == target["prior_remote_id"]
    assert part["edit_only"] is True


@pytest.mark.parametrize("fault", ["receipt_missing", "receipt_hash", "receipt_evidence", "receipt_duplicate_keys",
                                   "duplicate_receipt", "remote_id",
                                   "manual_scope", "manual_thread", "original_operator"])
def test_manual_caption_edit_requires_receipt_and_original_upload(led, monkeypatch, fault):
    import notify_cards
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    with led.db:
        if fault == "receipt_missing":
            led.db.execute("DELETE FROM command_receipts WHERE command_id=?", (req["command_id"],))
        elif fault == "receipt_hash":
            led.db.execute("UPDATE command_receipts SET payload_hash=? WHERE command_id=?",
                           ("f" * 64, req["command_id"]))
        elif fault == "receipt_evidence":
            saved = led.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                   (req["command_id"],)).fetchone()[0]
            saved = json.loads(saved)
            saved["evidence"]["worker_stopped"] = False
            led.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                           (json.dumps(saved), req["command_id"]))
        elif fault == "receipt_duplicate_keys":
            raw = led.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                 (req["command_id"],)).fetchone()[0]
            led.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                           (raw[:-1] + ',"reason":"duplicate"}', req["command_id"]))
        elif fault == "duplicate_receipt":
            led.db.execute("INSERT INTO command_receipts SELECT ?,payload_hash,project_id,request_id,outcome,"
                           "receipt_json,processed_at FROM command_receipts WHERE command_id=?",
                           (_uuid(992), req["command_id"]))
        elif fault == "remote_id":
            led.db.execute("UPDATE notification_render_parts SET remote_id='F_OTHER' WHERE delivery_id=? AND part_id=?",
                           (req["delivery_id"], req["part_id"]))
        elif fault == "manual_scope":
            led.db.execute("UPDATE notification_renders SET team_id='T_OTHER' WHERE delivery_id=?", (req["delivery_id"],))
        elif fault == "manual_thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='OTHER_THREAD' WHERE delivery_id=? AND kind='thread'",
                           (req["delivery_id"],))
        else:
            led.db.execute("UPDATE notification_render_parts SET error_code='operator_part_resolve' "
                           "WHERE delivery_id=? AND part_id=?", (original["delivery_id"], req["part_id"]))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 3, emitted, force=True)
    assert emitted == []
    assert _latest_render(led)["state"] == "held"


def test_prior_native_edit_alone_does_not_prove_the_original_upload(led, monkeypatch):
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False, repeated=True)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    with led.db:
        led.db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                       (original["delivery_id"], req["part_id"]))
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       ("synthetic changed body", "e" * 64))
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    assert _latest_render(led)["state"] == "held"


def test_legacy_collected_update_upload_with_later_native_edit_remains_usable(led, monkeypatch):
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False, repeated=True, legacy_original=True)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL,op='update' WHERE delivery_id=?",
                       (original["delivery_id"],))
        native = led.db.execute("SELECT delivery_id,spec_json FROM notification_renders WHERE render_rev=2").fetchone()
        spec = json.loads(native["spec_json"])
        part = next(p for p in spec["parts"]["manifest"] if p["part_id"] == req["part_id"])
        assert part["prior_remote_id"] == target["prior_remote_id"]
        part["edit_only"] = True
        led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                       (mcs_requests.canonical(spec).decode(), mcs_requests.payload_hash(spec), native["delivery_id"]))
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       ("synthetic changed body", "f" * 64))
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    assert _latest_render(led)["state"] == "queued"


@pytest.mark.parametrize("obstacle", [None, "missing_receipt", "attempt", "published", "previously_published",
                                      "part_attempt", "part_remote"])
def test_held_manual_caption_replans_only_after_proof_is_valid(led, monkeypatch, obstacle):
    import notify_cards
    import notify_parts
    from slack_testkit import SLACK

    req, _, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       ("synthetic changed body", "d" * 64))
    with monkeypatch.context() as patch:
        patch.setattr(notify_parts, "_manual_edit_receipts", lambda db, rows: {})
        assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    held = _latest_render(led)
    assert held["state"] == "held"
    with led.db:
        if obstacle == "missing_receipt":
            led.db.execute("DELETE FROM command_receipts WHERE command_id=?", (req["command_id"],))
        elif obstacle == "published":
            led.db.execute("UPDATE notification_renders SET spec_published=1 WHERE delivery_id=?", (held["delivery_id"],))
        elif obstacle == "previously_published":
            led.db.execute("UPDATE notification_renders SET first_published_at=? WHERE delivery_id=?", (NOW, held["delivery_id"]))
        elif obstacle in ("part_attempt", "part_remote"):
            column = "attempt_id" if obstacle == "part_attempt" else "remote_id"
            led.db.execute("UPDATE notification_render_parts SET " + column + "='synthetic-prior' "
                           "WHERE delivery_id=? AND kind='attachment_part'", (held["delivery_id"],))
        elif obstacle == "attempt":
            led.db.execute("INSERT INTO notification_delivery_attempts "
                           "(attempt_id,delivery_id,begin_command_id,state,created_at) VALUES (?,?,?,?,?)",
                           ("synthetic-held-attempt", held["delivery_id"], _uuid(991), "not_sent", NOW))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 3, emitted)
    latest = _latest_render(led)
    if obstacle is None:
        assert len(emitted) == 1 and latest["state"] == "queued"
        assert latest["delivery_id"] != held["delivery_id"]
    else:
        assert not emitted and latest["delivery_id"] == held["delivery_id"]
        assert latest["state"] == "held"


@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("legacy_private_witness", [False, True])
def test_manual_caption_witness_survives_gc_without_text(led, monkeypatch, corrupt, legacy_private_witness):
    import notify_cards
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    import notify_parts
    identity = notify_parts._sealed_attachment_identity
    def old_identity(*args, **kwargs):
        proof = identity(*args, **kwargs)
        if proof is not None and proof[5] == "private_operator_edit":
            return (*proof[:5], "private_stage", *proof[6:])
        return proof
    with monkeypatch.context() as patch:
        if legacy_private_witness:
            patch.setattr(notify_parts, "_sealed_attachment_identity", old_identity)
        notify_cards.gc(led, SLACK, now=NOW + 3, limit=100)
    raw = led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                         (req["delivery_id"],)).fetchone()[0]
    witness = json.loads(raw)
    assert "attachment_identity" in witness
    assert "synthetic original description" not in raw and "synthetic.pdf" not in raw
    original_witness = led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                                      (original["delivery_id"],)).fetchone()[0]
    assert "attachment_identity" in json.loads(original_witness)
    assert "synthetic.pdf" not in original_witness
    with led.db:
        if corrupt:
            witness["attachment_identity"]["proofs"][0] = "f" * 64
            led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                           (json.dumps(witness), req["delivery_id"]))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 4, emitted, force=True)
    if corrupt:
        assert not emitted and _latest_render(led)["state"] == "held"
    else:
        assert len(emitted) == 1 and _latest_render(led)["state"] == "queued"
        part = next(p for p in emitted[0]["parts"]["manifest"] if p["part_id"] == target["part_id"])
        assert part["prior_remote_id"] == target["prior_remote_id"]


def test_manual_then_native_edit_remain_reusable_after_both_specs_are_collected(led, monkeypatch):
    from adapters.common import envelopes
    import notify_cards
    from slack_testkit import SLACK

    req, _, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    notify_cards.gc(led, SLACK, now=NOW + 3, limit=100)
    with led.db:
        specs = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 4, specs, force=True)
    spec, = specs
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(notify_spool.data_root(led)), specs, NOW + 4)
    claim = {"spec": spec, "attempt_id": "e" * 16, "worker_id": "b" * 16, "payload_hash": mcs_requests.payload_hash(spec)}
    assert transport.apply_transport_begin(led, envelopes.transport_begin(claim), SLACK, now=NOW + 4)["granted"]
    assert transport.apply_transport_receipt(led, envelopes.transport_receipt(
        claim, "delivered", message_id="1790000000.000001"), SLACK, now=NOW + 4)["applied"]
    for part in spec["parts"]["manifest"][1:]:
        remote = ("1790000000.000001" if part["kind"] in ("thread", "file_group") else
                  target["prior_remote_id"] if part["kind"] == "attachment_part" else f"synthetic-{part['index']}")
        assert transport.apply_part_receipt(led, envelopes.part_receipt(
            claim, part, "delivered", remote_id=remote), SLACK, now=NOW + 4)["applied"]
    notify_cards.gc(led, SLACK, now=NOW + 5, limit=100)
    with led.db:
        later = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 6, later, force=True)
    assert len(later) == 1 and _latest_render(led)["state"] == "queued"
    part = next(p for p in later[0]["parts"]["manifest"] if p["part_id"] == target["part_id"])
    assert part["prior_remote_id"] == target["prior_remote_id"]


@pytest.mark.parametrize("invalid", [None, "body", "attachment", "duplicate_body", "escaped_duplicate_body",
                                     "duplicate_attachment"])
def test_compound_gc_witness_requires_both_existing_proof_formats(led, monkeypatch, invalid):
    import notify_cards
    import notify_scope
    from slack_testkit import SLACK

    req, _, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    notify_cards.gc(led, SLACK, now=NOW + 3, limit=100)
    row = led.db.execute("SELECT * FROM notification_renders WHERE delivery_id=?", (req["delivery_id"],)).fetchone()
    witness = json.loads(row["spec_json"])
    witness["body_delivery_coverage"] = {"version": 1, "payload_hash": row["payload_hash"],
                                         "proofs": [["a" * 64, "b" * 64, "c" * 64]]}
    if invalid == "body":
        witness["body_delivery_coverage"]["payload_hash"] = "f" * 64
    elif invalid == "attachment":
        witness["attachment_identity"]["proofs"] = ["invalid"]
    elif invalid in ("duplicate_body", "escaped_duplicate_body"):
        witness["body_delivery_coverage"]["proofs"] *= 2
    elif invalid == "duplicate_attachment":
        witness["attachment_identity"]["proofs"] *= 2
    raw = json.dumps(witness)
    if invalid == "escaped_duplicate_body":
        raw = raw.replace('"' + "a" * 64 + '"', '"\\u0061' + "a" * 63 + '"', 1)
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?", (raw, req["delivery_id"]))
    row = led.db.execute("SELECT * FROM notification_renders WHERE delivery_id=?", (req["delivery_id"],)).fetchone()
    body = notify_scope._body_witness_proofs(raw, row)
    attachment = notify_scope._attachment_identity_witness_proofs(raw, row)
    sql = led.db.execute(f"SELECT {notify_scope.BODY_COVERAGE_WITNESS_SQL}, "
                         f"{notify_scope.ATTACHMENT_IDENTITY_WITNESS_SQL} "
                         "FROM notification_renders r WHERE delivery_id=?", (req["delivery_id"],)).fetchone()
    assert (body is not None) == (invalid is None)
    assert (attachment is not None) == (invalid is None)
    assert tuple(sql) == ((1, 1) if invalid is None else (0, 0))


@pytest.mark.parametrize("field", ["op", "route_epoch", "correlation", "card_kind"])
def test_collected_attachment_witness_rejects_changed_sealed_binding(led, monkeypatch, field):
    import notify_cards
    import notify_parts
    import notify_scope
    from slack_testkit import SLACK

    req, original, target, _, _ = preflight_recovery(led, monkeypatch, missing_spec=False)
    req.update(result="mark_delivered", remote_id=target["prior_remote_id"])
    req["evidence"].pop("proof")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"
    notify_cards.gc(led, SLACK, now=NOW + 3, limit=100)
    with led.db:
        if field == "card_kind":
            led.db.execute("UPDATE notification_cards SET kind='signal' WHERE card_id=1")
        else:
            value = {"op": "update", "route_epoch": 99, "correlation": "f" * 32}[field]
            led.db.execute("UPDATE notification_renders SET " + field + "=? WHERE delivery_id=?",
                           (value, original["delivery_id"]))
        card = notify_scope.card_row(led.db, 1)
        _, _, identities = notify_parts._same_thread_delivered_parts(led.db, card)
    assert any(proof is None for proofs in identities.values() for proof in proofs)


def reject_preflight(led, req, monkeypatch, expected):
    from slack_testkit import SLACK
    before = recovery_state(led)
    published = []
    monkeypatch.setattr(notify_spool, "publish_specs", lambda *args: published.extend(args[2]))
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "rejected" and receipt["error"] == expected
    assert recovery_state(led) == before
    assert not published
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3) == receipt
    return receipt


@pytest.mark.parametrize("error", ["edit_target_unverified", "edit_history_unverified"])
@pytest.mark.parametrize("missing_spec", [True, False])
def test_local_preflight_recovery_uses_current_caption_and_only_exact_edits(led, monkeypatch, error, missing_spec):
    from adapters.slack.cards import validate
    from slack_testkit import SLACK
    req, original, target, path, _ = preflight_recovery(led, monkeypatch, error=error, missing_spec=missing_spec, sibling=True)
    journal_bytes = path.read_bytes()
    old_spec_bytes = led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                                    (req["delivery_id"],)).fetchone()[0]
    original_rows = [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?",
                                                       (original["delivery_id"],))]
    assert mcs_requests.validate(req) is None
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "not_sent", receipt.get("error")
    latest = _latest_render(led)
    spec = json.loads(latest["spec_json"])
    assert validate(spec) == spec
    assert spec["op"] == "update" and spec["delivery"]["thread_id"] == "1790000000.000001"
    assert spec["delivery"]["message_id"] == "1790000000.000001"
    assert receipt["replacement_delivery_id"] == latest["delivery_id"] != req["delivery_id"]
    assert receipt["replacement_manifest_sha256"] == latest["payload_hash"] == mcs_requests.payload_hash(spec)
    assert receipt["content_basis"] == "current_content"
    parts = spec["parts"]["manifest"]
    assert any(p["kind"] == "body_part" for p in parts)
    for part in parts:
        if part["kind"] in ("body_part", "attachment_part"):
            assert part["edit_only"] is True and part["prior_remote_id"]
        if part["part_id"] == target["part_id"]:
            assert part["prior_remote_id"] == target["prior_remote_id"] and part["sha256"] == req["payload_sha256"]
            assert part["caption"] == "synthetic current description"
    published = Path(notify_spool.notify_dirs(notify_spool.data_root(led))["slack_render"]) / (latest["delivery_id"] + ".json")
    assert published.read_bytes() == mcs_requests.canonical(spec)
    assert path.read_bytes() == journal_bytes
    assert [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?",
                                               (original["delivery_id"],))] == original_rows
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3) == receipt
    old = led.db.execute("SELECT spec_json,payload_hash FROM notification_renders WHERE delivery_id=?", (req["delivery_id"],)).fetchone()
    assert old["payload_hash"] == req["manifest_sha256"]
    assert old["spec_json"] == old_spec_bytes
    if missing_spec:
        from notify_scope import _attachment_identity_witness_proofs
        assert "parts" not in json.loads(old["spec_json"])
        assert _attachment_identity_witness_proofs(old["spec_json"], old) is not None


@pytest.mark.parametrize("change,expected", [
    ({"human_confirmed": False}, "human_confirmation_required"),
    ({"result": "mark_delivered", "remote_id": "F_SYNTHETIC_1"}, "bad_preflight_resolution"),
    ({"remote_id": "F_SYNTHETIC_1"}, "bad_preflight_resolution"),
    ({"worker_stopped": False}, "worker_not_proven_stopped"),
])
def test_preflight_proof_requires_its_own_human_and_worker_contract(led, monkeypatch, change, expected):
    req, *_ = preflight_recovery(led, monkeypatch)
    if "worker_stopped" in change:
        req["evidence"].update(change)
    else:
        req.update(change)
    reject_preflight(led, req, monkeypatch, expected)


@pytest.mark.parametrize("change", ["missing", "tainted", "interrupted", "second_start", "second_result",
    "other_attempt", "delivered", "remote", "unknown_error", "receipt_result", "worker",
    "start_scope", "result_scope", "manifest", "revision", "epoch", "correlation", "kind", "attempt"])
def test_preflight_proof_rejects_incomplete_or_contradictory_journal(led, monkeypatch, change):
    req, _, _, path, rows = preflight_recovery(led, monkeypatch)
    if change == "missing":
        rows = []
    elif change == "interrupted":
        rows.pop()
    elif change == "second_start":
        rows.insert(1, rows[0])
    elif change == "second_result":
        rows.insert(2, rows[1])
    elif change == "other_attempt":
        rows.append({**rows[0], "attempt_id": "another-attempt"})
    elif change == "delivered":
        rows[1]["result"] = "delivered"
    elif change == "remote":
        rows[1]["remote_id"] = "F_OTHER"
    elif change == "unknown_error":
        rows[1]["error_code"] = "timeouterror"
    elif change == "receipt_result":
        rows[2]["result"] = "delivered"
    elif change == "worker":
        rows[1]["worker_id"] = "c" * 16
    elif change in ("start_scope", "result_scope"):
        rows[0 if change == "start_scope" else 1]["receipt_envelope"]["channel_id"] = "C_OTHER"
    elif change in ("manifest", "revision", "epoch", "correlation", "kind", "attempt"):
        key, value = {"manifest": ("payload_hash", "c" * 64), "revision": ("render_rev", 99),
                      "epoch": ("route_epoch", 99), "correlation": ("correlation", "c" * 32),
                      "kind": ("kind", "body_part"), "attempt": ("attempt_id", "p:" + "a" * 32 + ":attach:0001")}[change]
        rows[1]["receipt_envelope"][key] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + ("broken\n" if change == "tainted" else ""))
    reject_preflight(led, req, monkeypatch, "local_preflight_unproven")


@pytest.mark.parametrize("change", ["error", "op", "kind"])
def test_preflight_proof_is_only_for_slack_attachment_update_refusal(led, monkeypatch, change):
    req, *_ = preflight_recovery(led, monkeypatch)
    with led.db:
        if change == "op":
            led.db.execute("UPDATE notification_renders SET op='create' WHERE delivery_id=?", (req["delivery_id"],))
        else:
            column, value = ("error_code", "timeouterror") if change == "error" else ("kind", "body_part")
            led.db.execute("UPDATE notification_render_parts SET " + column + "=? WHERE delivery_id=? AND part_id=?",
                           (value, req["delivery_id"], req["part_id"]))
    reject_preflight(led, req, monkeypatch, "local_preflight_unproven")


@pytest.mark.parametrize("change", ["missing", "part_id", "attachment_id", "hash", "name", "bytes", "scope", "thread", "operator"])
def test_preflight_requires_original_upload_identity(led, monkeypatch, change):
    req, original, _, _, _ = preflight_recovery(led, monkeypatch)
    with led.db:
        if change == "missing":
            led.db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                           (original["delivery_id"], req["part_id"]))
        elif change == "scope":
            led.db.execute("UPDATE notification_renders SET channel_id='C_OTHER' WHERE delivery_id=?", (original["delivery_id"],))
        elif change == "thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='1790000099.000001' WHERE delivery_id=? AND kind='thread'",
                           (original["delivery_id"],))
        else:
            column, value = {"part_id": ("part_id", "attach:9999"), "attachment_id": ("attachment_id", 9999),
                             "hash": ("payload_sha256", "c" * 64), "name": ("name", "other.pdf"),
                             "bytes": ("bytes", 9999), "operator": ("error_code", "operator_part_resolve")}[change]
            led.db.execute("UPDATE notification_render_parts SET " + column + "=? WHERE delivery_id=? AND part_id=?",
                           (value, original["delivery_id"], req["part_id"]))
    reject_preflight(led, req, monkeypatch, "preflight_target_unverified")


@pytest.mark.parametrize("hold", ["restore", "update_failed", "marker", "multiple_unknown"])
def test_preflight_does_not_resolve_under_independent_holds(led, monkeypatch, hold):
    req, *_ = preflight_recovery(led, monkeypatch)
    with led.db:
        if hold == "restore":
            led.db.execute("INSERT INTO notification_restore_holds(card_id,reason,held_at) VALUES(1,'synthetic_restore',?)", (NOW,))
        elif hold == "update_failed":
            led.db.execute("UPDATE notification_cards SET delivery_state='update_failed'")
        elif hold == "multiple_unknown":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND kind='body_part'", (req["delivery_id"],))
    if hold == "marker":
        notify_spool.mark_restored(notify_spool.data_root(led), now=NOW)
    reject_preflight(led, req, monkeypatch, "preflight_target_unverified" if hold in ("update_failed", "multiple_unknown")
                     else "preflight_replan_blocked")


@pytest.mark.parametrize("change", ["missing_prior", "sibling_upload", "changed_file", "missing_target", "scope", "thread",
                                     "create", "published", "part_started", "attempt", "blocked",
                                     "group_flag", "group_nonbool", "group_prior", "group_not_last", "group_missing"])
def test_unsafe_candidate_rolls_back_settlement_plan_and_publication(led, monkeypatch, change):
    import notify_cards
    req, _, target, _, _ = preflight_recovery(led, monkeypatch, sibling=True)
    issue = notify_cards.issue_render

    def tamper(db, card_id, cfg, now, specs, **kwargs):
        delivery_id = issue(db, card_id, cfg, now, specs, **kwargs)
        assert delivery_id and len(specs) == 1
        spec = specs[0]
        if change == "blocked":
            return None  # Even an appended but rejected candidate must not escape.
        if change in ("missing_prior", "sibling_upload", "changed_file", "missing_target"):
            if change == "missing_prior":
                part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "body_part")
                part.pop("prior_remote_id")
            elif change == "sibling_upload":
                part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part" and p["part_id"] != target["part_id"])
                part.pop("prior_remote_id")
            elif change == "changed_file":
                part = next(p for p in spec["parts"]["manifest"] if p["part_id"] == target["part_id"])
                part["sha256"] = "c" * 64
                db.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id=?",
                           (part["sha256"], delivery_id, part["part_id"]))
            else:
                spec["parts"]["manifest"] = [p for p in spec["parts"]["manifest"] if p["part_id"] != target["part_id"]]
                db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?", (delivery_id, target["part_id"]))
        elif change.startswith("group_"):
            group = next(p for p in spec["parts"]["manifest"] if p["kind"] == "file_group")
            if change == "group_flag":
                spec["parts"]["slack"].pop("file_group")
            elif change == "group_nonbool":
                spec["parts"]["slack"]["file_group"] = "true"
            elif change == "group_prior":
                group["prior_remote_id"] = "unverified-post"
            elif change == "group_not_last":
                spec["parts"]["manifest"].remove(group)
                spec["parts"]["manifest"].insert(1, group)
            else:
                spec["parts"]["manifest"].remove(group)
                db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id='file_group'", (delivery_id,))
        elif change == "scope":
            spec["delivery"]["channel_id"] = "C_OTHER"
        elif change == "thread":
            spec["delivery"]["thread_id"] = "1790000099.000001"
        elif change == "create":
            spec["op"] = "create"
        elif change == "published":
            db.execute("UPDATE notification_renders SET spec_published=1 WHERE delivery_id=?", (delivery_id,))
        elif change == "part_started":
            db.execute("UPDATE notification_render_parts SET attempt_id='synthetic-started' WHERE delivery_id=? AND kind='body_part'", (delivery_id,))
        elif change == "attempt":
            db.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,state,created_at) "
                       "VALUES(?,?,?,'granted',?)", ("f" * 16, delivery_id, _uuid(991), NOW))
        db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                   (mcs_requests.canonical(spec).decode(), mcs_requests.payload_hash(spec), delivery_id))
        return delivery_id

    monkeypatch.setattr(notify_cards, "issue_render", tamper)
    reject_preflight(led, req, monkeypatch, "preflight_replan_blocked" if change == "blocked" else "preflight_replan_unsafe")


@pytest.mark.parametrize("ambiguous", [False, True])
def test_preflight_requires_one_remote_identity_not_one_historical_row(led, monkeypatch, ambiguous):
    from slack_testkit import SLACK
    req, original, *_ = preflight_recovery(led, monkeypatch, repeated=True)
    if ambiguous:
        with led.db:
            led.db.execute("UPDATE notification_render_parts SET remote_id='F_OTHER' WHERE delivery_id=? AND part_id=?",
                           (original["delivery_id"], req["part_id"]))
        reject_preflight(led, req, monkeypatch, "preflight_target_unverified")
    else:
        assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)["outcome"] == "applied"


def test_preflight_resealing_storage_failure_rolls_back_without_publishing(led, monkeypatch):
    import sqlite3
    from slack_testkit import SLACK
    req, *_ = preflight_recovery(led, monkeypatch)
    before = recovery_state(led)
    seal = notify_recovery._seal_preflight_update
    published = []

    def fail(*args):
        assert seal(*args) is None
        raise sqlite3.OperationalError("synthetic failure after resealing")

    monkeypatch.setattr(notify_recovery, "_seal_preflight_update", fail)
    monkeypatch.setattr(notify_spool, "publish_specs", lambda *args: published.extend(args[2]))
    with pytest.raises(sqlite3.OperationalError, match="synthetic failure after resealing"):
        notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2)
    assert recovery_state(led) == before and not published
    assert led.db.execute("SELECT 1 FROM command_receipts WHERE command_id=?", (req["command_id"],)).fetchone() is None


def test_existing_spec_contradiction_is_not_ignored(led, monkeypatch):
    req, *_ = preflight_recovery(led, monkeypatch, missing_spec=False)
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json='{}' WHERE delivery_id=?", (req["delivery_id"],))
    reject_preflight(led, req, monkeypatch, "preflight_target_unverified")


@pytest.mark.parametrize("transport_name", ["discord", "lineworks"])
def test_local_preflight_proof_never_applies_to_other_transports(led, monkeypatch, transport_name):
    req, *_ = preflight_recovery(led, monkeypatch)
    req["transport"] = transport_name
    if transport_name == "discord":
        req["guild_id"] = req.pop("team_id")
    assert notify_recovery.validate_card_part_resolve(req) == "bad_preflight_resolution"


@pytest.mark.parametrize("history", ["unclaimed", "claimed", "denied"])
def test_cancelled_unstarted_pending_does_not_block_preflight_recovery(led, monkeypatch, history):
    from notify_scope import stored_scope
    from slack_testkit import SLACK
    req, _, _, path, rows = preflight_recovery(led, monkeypatch, cancelled=True)
    cancelled = led.db.execute("SELECT * FROM notification_renders WHERE state='cancelled'").fetchone()
    identity = {"v": 1, "worker_id": "b" * 16, "ts": NOW,
                "attempt_id": "e" * 16, "delivery_id": cancelled["delivery_id"]}
    if history != "unclaimed":
        rows.append({**identity, "phase": "claimed"})
    if history == "denied":
        begin = {"version": 2, "op": "transport_begin", "command_id": _uuid(992),
                 "attempt_id": identity["attempt_id"], "worker_id": identity["worker_id"],
                 **stored_scope(cancelled), **{key: cancelled[key] for key in
                    ("delivery_id", "render_rev", "payload_hash", "route_epoch")}}
        denied = transport.apply_transport_begin(led, begin, SLACK, now=NOW + 2)
        assert not denied["granted"] and denied["error"] == "denied_render_cancelled"
        rows += [{**identity, "phase": "begin", "begin_envelope": begin},
                 {**identity, "phase": "denied", "error": denied["error"]}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = [dict(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?",
                                                (cancelled["delivery_id"],))]
    assert before and all(row["state"] == "pending" for row in before)
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "not_sent"
    assert [dict(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?",
                                              (cancelled["delivery_id"],))] == before
    assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                          (cancelled["delivery_id"],)).fetchone()[0] == "cancelled"


@pytest.mark.parametrize("evidence", ["unknown_part", "started_part", "granted_attempt", "unknown_attempt", "delivered_attempt",
                                      "journal_granted", "journal_started", "journal_result", "journal_receipt"])
def test_cancelled_render_with_send_evidence_still_blocks_preflight_recovery(led, monkeypatch, evidence):
    req, _, _, path, rows = preflight_recovery(led, monkeypatch, cancelled=True)
    cancelled = led.db.execute("SELECT delivery_id FROM notification_renders WHERE state='cancelled'").fetchone()[0]
    with led.db:
        if evidence == "unknown_part":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND kind='attachment_part'", (cancelled,))
        elif evidence == "started_part":
            led.db.execute("UPDATE notification_render_parts SET attempt_id='synthetic-started' WHERE delivery_id=? AND kind='attachment_part'", (cancelled,))
        elif evidence.endswith("_attempt"):
            state = evidence.removesuffix("_attempt")
            led.db.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,state,created_at) "
                           "VALUES(?,?,?,?,?)", ("e" * 16, cancelled, _uuid(992), state, NOW))
    if evidence.startswith("journal_"):
        rows.append({"v": 1, "worker_id": "b" * 16, "ts": NOW, "attempt_id": "e" * 16,
                     "delivery_id": cancelled, "phase": evidence.removeprefix("journal_"), "result": "unknown"})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    reject_preflight(led, req, monkeypatch, "preflight_target_unverified")


def cancelled_withdrawal(led, monkeypatch, *, stored_attempt=True, begins=2):
    from adapters.common import envelopes
    from notify_scope import stored_scope
    from slack_testkit import SLACK
    req, _, _, path, rows = preflight_recovery(led, monkeypatch, cancelled=True)
    cancelled = led.db.execute("SELECT * FROM notification_renders WHERE state='cancelled'").fetchone()
    # Envelope construction only needs the preserved identity, never the old caption.
    claim = {"attempt_id": "e" * 16, "worker_id": "b" * 16, "payload_hash": cancelled["payload_hash"],
             "spec": {"delivery_id": cancelled["delivery_id"], "render_rev": cancelled["render_rev"],
                      "delivery": {**stored_scope(cancelled), "route_epoch": cancelled["route_epoch"],
                                   "correlation": cancelled["correlation"]}}}
    begin = envelopes.transport_begin(claim)
    if stored_attempt:
        assert transport.apply_transport_begin(led, begin, SLACK, now=NOW + 2)["error"] == "denied_render_cancelled"
        assert transport.apply_transport_receipt(led, envelopes.transport_receipt(
            claim, "not_sent", error_code="spec_withdrawn"), SLACK, now=NOW + 2)["applied"]
    identity = {"v": 1, "ts": NOW, "worker_id": claim["worker_id"],
                "attempt_id": claim["attempt_id"], "delivery_id": cancelled["delivery_id"]}
    withdrawn = [{**identity, "phase": "claimed", "render_rev": cancelled["render_rev"], "op": cancelled["op"]}]
    withdrawn.extend({**identity, "phase": "begin", "command_id": begin["command_id"],
                      "begin_envelope": dict(begin), "receipt_envelope": envelopes.transport_receipt(claim, "unknown")}
                     for _ in range(begins))
    withdrawn.append({**identity, "phase": "receipt", "result": "not_sent", "error_code": "spec_withdrawn"})
    return req, path, rows, withdrawn


@pytest.mark.parametrize("stored_attempt", [False, True])
@pytest.mark.parametrize("begins", [1, 2])
def test_cancelled_pregrant_withdrawal_allows_only_exact_edit_recovery(led, monkeypatch, stored_attempt, begins):
    from slack_testkit import SLACK
    req, path, rows, withdrawn = cancelled_withdrawal(led, monkeypatch, stored_attempt=stored_attempt, begins=begins)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows + withdrawn))
    original_journal = path.read_bytes()
    cancelled = withdrawn[0]["delivery_id"]
    before = [dict(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?", (cancelled,))]
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 3)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "not_sent"
    assert path.read_bytes() == original_journal
    assert [dict(row) for row in led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=?", (cancelled,))] == before


@pytest.mark.parametrize("change", ["grant", "started", "result", "receipt_twice", "missing_claim", "missing_begin",
    "error", "unknown", "scope", "manifest", "changed_begin", "placeholder", "worker", "attempt", "db_command", "db_unknown", "db_split"])
def test_withdrawal_exception_rejects_any_drift_in_its_proof(led, monkeypatch, change):
    req, path, rows, withdrawn = cancelled_withdrawal(led, monkeypatch)
    if change in ("grant", "started", "result"):
        withdrawn.insert(-1, {**withdrawn[-1], "phase": "granted" if change == "grant" else change})
    elif change == "receipt_twice":
        withdrawn.append(dict(withdrawn[-1]))
    elif change == "missing_claim":
        withdrawn.pop(0)
    elif change == "missing_begin":
        withdrawn = [withdrawn[0], withdrawn[-1]]
    elif change == "error":
        withdrawn[-1]["error_code"] = "worker_restart"
    elif change == "unknown":
        withdrawn[-1]["result"] = "unknown"
    elif change == "scope":
        withdrawn[1]["begin_envelope"]["channel_id"] = "C_OTHER"
    elif change == "manifest":
        withdrawn[1]["begin_envelope"]["payload_hash"] = "c" * 64
    elif change == "changed_begin":
        withdrawn[2]["begin_envelope"]["command_id"] = _uuid(993)
        withdrawn[2]["command_id"] = _uuid(993)
    elif change == "placeholder":
        withdrawn[1]["receipt_envelope"]["result"] = "delivered"
        withdrawn[1]["receipt_envelope"]["message_id"] = "1790000009.000001"
    elif change == "worker":
        withdrawn[-1]["worker_id"] = "c" * 16
    elif change == "attempt":
        withdrawn[-1]["attempt_id"] = "f" * 16
    elif change == "db_split":
        with led.db:
            led.db.execute("UPDATE notification_delivery_attempts SET begin_command_id=? WHERE attempt_id=?",
                           (_uuid(993), withdrawn[0]["attempt_id"]))
            led.db.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,begin_command_id,state,worker_id,created_at) "
                           "VALUES(?,?,?,'not_sent',?,?)", ("f" * 16, withdrawn[0]["delivery_id"],
                           withdrawn[1]["command_id"], withdrawn[0]["worker_id"], NOW))
    else:
        column, value = ("begin_command_id", _uuid(993)) if change == "db_command" else ("state", "unknown")
        with led.db:
            led.db.execute("UPDATE notification_delivery_attempts SET " + column + "=? WHERE attempt_id=?",
                           (value, withdrawn[0]["attempt_id"]))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows + withdrawn))
    reject_preflight(led, req, monkeypatch, "preflight_target_unverified")
