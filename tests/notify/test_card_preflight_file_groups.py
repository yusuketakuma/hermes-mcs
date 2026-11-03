"""Synthetic distinction between private preparation and shared file receipts."""
import sqlite3

import pytest

from notify_recovery import _preflight_shared_file
from notify_testkit import led as led


@pytest.mark.parametrize("fault", [None, "missing", "unknown", "wrong_remote", "wrong_rev", "error", "other_card"])
def test_private_stage_requires_same_render_shared_group_receipt(fault):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE notification_renders(delivery_id TEXT,card_id INTEGER,render_rev INTEGER,state TEXT);
            CREATE TABLE notification_render_parts(delivery_id TEXT,kind TEXT,part_id TEXT,state TEXT,
                                                   error_code TEXT,remote_id TEXT);
        """)
        db.execute("INSERT INTO notification_renders VALUES('synthetic',?,?, 'delivered')",
                   (2 if fault == "other_card" else 1, 8 if fault == "wrong_rev" else 7))
        if fault != "missing":
            db.execute("INSERT INTO notification_render_parts VALUES('synthetic','file_group','file_group',?,?,?)",
                       ("unknown" if fault == "unknown" else "delivered",
                        "synthetic_error" if fault == "error" else None,
                        "other-post" if fault == "wrong_remote" else "synthetic-post"))
        card = {"card_id": 1, "message_id": "synthetic-post"}
        part = {"part_id": "attachment-1", "attachment_id": 1, "sha256": "a" * 64,
                "bytes": 12, "name": "synthetic.pdf"}
        proof = ("attachment-1", 1, "a" * 64, 12, "synthetic.pdf", "private_stage", 7, False, True)
        assert _preflight_shared_file(db, card, part, "F_SYNTHETIC", {"F_SYNTHETIC": [proof]}) is (fault is None)
    finally:
        db.close()


@pytest.mark.parametrize("origin", ["file", "collected", "operator_edit", "operator_verified", "unavailable", None])
def test_shared_file_recovery_preserves_public_identity_classes_and_exact_bytes(origin):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE notification_renders(delivery_id TEXT,card_id INTEGER,render_rev INTEGER,state TEXT);
            CREATE TABLE notification_render_parts(delivery_id TEXT,kind TEXT,part_id TEXT,state TEXT,
                                                   error_code TEXT,remote_id TEXT);
        """)
        card = {"card_id": 1, "message_id": "synthetic-post"}
        part = {"part_id": "attachment-1", "attachment_id": 1, "sha256": "a" * 64,
                "bytes": 12, "name": "synthetic.pdf"}
        proof = ("attachment-1", 1, "a" * 64, 12, "synthetic.pdf", origin, 7, False, True)
        identities = {"F_SYNTHETIC": [proof] if origin is not None else [None]}
        expected = origin in ("file", "collected", "operator_edit", "operator_verified")
        assert _preflight_shared_file(db, card, part, "F_SYNTHETIC", identities) is expected
        assert not _preflight_shared_file(db, card, {**part, "sha256": "b" * 64}, "F_SYNTHETIC", identities)
        assert not _preflight_shared_file(db, card, part, "F_OTHER", identities)
        db.execute("INSERT INTO notification_renders VALUES('private',1,7,'delivered')")
        db.execute("INSERT INTO notification_render_parts VALUES('private','file_group','file_group','unknown',NULL,NULL)")
        assert not _preflight_shared_file(db, card, part, "F_SYNTHETIC", identities)
    finally:
        db.close()


def group_unknown(led):
    import json
    import notify_transport
    from adapters.common import envelopes
    from notify_testkit import NOW, _attach, _dispatch, _intent, _latest_render, _seed_thread, _uuid
    from slack_testkit import SLACK, ROOT_TS
    import notify_recovery

    _seed_thread(led, mids=(100,))
    first = _attach(led, 100, name="synthetic-one.pdf")
    with led.db:
        led.db.execute("UPDATE attachments SET file_id='synthetic-first-file' WHERE attachment_id=?", (first,))
    _attach(led, 100, name="synthetic-two.pdf")
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    claim = {"spec": spec, "attempt_id": "c" * 16, "worker_id": "b" * 16,
             "payload_hash": render["payload_hash"]}
    assert notify_transport.apply_transport_begin(led, envelopes.transport_begin(claim), SLACK, now=NOW)["granted"]
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id=ROOT_TS), SLACK, now=NOW)["applied"]
    for part in spec["parts"]["manifest"][1:]:
        group = part["kind"] == "file_group"
        remote = (ROOT_TS if part["kind"] == "thread" else
                  "F_SYNTHETIC_" + str(part["attachment_id"]) if part["kind"] == "attachment_part" else
                  f"1790000001.{part['index']:06d}")
        assert notify_transport.apply_part_receipt(led, envelopes.part_receipt(
            claim, part, "unknown" if group else "delivered", remote_id=None if group else remote,
            error_code="timeouterror" if group else None), SLACK, now=NOW)["applied"]
    plan = notify_recovery.card_part_resolve_plan(led, render["delivery_id"], "file_group")
    req = {**plan, "command_id": _uuid(930), "actor": "synthetic-operator", "human_confirmed": True,
           "reason": "合成の共有確認", "result": "mark_delivered", "remote_id": ROOT_TS,
           "evidence": {"method": "synthetic_lookup", "ref": "synthetic-group-proof", "worker_stopped": True}}
    return req, render, spec


def test_group_unknown_is_human_resolvable_and_receipt_replay_has_no_new_publication(led, monkeypatch):
    import notify_recovery
    import notify_spool
    from notify_testkit import NOW
    from slack_testkit import SLACK

    req, render, _ = group_unknown(led)
    assert notify_recovery.validate_card_part_resolve(req) is None
    guidance = notify_recovery.recovery_guidance("file_group", "slack", "timeouterror", req["attempt_id"])
    assert guidance["available"] and guidance["automatic_resend"] is False
    assert guidance["requirements"]["mark_delivered"]["remote_identity"] == "card_message_id_and_exact_file_set"
    published = []
    monkeypatch.setattr(notify_spool, "publish_specs", lambda *args: published.extend(args[2]))
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "delivered", receipt.get("error")
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2) == receipt
    assert not published
    row = led.db.execute("SELECT state,remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id='file_group'",
                         (render["delivery_id"],)).fetchone()
    assert row[0] == "delivered" and row[1] == req["remote_id"]
    assert notify_recovery._human_delivered_part(led.db, req["delivery_id"], "file_group")


@pytest.mark.parametrize("fault", ["wrong_card_remote", "stage_missing", "stage_unknown", "stage_duplicate_fid",
                                   "stage_hash", "stage_order", "stage_set"])
def test_group_resolution_requires_exact_sealed_order_complete_stages_and_card_remote(led, monkeypatch, fault):
    import copy
    import mcs_requests
    import notify_recovery
    from notify_testkit import NOW
    from slack_testkit import SLACK

    req, _, spec = group_unknown(led)
    stages = [part for part in spec["parts"]["manifest"] if part["kind"] == "attachment_part"]
    with led.db:
        if fault == "wrong_card_remote":
            req["remote_id"] = "1790000099.000001"
        elif fault == "stage_missing":
            led.db.execute("DELETE FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                           (req["delivery_id"], stages[0]["part_id"]))
        elif fault == "stage_unknown":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=? AND part_id=?",
                           (req["delivery_id"], stages[0]["part_id"]))
        elif fault == "stage_duplicate_fid":
            led.db.execute("UPDATE notification_render_parts SET remote_id='F_DUPLICATE' WHERE delivery_id=? AND kind='attachment_part'",
                           (req["delivery_id"],))
        elif fault == "stage_hash":
            led.db.execute("UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id=?",
                           ("b" * 64, req["delivery_id"], stages[0]["part_id"]))
        else:
            tampered = copy.deepcopy(spec)
            manifest = tampered["parts"]["manifest"]
            if fault == "stage_order":
                indexes = [i for i, part in enumerate(manifest) if part["kind"] == "attachment_part"]
                manifest[indexes[0]], manifest[indexes[1]] = manifest[indexes[1]], manifest[indexes[0]]
            else:
                manifest[:] = [part for part in manifest if part["part_id"] != stages[0]["part_id"]]
            digest = mcs_requests.payload_hash(tampered)
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (mcs_requests.canonical(tampered).decode(), digest, req["delivery_id"]))
            req["manifest_sha256"] = digest
    before = [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts ORDER BY delivery_id,idx")]
    result = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert result["outcome"] == "rejected"
    assert result["error"] in ("file_group_identity_unverified", "file_group_dependencies_unverified")
    assert [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts ORDER BY delivery_id,idx")] == before


def test_group_retry_needs_explicit_not_sent_proof_and_reuses_every_private_fid(led, monkeypatch):
    import notify_recovery
    import notify_spool
    from notify_testkit import NOW
    from slack_testkit import SLACK

    req, _, _ = group_unknown(led)
    old_files = {row[0]: row[1] for row in led.db.execute(
        "SELECT part_id,remote_id FROM notification_render_parts WHERE delivery_id=? AND kind='attachment_part'",
        (req["delivery_id"],))}
    published = []
    monkeypatch.setattr(notify_spool, "publish_specs", lambda *args: published.extend(args[2]))
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "no_journal_started"})
    req.pop("remote_id")
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "applied" and receipt["part_state"] == "not_sent", receipt.get("error")
    assert len(published) == 1
    stages = [part for part in published[0]["parts"]["manifest"] if part["kind"] == "attachment_part"]
    assert {part["part_id"]: part.get("prior_remote_id") for part in stages} == old_files
    assert sum(part["kind"] == "file_group" for part in published[0]["parts"]["manifest"]) == 1
    assert notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 2) == receipt
    assert len(published) == 1


def test_group_retry_cannot_include_a_new_upload_outside_the_approved_file_set(led, monkeypatch):
    import hashlib
    from pathlib import Path
    import notify_recovery
    import notify_spool
    from notify_testkit import NOW, _attach
    from slack_testkit import SLACK

    req, _, _ = group_unknown(led)
    source = Path(notify_spool.data_root(led))/"attachments"/"synthetic-new-upload"
    source.parent.mkdir(exist_ok=True)
    content = b"Completely synthetic newly added attachment"
    source.write_bytes(content)
    with led.db:
        led.db.execute("UPDATE attachments SET file_id='synthetic-existing-' || attachment_id WHERE message_id=100")
    _attach(led, 100, name="synthetic-new.pdf", local_path=str(source),
            sha256=hashlib.sha256(content).hexdigest(), nbytes=len(content))
    published = []
    monkeypatch.setattr(notify_spool, "publish_specs", lambda *args: published.extend(args[2]))
    req.update(result="mark_not_sent", evidence={**req["evidence"], "proof": "no_journal_started"})
    req.pop("remote_id")
    before = [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts ORDER BY delivery_id,idx")]
    render_count = led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0]
    receipt = notify_recovery.apply_card_part_resolve(led, req, SLACK, NOW + 1)
    assert receipt["outcome"] == "rejected" and receipt["error"] == "file_group_retry_scope_changed"
    assert not published
    assert [tuple(row) for row in led.db.execute("SELECT * FROM notification_render_parts ORDER BY delivery_id,idx")] == before
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == render_count
