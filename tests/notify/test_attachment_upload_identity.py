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
    witness = led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                             (original["delivery_id"],)).fetchone()[0]
    assert "attachment_identity" in json.loads(witness)
    assert "synthetic-original.bin" not in witness
    # Simulate data collected by the older GC, before identity witnesses existed.
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (original["delivery_id"],))
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


def _held_collected(led, uploaded):
    """The delivered upload loses its sealed manifest; only collected rows remain."""
    world, prior, _spec, part, attachment_id = uploaded
    did = prior["delivery_id"]
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?", (did,))
        led.db.execute("UPDATE attachments SET name='synthetic-attested.bin' WHERE attachment_id=?",
                       (attachment_id,))
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, [], force=True)
    latest = _latest_render(led)
    assert latest["delivery_id"] != did and latest["state"] == "held"
    held = led.db.execute(
        "SELECT part_id,state,error_code,attempt_id FROM notification_render_parts "
        "WHERE delivery_id=? AND kind='attachment_part'", (latest["delivery_id"],)).fetchone()
    assert held["state"] == "held" and held["error_code"] == "attachment_upload_identity_unverified"
    return world, prior, latest, held, part, attachment_id


def _confirm_req(plan, suffix):
    return {**plan, "command_id": f"c0ffee00-0000-4000-8000-{suffix:012d}",
            "actor": "operator", "reason": "synthetic attestation",
            "evidence": {"method": "remote_check", "ref": "synthetic", "worker_stopped": True},
            "human_confirmed": True}


def test_collected_only_upload_identity_is_held_then_confirmed_recovers(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    did, pid = prior["delivery_id"], part["part_id"]
    remote = led.db.execute("SELECT remote_id FROM notification_render_parts "
                            "WHERE delivery_id=? AND part_id=?", (did, pid)).fetchone()[0]
    # plan binds scope/card/part/attachment/route/history and the single collected remote
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    assert plan["cmd"] == "ops.attachment_identity_confirm" and plan["expected_state"] == "held"
    assert plan["remote_id"] == remote and plan["attachment_id"] == attachment_id
    assert plan["history_sha256"] and plan["route_epoch"] and plan["correlation"]
    # unapproved attempts reject: nothing is promoted, nothing is sent
    uploads, posts = len(world.client.upload_calls), len(world.client.thread_posts)
    for n, tamper in enumerate(("missing_human", "bad_remote", "bad_sha", "stale_plan"), 1):
        req = _confirm_req(plan, n)
        if tamper == "missing_human":
            del req["human_confirmed"]
        elif tamper == "bad_remote":
            req["remote_id"] = "F_OTHER"
        elif tamper == "bad_sha":
            req["sha256"] = "0" * 64
        else:
            req["bytes"] += 1
        assert apply_attachment_identity_confirm(led, req)["outcome"] == "rejected"
    receipt = apply_attachment_identity_confirm(led, _confirm_req(plan, 0xAA))
    assert receipt["outcome"] == "applied" and receipt["remote_id"] == remote
    # replay is idempotent and returns the identical stored receipt
    req = _confirm_req(plan, 0xAA)
    again = apply_attachment_identity_confirm(led, req)
    assert again == receipt or (again["outcome"] == "applied" and again["command_id"] == req["command_id"])
    # the held render is re-sealed to edit_only reuse of the attested remote
    sealed = _latest_render(led)
    assert sealed["delivery_id"] == latest["delivery_id"] and sealed["state"] == "queued"
    spec = json.loads(sealed["spec_json"])
    current = next(p for p in spec["parts"]["manifest"] if p["part_id"] == held["part_id"])
    assert current["edit_only"] is True and current["prior_remote_id"] == remote
    asyncio.run(_granted_card(world.worker, led, world.root))
    row = led.db.execute("SELECT state,remote_id FROM notification_render_parts "
                         "WHERE delivery_id=? AND part_id=?",
                         (sealed["delivery_id"], held["part_id"])).fetchone()
    assert row["state"] == "delivered" and row["remote_id"] == remote
    assert len(world.client.upload_calls) == uploads and len(world.client.thread_posts) == posts


def test_attestation_rejects_unsafe_and_changed_state(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    # stale attestation: attachment name changed after the plan was read
    with led.db:
        led.db.execute("UPDATE attachments SET name='renamed-after-plan.bin' WHERE attachment_id=?",
                       (attachment_id,))
    assert apply_attachment_identity_confirm(led, _confirm_req(plan, 0xBB))["outcome"] == "rejected"
    with led.db:
        led.db.execute("UPDATE attachments SET name='synthetic-attested.bin' WHERE attachment_id=?",
                       (attachment_id,))
    # a receipt bound to mismatched scope is rejected by the validator
    bad = _confirm_req(plan, 0xCC)
    bad["team_id"] = "T_OTHER"
    assert apply_attachment_identity_confirm(led, bad)["outcome"] == "rejected"
    # a granted attempt owning the card rejects the approval outright
    with led.db:
        led.db.execute(
            "INSERT INTO notification_delivery_attempts("
            "attempt_id,delivery_id,begin_command_id,state,error_code,worker_id,created_at,finished_at)"
            " VALUES('att-x',?,'c0ffee00-0000-4000-8000-000000000001','granted',NULL,'w1',?,NULL)",
            (latest["delivery_id"], NOW + 1))
    assert apply_attachment_identity_confirm(led, _confirm_req(plan, 0xDD))["outcome"] == "rejected"
    with led.db:
        led.db.execute("DELETE FROM notification_delivery_attempts WHERE attempt_id='att-x'")
    emitted = []
    with led.db:
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 3, emitted, force=True)
    assert _latest_render(led)["state"] == "held"


def test_source_change_after_plan_rejects_and_history_bound(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    req = _confirm_req(plan, 0xEE)
    # the card's source material changes between plan and apply → stale_source
    with led.db:
        led.db.execute("UPDATE messages SET body_text='tampered body',content_hash=? WHERE message_id=100",
                       ("f" * 64,))
    assert apply_attachment_identity_confirm(led, req)["outcome"] == "rejected"
    with led.db:
        led.db.execute("UPDATE messages SET body_text='updated source',content_hash=? WHERE message_id=100",
                       ("e" * 64,))
    plan2 = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    receipt = apply_attachment_identity_confirm(led, _confirm_req(plan2, 0xEF))
    assert receipt["outcome"] == "applied"
    # tampering with the stored payload_hash never promotes an identity
    with led.db:
        led.db.execute("UPDATE command_receipts SET payload_hash=? WHERE command_id=?",
                       ("0" * 64, _confirm_req(plan2, 0xEF)["command_id"]))
        led.db.execute("UPDATE attachments SET name='second-rename.bin' WHERE attachment_id=?",
                       (attachment_id,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 4, emitted, force=True)
    assert _latest_render(led)["state"] == "held"


def test_second_command_same_target_dedups_conflicting_rejects(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    first = apply_attachment_identity_confirm(led, _confirm_req(plan, 0xF1))
    assert first["outcome"] == "applied"
    # a second command_id for the same target sees the render already sealed:
    # rejecting the stale plan keeps the attestation without re-holding or
    # widening the approved identity
    second = apply_attachment_identity_confirm(led, _confirm_req(plan, 0xF2))
    assert second["outcome"] == "rejected" and second["error"] == "part_not_confirmable"
    # a conflicting remote for the same target is rejected at plan comparison
    conflicting = _confirm_req(plan, 0xF3)
    conflicting["remote_id"] = "F_CONFLICTING"
    assert apply_attachment_identity_confirm(led, conflicting)["outcome"] == "rejected"


def test_confirmed_then_second_rename_recovers_without_new_upload(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    did, pid = prior["delivery_id"], part["part_id"]
    remote = led.db.execute("SELECT remote_id FROM notification_render_parts "
                            "WHERE delivery_id=? AND part_id=?", (did, pid)).fetchone()[0]
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    assert apply_attachment_identity_confirm(led, _confirm_req(plan, 0xF4))["outcome"] == "applied"
    sealed = _latest_render(led)
    assert sealed["state"] == "queued"
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (sealed["delivery_id"], held["part_id"])).fetchone()[0] == "delivered"
    uploads = len(world.client.upload_calls)
    # a later rename recovers through the now-sealed delivered identity — no new
    # approval and no second upload of the same bytes
    with led.db:
        led.db.execute("UPDATE attachments SET name='renamed-again.bin' WHERE attachment_id=?",
                       (attachment_id,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 5, emitted, force=True)
    spec, = emitted
    current = next(p for p in spec["parts"]["manifest"] if p["part_id"] == held["part_id"])
    assert current["edit_only"] is True and current["prior_remote_id"] == remote
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 5)
    asyncio.run(_granted_card(world.worker, led, world.root))
    row = led.db.execute("SELECT state,remote_id FROM notification_render_parts "
                         "WHERE delivery_id=? AND part_id=?",
                         (spec["delivery_id"], held["part_id"])).fetchone()
    assert row["state"] == "delivered" and row["remote_id"] == remote
    assert len(world.client.upload_calls) == uploads


def test_approval_never_promotes_later_unapproved_collected_row(led, uploaded):
    world, prior, latest, held, part, attachment_id = _held_collected(led, uploaded)
    did, pid = prior["delivery_id"], part["part_id"]
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    plan = attachment_identity_confirm_plan(led.db, latest["delivery_id"], held["part_id"])
    assert apply_attachment_identity_confirm(led, _confirm_req(plan, 0xF5))["outcome"] == "applied"
    # A later unapproved collected row carrying the same identity tuple must not
    # be promoted by the earlier approval.
    new_did = "d0ffee00-0000-4000-8000-0000000000f6"
    old_part = led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (did, pid)).fetchone()
    old_render = dict(led.db.execute(
        "SELECT * FROM notification_renders WHERE delivery_id=?", (did,)).fetchone())
    with led.db:
        render = {**old_render, "delivery_id": new_did, "render_rev": prior["render_rev"] + 50,
                  "spec_json": None, "correlation": "ffffffff-0000-4000-8000-0000000000f6"}
        cols = ",".join(render)
        led.db.execute(f"INSERT INTO notification_renders({cols}) "
                       f"VALUES({','.join('?' * len(render))})", tuple(render.values()))
        for src in (old_part, dict(led.db.execute(
                "SELECT * FROM notification_render_parts WHERE delivery_id=? AND kind='thread'",
                (did,)).fetchone())):
            row = {**src, "delivery_id": new_did}
            cols = ",".join(row)
            led.db.execute(f"INSERT INTO notification_render_parts({cols}) "
                           f"VALUES({','.join('?' * len(row))})", tuple(row.values()))
    import notify_parts
    card = led.db.execute("SELECT * FROM notification_cards WHERE card_id=?", (latest["card_id"],)).fetchone()
    _body, _att, identities = notify_parts._same_thread_delivered_parts(led.db, card)
    later = next(proof for proof in next(iter(identities.values()))
                 if proof[0] == pid and proof[6] == prior["render_rev"] + 50)
    assert later[5] == "collected"


def test_two_attachments_sequential_approval_then_gc_continuity(led, tmp_path, monkeypatch):
    """Sequential per-part approvals; the last one unholds; GC of the sealed
    spec keeps receipts so recovery continues."""
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    aid1, _p1, _b1 = _attach(led, tmp_path, name="syn-one.bin")
    aid2, _p2, blob2 = _attach(led, tmp_path, name="syn-two.bin", blob=b"other-bytes")
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    prior = _latest_render(led)
    parts = {p["part_id"]: p for p in json.loads(prior["spec_json"])["parts"]["manifest"]
             if p["kind"] == "attachment_part"}
    assert len(parts) == 2
    # lose all sealed manifests → both attachment parts hold collected-only
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?",
                       (prior["delivery_id"],))
        led.db.execute("UPDATE attachments SET name='renamed-one.bin' WHERE attachment_id=?", (aid1,))
        led.db.execute("UPDATE attachments SET name='renamed-two.bin' WHERE attachment_id=?", (aid2,))
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, [], force=True)
    held_render = _latest_render(led)
    assert held_render["state"] == "held"
    held_parts = [r[0] for r in led.db.execute(
        "SELECT part_id FROM notification_render_parts WHERE delivery_id=? "
        "AND kind='attachment_part' AND state='held'", (held_render["delivery_id"],))]
    assert len(held_parts) == 2
    from notify_recovery import attachment_identity_confirm_plan, apply_attachment_identity_confirm
    # first approval: second part still unattested → render stays held
    plan1 = attachment_identity_confirm_plan(led.db, held_render["delivery_id"], held_parts[0])
    receipt1 = apply_attachment_identity_confirm(led, _confirm_req(plan1, 0xA1))
    assert receipt1["outcome"] == "applied" and receipt1["replan"] == "withheld"
    assert _latest_render(led)["state"] == "held"
    # second approval: every held attachment attested → re-sealed and queued
    plan2 = attachment_identity_confirm_plan(led.db, held_render["delivery_id"], held_parts[1])
    receipt2 = apply_attachment_identity_confirm(led, _confirm_req(plan2, 0xA2))
    assert receipt2["outcome"] == "applied" and receipt2["replan"] == "sealed"
    sealed = _latest_render(led)
    assert sealed["state"] == "queued"
    uploads = len(world.client.upload_calls)
    asyncio.run(_granted_card(world.worker, led, world.root))
    states = {r[0]: r[1] for r in led.db.execute(
        "SELECT part_id,state FROM notification_render_parts WHERE delivery_id=? "
        "AND kind='attachment_part'", (held_render["delivery_id"],))}
    assert set(states.values()) == {"delivered"}
    assert len(world.client.upload_calls) == uploads
    # GC clears sealed manifests; receipts still drive later recovery
    assert notify_cards.gc(led, SLACK, now=NOW + 100, limit=10)["spec_json_cleared"] >= 1
    remotes = {r[0]: r[1] for r in led.db.execute(
        "SELECT part_id,remote_id FROM notification_render_parts WHERE delivery_id=? "
        "AND kind='attachment_part'", (held_render["delivery_id"],))}
    uploads = len(world.client.upload_calls)
    with led.db:
        led.db.execute("UPDATE attachments SET name='post-gc.bin' WHERE attachment_id=?", (aid2,))
        emitted = []
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 101, emitted, force=True)
    spec, = emitted
    manifest = {p["part_id"]: p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part"}
    assert all(p["edit_only"] and p["prior_remote_id"] == remotes[pid]
               for pid, p in manifest.items())
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), emitted, NOW + 101)
    asyncio.run(_granted_card(world.worker, led, world.root))
    rows = {r[0]: (r[1], r[2]) for r in led.db.execute(
        "SELECT part_id,state,remote_id FROM notification_render_parts "
        "WHERE delivery_id=? AND kind='attachment_part'", (spec["delivery_id"],))}
    assert all(state == "delivered" and remote == remotes[pid]
               for pid, (state, remote) in rows.items())
    assert len(world.client.upload_calls) == uploads


