"""Synthetic sealed upload identity is required before a caption rename."""
import asyncio
import hashlib
import json

import pytest

import notify_cards
import notify_scope
import notify_spool
from mcs_util import canonical, payload_hash
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import SLACK, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]
pytestmark = pytest.mark.thread_bodies


@pytest.fixture
def uploaded(led, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    attachment_id, path, blob = _attach(led, tmp_path, name="synthetic-original.bin")
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    row = led.db.execute(
        "SELECT state,remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (render["delivery_id"], part["part_id"])).fetchone()
    assert row["state"] == "delivered" and row["remote_id"]
    new_path = path.with_name("synthetic-renamed.bin")
    new_path.write_bytes(blob)
    with led.db:
        led.db.execute("UPDATE attachments SET name=?,local_path=? WHERE attachment_id=?",
                       (new_path.name, str(new_path), attachment_id))
    return world, render, spec, part, attachment_id


@pytest.fixture
def collected_with_edit_witness(led, uploaded):
    world, original, _spec, part, attachment_id = uploaded
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    edit, = emitted
    edit_part = next(p for p in edit["parts"]["manifest"] if p["part_id"] == part["part_id"])
    assert edit_part["edit_only"] is True and edit_part["prior_remote_id"]
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    row = led.db.execute("SELECT state,remote_id FROM notification_render_parts "
                         "WHERE delivery_id=? AND part_id=?", (edit["delivery_id"], part["part_id"])).fetchone()
    assert row["state"] == "delivered" and row["remote_id"] == edit_part["prior_remote_id"]
    with led.db:
        led.db.execute("UPDATE notification_renders SET updated_at=? WHERE delivery_id=?",
                       (NOW - 100, original["delivery_id"]))
        led.db.execute("UPDATE notification_renders SET updated_at=? WHERE delivery_id=?",
                       (NOW - 10, edit["delivery_id"]))
    assert notify_cards.gc(led, SLACK, now=NOW + 3, limit=1)["spec_json_cleared"] == 1
    assert led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                          (original["delivery_id"],)).fetchone()[0] is None
    assert led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                          (edit["delivery_id"],)).fetchone()[0] is not None
    with led.db:
        led.db.execute("UPDATE attachments SET name='synthetic-third.bin' WHERE attachment_id=?",
                       (attachment_id,))
    return world, original, edit, part, attachment_id


def test_matching_body_coverage_witness_and_later_edit_allow_same_file_edit(
        led, collected_with_edit_witness):
    world, original, _edit, part, _attachment_id = collected_with_edit_witness
    proof = [hashlib.sha256(f"synthetic-proof-{n}".encode()).hexdigest() for n in range(3)]
    witness = {"body_delivery_coverage": {"version": 1,
               "payload_hash": original["payload_hash"], "proofs": [proof]}}
    raw = canonical(witness).decode()
    assert notify_scope._body_witness_proofs(raw, original) == {tuple(proof)}
    uploads, posts = len(world.client.upload_calls), len(world.client.thread_posts)
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                       (raw, original["delivery_id"]))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 4, emitted, force=True)
    spec, = emitted
    current = next(p for p in spec["parts"]["manifest"] if p["part_id"] == part["part_id"])
    assert current["edit_only"] is True and current["prior_remote_id"]
    assert current["name"] == "synthetic-original.bin" and "synthetic-third.bin" in current["caption"]
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 4)
    asyncio.run(_granted_card(world.worker, led, world.root))
    row = led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                         (spec["delivery_id"], part["part_id"])).fetchone()
    assert row["state"] == "delivered"
    assert len(world.client.upload_calls) == uploads and len(world.client.thread_posts) == posts


@pytest.mark.parametrize("fault", [
    "all_specs_missing", "no_successor_part", "successor_not_edit", "successor_wrong_prior",
    "malformed_original", "fake_witness", "malformed_witness",
    "part_id", "attachment_id", "remote_id", "hash", "bytes", "name", "part_error", "scope", "thread",
    "unknown", "pending_sending", "pending_delivered",
])
def test_collected_upload_with_insufficient_or_conflicting_evidence_is_held(
        led, collected_with_edit_witness, fault):
    world, original, edit, part, attachment_id = collected_with_edit_witness
    did, pid = original["delivery_id"], part["part_id"]
    uploads, posts = len(world.client.upload_calls), len(world.client.thread_posts)
    with led.db:
        if fault == "all_specs_missing":
            led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?",
                           (edit["delivery_id"],))
        elif fault == "no_successor_part":
            led.db.execute("UPDATE notification_render_parts SET state='not_sent',remote_id=NULL "
                           "WHERE delivery_id=? AND part_id=?", (edit["delivery_id"], pid))
        elif fault in ("successor_not_edit", "successor_wrong_prior"):
            successor = json.loads(led.db.execute(
                "SELECT spec_json FROM notification_renders WHERE delivery_id=?", (edit["delivery_id"],)
            ).fetchone()[0])
            successor_part = next(p for p in successor["parts"]["manifest"] if p["part_id"] == pid)
            if fault == "successor_not_edit":
                successor_part.pop("edit_only")
            else:
                successor_part["prior_remote_id"] = "F_OTHER"
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (canonical(successor).decode(), payload_hash(successor), edit["delivery_id"]))
        elif fault == "malformed_original":
            led.db.execute("UPDATE notification_renders SET spec_json='{' WHERE delivery_id=?", (did,))
        elif fault in ("fake_witness", "malformed_witness"):
            witness = {"body_delivery_coverage": {"version": 1,
                       "payload_hash": "0" * 64 if fault == "fake_witness" else original["payload_hash"],
                       "proofs": [["a" * 64, "b" * 64, "c" * 64]] if fault == "fake_witness" else []}}
            led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                           (canonical(witness).decode(), did))
        elif fault == "part_id":
            led.db.execute("UPDATE notification_render_parts SET part_id='attach:9999' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "attachment_id":
            led.db.execute("UPDATE notification_render_parts SET attachment_id=? "
                           "WHERE delivery_id=? AND part_id=?", (attachment_id + 1, did, pid))
        elif fault == "remote_id":
            led.db.execute("UPDATE notification_render_parts SET remote_id='F_OTHER' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "hash":
            led.db.execute("UPDATE notification_render_parts SET payload_sha256=? "
                           "WHERE delivery_id=? AND part_id=?", ("f" * 64, did, pid))
        elif fault == "bytes":
            led.db.execute("UPDATE notification_render_parts SET bytes=bytes+1 "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "name":
            led.db.execute("UPDATE notification_render_parts SET name='different.bin' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "part_error":
            led.db.execute("UPDATE notification_render_parts SET error_code='synthetic_error' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "scope":
            led.db.execute("UPDATE notification_renders SET team_id='T_OTHER' WHERE delivery_id=?", (did,))
        elif fault == "thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='OTHER_THREAD' "
                           "WHERE delivery_id=? AND kind='thread'", (did,))
        else:
            state = "unknown" if fault == "unknown" else "pending"
            led.db.execute("UPDATE notification_render_parts SET state=?,remote_id=NULL "
                           "WHERE delivery_id=? AND part_id=?", (state, did, pid))
            if fault == "pending_sending":
                led.db.execute("UPDATE notification_renders SET state='sending' WHERE delivery_id=?", (did,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 4, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["delivery_id"] != edit["delivery_id"] and latest["state"] == "held"
    errors = {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))}
    assert errors == {"attachment_upload_identity_unverified"}
    assert len(world.client.upload_calls) == uploads and len(world.client.thread_posts) == posts


@pytest.fixture
def unavailable(led, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    attachment_id, path, blob = _attach(led, tmp_path, name="synthetic-recovered.bin", state="failed")
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    prior = _latest_render(led)
    spec = json.loads(prior["spec_json"])
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    receipt = led.db.execute(
        "SELECT state,remote_id,payload_sha256,bytes FROM notification_render_parts "
        "WHERE delivery_id=? AND part_id=?", (prior["delivery_id"], part["part_id"])).fetchone()
    assert part["unavailable"] is True and part["caption"]
    assert receipt["state"] == "delivered" and receipt["remote_id"]
    assert receipt["payload_sha256"] is None and receipt["bytes"] is None
    assert world.client.upload_calls == []
    return world, prior, part, attachment_id, path, blob


def test_unavailable_caption_then_download_allows_only_first_upload(led, unavailable):
    world, _prior, old_part, attachment_id, path, blob = unavailable
    led.attachment_saved(attachment_id, str(path), len(blob), hashlib.sha256(blob).hexdigest())
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    spec, = emitted
    part = next(p for p in spec["parts"]["manifest"] if p["part_id"] == old_part["part_id"])
    assert not part.get("unavailable") and not part.get("prior_remote_id") and not part.get("edit_only")
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == 1
    assert led.db.execute(
        "SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (spec["delivery_id"], part["part_id"])).fetchone()[0] == "delivered"
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 2, emitted, force=True)
    assert len(emitted) == 1
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 2)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == 1


def _append_unavailable_history(led, original):
    """Append a synthetic delivered caption plan after the current render."""
    current = _latest_render(led)
    assert current["state"] == "delivered"
    previous_parts = led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?", (original["delivery_id"],)).fetchall()
    synthetic_id = "00000000-0000-4000-8000-000000000999"
    prior_spec = json.loads(original["spec_json"])
    prior_spec.update(delivery_id=synthetic_id, render_rev=current["render_rev"] + 1, op="update")
    card = led.db.execute("SELECT message_id,thread_id FROM notification_cards WHERE card_id=1").fetchone()
    prior_spec["delivery"].update(message_id=card["message_id"], thread_id=card["thread_id"],
                                  correlation="9" * 32)
    render = dict(original)
    render.update(delivery_id=synthetic_id, render_rev=prior_spec["render_rev"], op="update",
                  correlation=prior_spec["delivery"]["correlation"],
                  spec_json=canonical(prior_spec).decode(), payload_hash=payload_hash(prior_spec))
    with led.db:
        columns = list(render)
        led.db.execute("INSERT INTO notification_renders(" + ",".join(columns) + ") VALUES("
                       + ",".join("?" for _ in columns) + ")", [render[k] for k in columns])
        for row in previous_parts:
            copied = dict(row)
            copied["delivery_id"] = synthetic_id
            columns = list(copied)
            led.db.execute("INSERT INTO notification_render_parts(" + ",".join(columns) + ") VALUES("
                           + ",".join("?" for _ in columns) + ")", [copied[k] for k in columns])
    return synthetic_id


def test_unavailable_caption_cannot_hide_earlier_delivered_file(led, unavailable):
    world, original, _old_part, attachment_id, path, blob = unavailable
    led.attachment_saved(attachment_id, str(path), len(blob), hashlib.sha256(blob).hexdigest())
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == 1
    synthetic_id = _append_unavailable_history(led, original)
    with led.db:
        led.db.execute("UPDATE attachments SET name='synthetic-third.bin' WHERE attachment_id=?",
                       (attachment_id,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 2, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["delivery_id"] != synthetic_id and latest["state"] == "held"
    assert {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))} \
        == {"attachment_upload_identity_unverified"}
    assert len(world.client.upload_calls) == 1


@pytest.mark.parametrize("fault", ["unknown_same_part", "unknown_same_attachment", "delivered_no_remote"])
def test_missing_prior_receipt_blocks_new_upload_even_if_later_caption_delivered(led, unavailable, fault):
    world, original, part, attachment_id, path, blob = unavailable
    synthetic_id = _append_unavailable_history(led, original)
    with led.db:
        if fault == "unknown_same_attachment":
            led.db.execute("UPDATE notification_render_parts SET part_id='attach:9999',state='unknown',remote_id=NULL "
                           "WHERE delivery_id=? AND part_id=?", (original["delivery_id"], part["part_id"]))
        else:
            state = "delivered" if fault == "delivered_no_remote" else "unknown"
            led.db.execute("UPDATE notification_render_parts SET state=?,remote_id=NULL "
                           "WHERE delivery_id=? AND part_id=?", (state, original["delivery_id"], part["part_id"]))
    led.attachment_saved(attachment_id, str(path), len(blob), hashlib.sha256(blob).hexdigest())
    uploads, posts = len(world.client.upload_calls), len(world.client.thread_posts)
    with led.db:
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 2, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["delivery_id"] != synthetic_id and latest["state"] == "held"
    assert {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))} \
        == {"attachment_upload_identity_unverified"}
    assert len(world.client.upload_calls) == uploads and len(world.client.thread_posts) == posts


@pytest.mark.parametrize("fault", ["scope", "thread", "missing_spec", "payload_hash"])
def test_unverified_unavailable_caption_cannot_authorize_first_upload(led, unavailable, fault):
    world, prior, _part, attachment_id, path, blob = unavailable
    led.attachment_saved(attachment_id, str(path), len(blob), hashlib.sha256(blob).hexdigest())
    did = prior["delivery_id"]
    with led.db:
        if fault == "scope":
            led.db.execute("UPDATE notification_renders SET team_id='T_OTHER' WHERE delivery_id=?", (did,))
        elif fault == "thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='OTHER_THREAD' "
                           "WHERE delivery_id=? AND kind='thread'", (did,))
        elif fault == "missing_spec":
            led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (did,))
        else:
            led.db.execute("UPDATE notification_renders SET payload_hash=? WHERE delivery_id=?", ("0" * 64, did))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["state"] == "held"
    assert {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))} \
        == {"attachment_upload_identity_unverified"}
    assert world.client.upload_calls == []


@pytest.mark.parametrize("fault", [
    "scope", "thread", "part_id", "attachment_id", "hash", "bytes", "name",
    "missing_spec", "malformed_spec", "payload_hash", "duplicate_manifest", "unavailable_manifest",
    "bool_render_rev", "bool_route_epoch", "card_key", "kind", "logical_intent_id",
])
def test_unverified_upload_identity_holds_render_without_writes(led, uploaded, fault):
    world, prior, old_spec, part, attachment_id = uploaded
    did, pid = prior["delivery_id"], part["part_id"]
    before_uploads, before_posts = len(world.client.upload_calls), len(world.client.thread_posts)
    with led.db:
        if fault == "scope":
            led.db.execute("UPDATE notification_renders SET team_id='T_OTHER' WHERE delivery_id=?", (did,))
        elif fault == "thread":
            led.db.execute("UPDATE notification_render_parts SET remote_id='OTHER_THREAD' "
                           "WHERE delivery_id=? AND kind='thread'", (did,))
        elif fault == "part_id":
            led.db.execute("UPDATE notification_render_parts SET part_id='attach:9999' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "attachment_id":
            led.db.execute("UPDATE notification_render_parts SET attachment_id=? "
                           "WHERE delivery_id=? AND part_id=?", (attachment_id + 1, did, pid))
        elif fault == "hash":
            led.db.execute("UPDATE notification_render_parts SET payload_sha256=? "
                           "WHERE delivery_id=? AND part_id=?", ("f" * 64, did, pid))
        elif fault == "bytes":
            led.db.execute("UPDATE notification_render_parts SET bytes=bytes+1 "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "name":
            led.db.execute("UPDATE notification_render_parts SET name='different.bin' "
                           "WHERE delivery_id=? AND part_id=?", (did, pid))
        elif fault == "missing_spec":
            led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (did,))
        elif fault == "malformed_spec":
            led.db.execute("UPDATE notification_renders SET spec_json='{' WHERE delivery_id=?", (did,))
        elif fault == "payload_hash":
            led.db.execute("UPDATE notification_renders SET payload_hash=? WHERE delivery_id=?", ("0" * 64, did))
        elif fault in ("duplicate_manifest", "unavailable_manifest"):
            if fault == "duplicate_manifest":
                old_spec["parts"]["manifest"].append(dict(part))
            else:
                next(p for p in old_spec["parts"]["manifest"] if p["part_id"] == pid)["unavailable"] = True
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (canonical(old_spec).decode(), payload_hash(old_spec), did))
        else:
            if fault == "bool_render_rev":
                old_spec["render_rev"] = True
            elif fault == "bool_route_epoch":
                old_spec["delivery"]["route_epoch"] = True
            elif fault == "kind":
                old_spec["kind"] = "signal"
            else:
                old_spec[fault] = "synthetic-other-card"
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (canonical(old_spec).decode(), payload_hash(old_spec), did))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["delivery_id"] != did and latest["state"] == "held"
    errors = {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))}
    assert errors == {"attachment_upload_identity_unverified"}
    assert len(world.client.upload_calls) == before_uploads and len(world.client.thread_posts) == before_posts


def test_unknown_prior_remote_never_reuploads_or_posts(led, uploaded):
    world, prior, _old_spec, part, _attachment_id = uploaded
    did, pid = prior["delivery_id"], part["part_id"]
    before_uploads, before_posts = len(world.client.upload_calls), len(world.client.thread_posts)
    emitted = []
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET remote_id='F_OTHER' "
                       "WHERE delivery_id=? AND part_id=?", (did, pid))
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    if not emitted:
        latest = _latest_render(led)
        assert latest["state"] == "held"
    else:
        spec, = emitted
        current = next(p for p in spec["parts"]["manifest"] if p["part_id"] == pid)
        assert current["prior_remote_id"] == "F_OTHER" and current["edit_only"] is True
        notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 1)
        asyncio.run(_granted_card(world.worker, led, world.root))
        part_row = led.db.execute(
            "SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
            (spec["delivery_id"], pid)).fetchone()
        assert part_row["state"] == "unknown"
    assert len(world.client.upload_calls) == before_uploads and len(world.client.thread_posts) == before_posts


@pytest.mark.parametrize("fault", ["remote_id", "missing_thread", "missing_message", "other_message"])
def test_delivered_edit_with_conflicting_sealed_identity_is_held(led, uploaded, fault):
    world, _prior, _old_spec, part, attachment_id = uploaded
    pid = part["part_id"]
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, emitted, force=True)
    edit, = emitted
    sealed_part = next(p for p in edit["parts"]["manifest"] if p["part_id"] == pid)
    assert sealed_part["edit_only"] is True and sealed_part["prior_remote_id"]
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert led.db.execute(
        "SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (edit["delivery_id"], pid)).fetchone()[0] == "delivered"
    uploads, posts = len(world.client.upload_calls), len(world.client.thread_posts)
    with led.db:
        if fault == "remote_id":
            led.db.execute("UPDATE notification_render_parts SET remote_id='F_OTHER' "
                           "WHERE delivery_id=? AND part_id=?", (edit["delivery_id"], pid))
        else:
            if fault == "missing_thread":
                edit["delivery"].pop("thread_id")
            elif fault == "missing_message":
                edit["delivery"].pop("message_id")
            else:
                edit["delivery"]["message_id"] = "synthetic-other-message"
            led.db.execute("UPDATE notification_renders SET spec_json=?,payload_hash=? WHERE delivery_id=?",
                           (canonical(edit).decode(), payload_hash(edit), edit["delivery_id"]))
        led.db.execute("UPDATE attachments SET name='synthetic-again.bin' WHERE attachment_id=?",
                       (attachment_id,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 2, emitted, force=True)
    assert emitted == []
    latest = _latest_render(led)
    assert latest["state"] == "held"
    assert {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?", (latest["delivery_id"],))} \
        == {"attachment_upload_identity_unverified"}
    assert len(world.client.upload_calls) == uploads and len(world.client.thread_posts) == posts
