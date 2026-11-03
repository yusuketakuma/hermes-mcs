"""Only files actually covered by accepted cards reach the attachment outbox."""
import hashlib
import json

import pytest

import ledger
import notify_cards
import mcs_util
import notify_parts
import notify_sender
import notify_spool
from notify_testkit import (
    NOW, _dispatch, _intent, _latest_render, _msg, _seed_thread,
)
from slack_testkit import SLACK
from common_testkit import ledger_fixture


led = ledger_fixture(data_dir=True)


@pytest.mark.parametrize("download_after", [False, True])
def test_card_attachment_scope_uses_frozen_delivered_coverage(
        led, tmp_path, monkeypatch, download_after):
    _seed_thread(led, mids=(100,))
    _msg(led, 200)
    _msg(led, 300)
    event = _intent(led, payload={"message_ids": [100, 200]})
    _dispatch(led, event)
    with led.db:
        led.db.execute(
            "UPDATE notification_intent_cards SET state=CASE "
            "WHEN card_id=(SELECT card_id FROM notification_cards "
            "WHERE root_message_id=100) THEN 'delivered' ELSE 'suppressed' END")
        # The accepted intent was sealed before this mutable row changed.
        led.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?",
                       (json.dumps({"message_ids": [300]}), event["event_id"]))
        for mid in (100, 200, 300):
            led.db.execute(
                "INSERT INTO attachments(message_id,file_id,state,local_path) "
                "VALUES(?,?,?,?)", (mid, f"file-{mid}",
                                   "pending" if download_after else "downloaded",
                                   str(tmp_path / f"synthetic-{mid}.txt")))
        assert notify_cards.complete_intent(led.db, event["event_id"], NOW) == "accepted"
    if download_after:
        monkeypatch.setattr(ledger.time, "time", lambda: NOW + 1)
        for row in led.db.execute("SELECT * FROM attachments").fetchall():
            led.attachment_saved(row["attachment_id"], row["local_path"], 1, "synthetic")
    rows = led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='attachment_followup'").fetchall()
    assert [json.loads(row["payload"])["message_id"] for row in rows] == [100]


def test_slack_manifest_attachment_suppresses_text_followup(led, tmp_path):
    """A file sealed into the slack card's durable part manifest is
    owned by the part pipeline — the legacy text follow-up must not
    send the same file a second time (T10)."""
    _seed_thread(led, mids=(100,))
    blob = b"synthetic-slack-file"
    f = tmp_path / "syn.bin"
    f.write_bytes(blob)
    cur = led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "bytes,sha256,state) VALUES(?,?,?,?,?,?,'downloaded')",
        (100, "file-syn", "syn.bin", str(f), len(blob),
         hashlib.sha256(blob).hexdigest()))
    aid = cur.lastrowid
    event = _intent(led, payload={"message_ids": [100]})
    assert _dispatch(led, event, SLACK)["dispatched"]
    render = _latest_render(led)
    parts = led.db.execute(
        "SELECT kind,state,attachment_id FROM notification_render_parts "
        "WHERE delivery_id=?", (render["delivery_id"],)).fetchall()
    assert any(p["kind"] == "attachment_part"
               and p["attachment_id"] == aid for p in parts)
    assert not led.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='attachment_followup'"
    ).fetchall()


@pytest.mark.parametrize("part_state", ["pending", "unknown", "held"])
def test_retention_waits_for_unsettled_attachment_parts(led, tmp_path, monkeypatch, part_state):
    import maintenance
    _seed_thread(led, mids=(100,))
    attachments = tmp_path / "data" / "attachments"
    attachments.mkdir()
    blob = attachments / "1"
    blob.write_bytes(b"synthetic")
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,bytes,sha256,state,downloaded_at) "
        "VALUES(100,'synthetic','synthetic.bin',?,9,?,'downloaded',?)",
        (str(blob), hashlib.sha256(blob.read_bytes()).hexdigest(),
         NOW - maintenance.ATTACHMENT_KEEP_S - 1))
    event = _intent(led, payload={"message_ids": [100]})
    _dispatch(led, event, SLACK)
    with led.db:
        led.db.execute("UPDATE notify_outbox SET state='accepted'")
        led.db.execute("UPDATE notification_renders SET state='delivered'")
        led.db.execute("UPDATE notification_render_parts SET state=? WHERE kind='attachment_part'",
                       (part_state,))
    monkeypatch.setattr(maintenance.time, "time", lambda: NOW)
    db_path = led.db.execute("PRAGMA database_list").fetchone()[2]
    assert maintenance.prune_attachments(db_path) == 0
    assert blob.exists()
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET state='delivered' WHERE kind='attachment_part'")
    assert maintenance.prune_attachments(db_path) == 1
    assert not blob.exists()


def test_retention_releases_attachment_parts_held_by_card_revocation(led, tmp_path, monkeypatch):
    import maintenance
    _seed_thread(led, mids=(100,))
    attachments = tmp_path / "data" / "attachments"
    attachments.mkdir()
    blob = attachments / "1"
    blob.write_bytes(b"synthetic")
    led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,bytes,sha256,state,downloaded_at) "
        "VALUES(100,'synthetic','synthetic.bin',?,9,?,'downloaded',?)",
        (str(blob), hashlib.sha256(blob.read_bytes()).hexdigest(),
         NOW - maintenance.ATTACHMENT_KEEP_S - 1))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    with led.db:
        led.db.execute("UPDATE notify_outbox SET state='accepted'")
        led.db.execute("UPDATE notification_renders SET state='delivered'")
        card_id = led.db.execute("SELECT card_id FROM notification_renders").fetchone()[0]
        notify_cards.revoke_card(led.db, card_id, NOW)
    assert tuple(led.db.execute(
        "SELECT state,error_code FROM notification_render_parts WHERE kind='attachment_part'"
    ).fetchone()) == ("held", "card_revoked")
    monkeypatch.setattr(maintenance.time, "time", lambda: NOW)
    db_path = led.db.execute("PRAGMA database_list").fetchone()[2]
    # a revoked card's parts can never be sent: they must not pin the payload
    assert maintenance.prune_attachments(db_path) == 1
    assert not blob.exists()


# Late-file regressions drive the same runner/worker wire contracts as the
# already-downloaded Slack attachment tests. No new production helper/API.
def _late_slack_file(led, tmp_path, monkeypatch, name="synthetic.png", mid=100):
    import asyncio
    import notify_flush
    from slack_testkit import _mkworld, _granted_card

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    cfg = {**SLACK, "notify_target": "slack:C_SYNTHETIC"}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "hermes_exe", lambda *_: "/bin/sh")
    mids = (100,) if mid == 100 else (100, mid)
    _seed_thread(led, mids=mids)
    with led.db:
        aid = led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,state) "
            "VALUES(?,'synthetic-late',?,'pending')", (mid, name)).lastrowid
    event = _intent(led, payload={"message_ids": list(mids)})
    assert _dispatch(led, event, SLACK)["dispatched"]
    world = _mkworld(led)
    # Each source post owns a card; finish every card before adding the late file.
    for _ in range(len(mids) + 1):
        asyncio.run(_granted_card(world.worker, led, world.root))
        if led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "accepted":
            break
        notify_flush.flush(led)  # The parent's receipts wake and dispatch its held reply cards.
    assert led.db.execute("SELECT COUNT(*) FROM notification_intent_cards "
                          "WHERE event_id=? AND state='delivered'", (event["event_id"],)).fetchone()[0] == len(mids)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "accepted"
    assert not world.client.upload_calls
    source = world.root / "attachments" / str(aid)
    source.parent.mkdir(exist_ok=True)
    blob = ("completely synthetic " + name).encode()
    source.write_bytes(blob)
    monkeypatch.setattr(ledger.time, "time", lambda: NOW + 10)
    led.attachment_saved(aid, str(source), len(blob), hashlib.sha256(blob).hexdigest())
    followup = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='attachment_followup'").fetchone()
    assert followup is not None
    assert json.loads(followup["payload"])["attachment_id"] == aid
    legacy = []

    def no_legacy(*args, **kwargs):
        legacy.append((args, kwargs))
        raise AssertionError("late interactive file must use its bound native thread")

    monkeypatch.setattr(notify_sender, "send", no_legacy)
    return world, aid, int(followup["event_id"]), blob, legacy


def _late_followup_state(led, event_id):
    return led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event_id,)).fetchone()[0]


async def _late_slack_cycle(world, led, *, before_receipt=None):
    import notify_cmds

    assert await world.sender.bind()
    assert world.worker.acquire_scope_lock()
    try:
        await world.worker.tick()
        notify_cmds.drain_int_commands(led, {"errors": []}, SLACK, str(world.root))
        await world.worker.tick()
        if before_receipt is not None:
            before_receipt()
        notify_cmds.drain_int_commands(led, {"errors": []}, SLACK, str(world.root))
    finally:
        world.worker.release_scope_lock()


@pytest.mark.parametrize("name,mid", [
    ("synthetic.png", 100), ("synthetic.pdf", 100), ("synthetic.psf", 100),
    ("synthetic.pdf", 200)])
@pytest.mark.parametrize("receipt_last", ["attachment", "thread", "card"],
                         ids=["file-last", "thread-last", "card-last"])
def test_late_slack_file_uses_original_thread_and_exact_attachment_receipt(
        led, tmp_path, monkeypatch, name, mid, receipt_last):
    import asyncio
    import os
    import notify_cmds
    import notify_flush
    from slack_testkit import ROOT_TS

    world, aid, event_id, blob, legacy = _late_slack_file(
        led, tmp_path, monkeypatch, name, mid)
    previous_posts = len(world.client.thread_posts)
    notify_flush.flush(led)
    assert not legacy
    assert _late_followup_state(led, event_id) != "accepted"
    held = []

    def delay_matching_receipt():
        for path in (world.root / "cmd_int").glob("*.json"):
            req = json.loads(path.read_text())
            matching = receipt_last == "card" and req.get("op") == "transport_receipt"
            if req.get("op") == "part_receipt":
                row = led.db.execute(
                    "SELECT kind,attachment_id FROM notification_render_parts "
                    "WHERE delivery_id=? AND part_id=?",
                    (req.get("delivery_id"), req.get("part_id"))).fetchone()
                matching = row is not None and (
                    (receipt_last == "attachment" and row[1] == aid)
                    or (receipt_last == "thread" and row[0] == "thread"))
            if matching:
                parked = world.root / ("held-" + path.name)
                os.replace(path, parked)
                held.append((path, parked))
        assert len(held) == 1

    try:
        asyncio.run(_late_slack_cycle(world, led, before_receipt=delay_matching_receipt))
        # Every required receipt must land, regardless of arrival order.
        notify_flush.flush(led)
        assert _late_followup_state(led, event_id) != "accepted"
        assert len(world.client.upload_calls) == 1
        upload = world.client.upload_calls[0]
        # private stage: no channel/thread on the upload — the group
        # chat.update(file_ids) attaches the FID to the card itself.
        assert "channel" not in upload and "thread_ts" not in upload
        assert upload["filename"] == name and upload["file"] == blob
        groups = [c[1] for c in world.client.calls if c[0] == "update"
                  and c[1].get("file_ids")]
        assert groups and groups[-1]["file_ids"]
        assert not any("新しい返信をカードに追加しました" in post["text"]
                       for post in world.client.thread_posts[previous_posts:])
        assert not legacy
    finally:
        for path, parked in held:
            if parked.exists():
                os.replace(parked, path)
    notify_cmds.drain_int_commands(led, {"errors": []}, SLACK, str(world.root))
    notify_flush.flush(led)
    assert _late_followup_state(led, event_id) == "accepted"
    row = led.db.execute(
        "SELECT state,remote_id,payload_sha256 FROM notification_render_parts "
        "WHERE attachment_id=? AND state='delivered'", (aid,)).fetchone()
    assert row is not None and row[1].startswith("F_SYNTHETIC")
    assert row[2] == hashlib.sha256(blob).hexdigest()
    owner = led.db.execute(
        "SELECT c.message_id,c.thread_id FROM notification_render_parts p "
        "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
        "JOIN notification_cards c ON c.card_id=r.card_id "
        "WHERE p.attachment_id=? AND p.state='delivered' ORDER BY r.render_rev DESC LIMIT 1", (aid,)).fetchone()
    assert owner[1] == ROOT_TS
    assert (owner[0] == ROOT_TS) is (mid == 100)
    assert groups[-1]["ts"] == owner[0] and groups[-1]["file_ids"] == [row[1]]
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    assert len(world.client.upload_calls) == 1 and not legacy


@pytest.mark.parametrize("fault", ["definitive_rejection", "unknown"])
def test_late_slack_file_failure_never_becomes_accepted_or_auto_duplicates(
        led, tmp_path, monkeypatch, fault):
    import asyncio
    from types import SimpleNamespace
    import notify_flush

    world, aid, event_id, _blob, legacy = _late_slack_file(led, tmp_path, monkeypatch)
    if fault == "unknown":
        failure = TimeoutError("synthetic upload uncertainty")
    else:
        class RejectedUpload(RuntimeError):
            response = SimpleNamespace(
                status_code=403, data={"ok": False, "error": "missing_scope"})

        failure = RejectedUpload("synthetic definitive rejection")
    world.client.upload_fail_at = {0}
    world.client.upload_failure = failure
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    notify_flush.flush(led)
    assert _late_followup_state(led, event_id) != "accepted"
    attempts = [call for call in world.client.calls if call[0] == "files_upload_v2"]
    assert len(attempts) == 1 and not world.client.upload_calls
    row = led.db.execute(
        "SELECT state FROM notification_render_parts WHERE attachment_id=?", (aid,)).fetchone()
    # A composite upload may have allocated a private FID before its later 403.
    assert row is not None and row[0] == "unknown"
    for _ in range(2):
        notify_flush.flush(led)
        asyncio.run(_late_slack_cycle(world, led))
    assert [call for call in world.client.calls if call[0] == "files_upload_v2"] == attempts
    assert _late_followup_state(led, event_id) != "accepted" and not legacy


def test_late_slack_file_cannot_complete_if_render_omits_the_attachment(
        led, tmp_path, monkeypatch):
    import asyncio
    import notify_flush

    world, _aid, event_id, _blob, legacy = _late_slack_file(led, tmp_path, monkeypatch)
    monkeypatch.setattr(notify_parts, "_plan_attachments", lambda *_: [])
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    notify_flush.flush(led)
    assert not world.client.upload_calls and not legacy
    assert _late_followup_state(led, event_id) != "accepted"


@pytest.mark.parametrize("invalid", ["foreign_project", "missing_attachment", "deleted_source", "revoked_card"])
def test_late_slack_file_source_scope_and_revocation_fail_closed(
        led, tmp_path, monkeypatch, invalid):
    import asyncio
    import notify_flush

    world, aid, event_id, _blob, legacy = _late_slack_file(led, tmp_path, monkeypatch)
    with led.db:
        if invalid == "foreign_project":
            led.ensure_patient(2)
            led.db.execute("UPDATE notify_outbox SET project_id=2 WHERE event_id=?", (event_id,))
        elif invalid == "missing_attachment":
            led.db.execute("DELETE FROM attachments WHERE attachment_id=?", (aid,))
        elif invalid == "deleted_source":
            led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
        else:
            card = led.db.execute("SELECT card_id FROM notification_cards").fetchone()[0]
            notify_cards.revoke_card(led.db, card, NOW + 10)
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    notify_flush.flush(led)
    assert not world.client.upload_calls and not legacy
    assert _late_followup_state(led, event_id) != "accepted"


@pytest.mark.parametrize("prior", ["accepted_text", "unknown_text"])
def test_late_slack_file_never_blindly_adopts_old_text_outcomes(
        led, tmp_path, monkeypatch, prior):
    import asyncio
    import notify_flush

    world, aid, event_id, _blob, legacy = _late_slack_file(led, tmp_path, monkeypatch)
    progress = json.dumps({"next": 0, "sending": 0, "ids": [], "fingerprint": "synthetic"})
    with led.db:
        if prior == "accepted_text":
            led.db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event_id,))
        else:
            led.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (progress, event_id))
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    assert not world.client.upload_calls and not legacy
    assert not led.db.execute(
        "SELECT 1 FROM notification_render_parts WHERE attachment_id=?", (aid,)).fetchone()
    if prior == "accepted_text":
        assert _late_followup_state(led, event_id) == "accepted"
    else:
        assert _late_followup_state(led, event_id) != "accepted"
        raw = led.db.execute("SELECT progress FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()[0]
        assert json.loads(raw).get("sending") == 0


def test_two_late_slack_files_survive_one_spec_publish_failure(
        led, tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path
    import notify_flush
    from slack_testkit import ROOT_TS

    world, aid, event_id, blob, legacy = _late_slack_file(led, tmp_path, monkeypatch)
    second_blob = b"completely synthetic second PDF"
    with led.db:
        second_aid = led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,state) "
            "VALUES(100,'synthetic-late-second','synthetic-second.pdf','pending')").lastrowid
    second_source = world.root / "attachments" / str(second_aid)
    second_source.write_bytes(second_blob)
    led.attachment_saved(second_aid, str(second_source), len(second_blob),
                         hashlib.sha256(second_blob).hexdigest())
    second_event = led.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='attachment_followup' "
        "AND json_extract(payload,'$.attachment_id')=?", (second_aid,)).fetchone()
    assert second_event is not None and second_event[0] != event_id
    events = (event_id, second_event[0])
    original_publish = notify_spool.publish_file
    failed = []

    def fail_once(directory, name, raw):
        if Path(directory) == world.root / "slack_render" and not failed:
            failed.append(name)
            raise OSError("synthetic spec publication failure")
        return original_publish(directory, name, raw)

    monkeypatch.setattr(notify_spool, "publish_file", fail_once)
    notify_flush.flush(led)
    assert len(failed) == 1 and not world.client.upload_calls and not legacy
    assert all(_late_followup_state(led, eid) != "accepted" for eid in events)
    # Recover the exact committed render; the other followup waits for
    # the first render's parts instead of claiming a second send owner.
    assert notify_spool.recover(led, SLACK, {})["republished"] == 1
    asyncio.run(_late_slack_cycle(world, led))
    monkeypatch.setattr(ledger.time, "time", lambda: NOW + 11 + notify_cards.RESEAT_S)
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    notify_flush.flush(led)
    assert all(_late_followup_state(led, eid) == "accepted" for eid in events), {
        "outbox": [tuple(row) for row in led.db.execute(
            "SELECT event_id,state,route,next_try,progress FROM notify_outbox")],
        "intents": [tuple(row) for row in led.db.execute(
            "SELECT event_id,state,delivery_id FROM notification_intent_cards")],
        "parts": [tuple(row) for row in led.db.execute(
            "SELECT kind,state,remote_id FROM notification_render_parts")],
    }
    expected = {"synthetic.png": blob, "synthetic-second.pdf": second_blob}
    assert len(world.client.upload_calls) == 2
    assert {call["filename"]: call["file"] for call in world.client.upload_calls} == expected
    assert all("channel" not in call and "thread_ts" not in call
               for call in world.client.upload_calls)
    groups = [call[1] for call in world.client.calls if call[0] == "update" and call[1].get("file_ids")]
    assert groups and groups[-1]["ts"] == ROOT_TS and len(set(groups[-1]["file_ids"])) == 2
    for attachment_id, content in ((aid, blob), (second_aid, second_blob)):
        assert led.db.execute(
            "SELECT 1 FROM notification_render_parts WHERE attachment_id=? "
            "AND state='delivered' AND remote_id IS NOT NULL AND payload_sha256=?",
            (attachment_id, hashlib.sha256(content).hexdigest())).fetchone() is not None
    # Re-entry and worker resume must consume the same factual receipts.
    notify_spool.recover(led, SLACK, {})
    notify_flush.flush(led)
    asyncio.run(_late_slack_cycle(world, led))
    assert len([call for call in world.client.calls if call[0] == "files_upload_v2"]) == 2
    assert len(world.client.upload_calls) == 2 and not legacy
    assert all(_late_followup_state(led, eid) == "accepted" for eid in events)


# Slack's thread part binds an existing root without a thread-creation HTTP
# call. Settle its canonical receipts to model a factual rejected binding.
def _failed_slack_thread_file(led, tmp_path, monkeypatch, result="not_sent"):
    from pathlib import Path
    from adapters.common import envelopes
    import notify_flush
    import notify_transport
    from slack_testkit import ROOT_TS

    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    source = Path(notify_spool.data_root(led)) / "attachments" / "fallback.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    blob = b"completely synthetic rejected-thread PDF"
    source.write_bytes(blob)
    with led.db:
        aid = led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,state,local_path,bytes,sha256) "
            "VALUES(100,'synthetic-rejected','fallback.pdf','downloaded',?,?,?)",
            (str(source), len(blob), hashlib.sha256(blob).hexdigest())).lastrowid
    event = _intent(led, payload={"message_ids": [100]})
    assert _dispatch(led, event, SLACK)["dispatched"]
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    claim = {"spec": spec, "attempt_id": "ab" * 8,
             "worker_id": "bc" * 8, "payload_hash": render["payload_hash"]}
    assert notify_transport.apply_transport_begin(
        led, envelopes.transport_begin(claim), SLACK, now=NOW)["granted"]
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id=ROOT_TS),
        SLACK, now=NOW)["applied"]
    thread = next(p for p in spec["parts"]["manifest"] if p["kind"] == "thread")
    assert notify_transport.apply_part_receipt(
        led, envelopes.part_receipt(claim, thread, result, error_code="http_404"),
        SLACK, now=NOW)["applied"]
    assert led.db.execute(
        "SELECT state FROM notification_render_parts WHERE attachment_id=?", (aid,)
    ).fetchone()[0] == "held"
    row = led.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='attachment_followup'").fetchone()
    # An unknown thread normally queues nothing; a legacy pending row is
    # deliberately supplied so the negative case checks refusal to send it.
    eid = row[0] if row else led.outbox_add(
        "attachment_followup", 1, {"attachment_id": aid, "message_id": 100})
    cfg = {**SLACK, "notify_target": "slack:C_SYNTHETIC"}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "hermes_exe", lambda *_: "/bin/sh")
    sent = []
    monkeypatch.setattr(notify_sender, "send", lambda argv, text, files=None, **kw:
                        sent.append((text, files)))
    return eid, aid, source, blob, cfg, sent


@pytest.mark.parametrize("blocked", [
    None, "unknown_thread", "revoked", "foreign_scope", "foreign_project",
    "owner_pending", "owner_unknown", "owner_delivered", "owner_remote"])
def test_failed_slack_thread_file_falls_back_only_with_safe_witness(
        led, tmp_path, monkeypatch, blocked):
    import notify_flush

    eid, aid, source, blob, cfg, sent = _failed_slack_thread_file(
        led, tmp_path, monkeypatch,
        result="unknown" if blocked == "unknown_thread" else "not_sent")
    if blocked == "revoked":
        with led.db:
            notify_cards.revoke_card(led.db, _latest_render(led)["card_id"], NOW)
    elif blocked == "foreign_scope":
        cfg["notify"] = {**cfg["notify"], "slack": {
            **cfg["notify"]["slack"], "channel_id": "C_FOREIGN_SYNTHETIC"}}
    elif blocked == "foreign_project":
        led.ensure_patient(2)
        with led.db:
            led.db.execute("UPDATE notify_outbox SET project_id=2 WHERE event_id=?", (eid,))
    elif blocked and blocked.startswith("owner_"):
        state = blocked.removeprefix("owner_")
        # Adversarial persisted witness: even contradictory owner state
        # cannot be discarded merely because its thread was not_sent.
        with led.db:
            led.db.execute(
                "UPDATE notification_render_parts SET state=?,remote_id=? "
                "WHERE attachment_id=?",
                ("held" if state == "remote" else state,
                 "F_SYNTHETIC_PRIOR" if state in ("remote", "delivered") else None, aid))
    notify_flush.flush(led)
    row = led.db.execute("SELECT state,route FROM notify_outbox WHERE event_id=?",
                         (eid,)).fetchone()
    if blocked is None:
        assert row["state"] == "accepted" and row["route"] == "text"
        assert len(sent) == 1 and sent[0][1] == [("fallback.pdf", str(source))]
        assert source.read_bytes() == blob
        notify_flush.flush(led)
        assert len(sent) == 1
    else:
        assert not sent and row["state"] != "accepted"


@pytest.mark.parametrize("prior", ["unsealed_unsent", "sealed", "unsealed_unknown"])
@pytest.mark.parametrize("switch", ["interactive", "card_thread"])
def test_slack_attachment_kill_switch_preserves_send_ownership(
        led, tmp_path, monkeypatch, prior, switch):
    import notify_flush

    world, aid, eid, blob, _legacy = _late_slack_file(led, tmp_path, monkeypatch)
    if prior == "sealed":
        notify_flush.flush(led)
        assert led.db.execute(
            "SELECT 1 FROM notification_intent_batches WHERE event_id=?", (eid,)
        ).fetchone() is not None
    else:
        with led.db:
            led.db.execute("UPDATE notify_outbox SET route='interactive' WHERE event_id=?", (eid,))
            if prior == "unsealed_unknown":
                led.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                               (json.dumps({"next": 0, "sent": [], "sending": 1}), eid))
    off = {**SLACK, "notify": {**SLACK["notify"], switch: "off" if switch == "interactive" else False},
           "notify_target": "slack:C_SYNTHETIC"}
    monkeypatch.setattr(notify_flush, "_config", lambda: off)
    monkeypatch.setattr(ledger.time, "time", lambda: NOW + 11 + notify_cards.RESEAT_S)
    sent = []
    monkeypatch.setattr(notify_sender, "send", lambda argv, text, files=None, **kw:
                        sent.append((text, files)))
    notify_flush.flush(led)
    row = led.db.execute("SELECT state,route,progress FROM notify_outbox WHERE event_id=?",
                         (eid,)).fetchone()
    if prior == "unsealed_unsent":
        assert row["state"] == "accepted" and row["route"] == "text"
        assert len(sent) == 1 and len(sent[0][1]) == 1
        assert sent[0][1][0][0] == "synthetic.png"
        from pathlib import Path
        assert Path(sent[0][1][0][1]).read_bytes() == blob
        notify_flush.flush(led)
        assert len(sent) == 1
    else:
        assert not sent and row["route"] == "interactive" and row["state"] != "accepted"
        if prior == "unsealed_unknown":
            assert json.loads(row["progress"])["sending"] == 1
    assert not world.client.upload_calls

