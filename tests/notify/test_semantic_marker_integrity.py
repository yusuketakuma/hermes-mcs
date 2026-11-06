"""Synthetic receipt and in-flight boundaries for semantic notice delivery."""
import json

import pytest

import notify_flush
import notify_sender
import semantic_send_gate
from semantic_testkit import _cfg, _delivery_db, _message, _semantic_event


@pytest.mark.parametrize("raw", [
    '{"next":0,"sent":[],"sending":1,"sending":null}',
    '{"next":0,"sent":[],"fingerprint":NaN}',
    '[]', '0',
])
def test_progress_never_discards_ambiguous_inflight_evidence(raw):
    with pytest.raises(ValueError, match="progress_invalid"):
        notify_flush._progress(raw, 2)


@pytest.mark.parametrize("mutation", ["receipt", "summary", "payload", "progress", "held", "accepted"])
def test_marker_rechecks_current_receipt_and_progress_before_send(tmp_path, monkeypatch, mutation):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        monkeypatch.setattr(notify_sender, "hermes_free", lambda *args: False)
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        original = notify_flush._send_marked
        sent = []
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: sent.append(True))

        def mutate_before_marker(*args, **kwargs):
            with db.db:
                if mutation == "receipt":
                    db.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?",
                                  (payload["notice_receipt_id"],))
                elif mutation == "summary":
                    db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',1) WHERE artifact_id=?",
                                  (payload["summary_artifact_id"],))
                elif mutation == "payload":
                    db.db.execute("UPDATE notify_outbox SET payload=json_set(payload,'$.text','changed') WHERE event_id=?",
                                  (event["event_id"],))
                elif mutation == "progress":
                    db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                                  ('{"next":0,"sent":[],"sending":1,"sending":null}', event["event_id"]))
                elif mutation == "held":
                    db.outbox_hold(event["event_id"], "synthetic_hold")
                else:
                    db.db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event["event_id"],))
            return original(*args, **kwargs)

        monkeypatch.setattr(notify_flush, "_send_marked", mutate_before_marker)
        with pytest.raises((semantic_send_gate.DeferredSend, semantic_send_gate.FreezeSend,
                            notify_flush._SemanticSnapshotChanged)) as caught:
            notify_flush._send_text(db, event, cfg, [], "synthetic", {"sent": 0, "failed": 0}, None)
        notify_flush._fail_event(db, event, cfg, {"sent": 0, "failed": 0, "skipped": 0, "parked": 0}, caught.value)
        assert not sent
        row = db.db.execute("SELECT state,progress,next_try FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        assert row["state"] == ("failed" if mutation == "held" else "accepted" if mutation == "accepted" else "pending")
        if mutation == "progress":
            assert '"sending":1' in row["progress"]
        elif mutation == "held":
            assert row["next_try"] is None and json.loads(row["progress"])["hold_reason"] == "synthetic_hold"
        else:
            assert json.loads(row["progress"])["sending"] is None
    finally:
        db.close()


@pytest.mark.parametrize("progress", [
    '{"next":0,"sent":[],"sending":1,"fingerprint":"synthetic"}',
    '{"next":1,"sent":["1"],"sending":null,"fingerprint":"synthetic"}',
])
def test_newer_progress_is_not_overwritten_by_initialization(tmp_path, monkeypatch, progress):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        monkeypatch.setattr(notify_sender, "hermes_free", lambda *args: False)
        event = _semantic_event(db)
        original = notify_flush._progress
        changed = []

        def concurrent_progress(raw, count):
            value = original(raw, count)
            if not changed:
                changed.append(True)
                with db.db:
                    db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (progress, event["event_id"]))
            return value

        monkeypatch.setattr(notify_flush, "_progress", concurrent_progress)
        sent = []
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: sent.append(True))
        with pytest.raises(notify_flush._SemanticSnapshotChanged):
            notify_flush._send_text(db, event, cfg, [], "synthetic", {"sent": 0, "failed": 0}, None)
        assert not sent
        assert db.db.execute("SELECT progress FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()[0] == progress
    finally:
        db.close()


def test_invalid_progress_never_creates_a_rescue_notice(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event_id = db.outbox_add("new_messages", 1, {"message_ids": [1]})
        raw = '{"next":0,"sent":[],"sending":1,"sending":null}'
        with db.db:
            db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (raw, event_id))
        event = db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        before = db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        notify_flush._fail_event(db, event, _cfg("enforce"), {"sent": 0, "failed": 0, "skipped": 0}, ValueError("progress_invalid"))
        after = db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        assert before == after
        current = db.db.execute("SELECT state,progress FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        assert current["state"] == "failed" and '"sending":1' in current["progress"]
    finally:
        db.close()


def test_send_usage_does_not_erase_concurrent_hold(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event = _semantic_event(db)

        def hold_then_refuse(*args, **kwargs):
            db.outbox_hold(event["event_id"], "synthetic_hold")
            raise notify_sender.SendUsage("synthetic")

        monkeypatch.setattr(notify_sender, "send", hold_then_refuse)
        with pytest.raises(notify_flush._SemanticSnapshotChanged):
            notify_flush._send_marked(db, event, 0, [], "synthetic", [], "text", None, None)
        row = db.db.execute("SELECT state,progress,next_try FROM notify_outbox WHERE event_id=?",
                            (event["event_id"],)).fetchone()
        progress = json.loads(row["progress"])
        assert row["state"] == "failed" and row["next_try"] is None
        assert progress["hold_reason"] == "synthetic_hold" and progress["sending"] == 1
    finally:
        db.close()


def test_early_inflight_hold_does_not_overwrite_accepted_event(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        event = _semantic_event(db)
        with db.db:
            db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                          (json.dumps({"next": 0, "sent": [], "sending": 1}), event["event_id"]))
        event = db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event["event_id"],))
        result = {"sent": 0, "failed": 0, "skipped": 0}
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: pytest.fail("must not send"))
        notify_flush._send_text(db, event, cfg, [], "synthetic", result, None)
        assert result["skipped"] == 1
        row = db.db.execute("SELECT state,progress FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        assert row["state"] == "accepted" and json.loads(row["progress"])["sending"] == 1
    finally:
        db.close()


def test_normal_flush_uses_complete_snapshot_and_sends_once(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        event = _semantic_event(db)
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        monkeypatch.setattr(notify_sender, "target", lambda *args: "synthetic")
        monkeypatch.setattr(notify_sender, "send_argv", lambda *args, kind=None: [])
        monkeypatch.setattr(notify_sender, "hermes_free", lambda *args: False)
        sent = []
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: sent.append(True))
        result = notify_flush.flush(db)
        row = db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        assert result["sent"] == 1 and sent == [True] and row["state"] == "accepted"
    finally:
        db.close()


def test_flush_resumes_after_a_proven_send_failure_without_repeating_chunks(tmp_path, monkeypatch):
    import semantic_render

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        event = _semantic_event(db, text="合成の記録。" * 1300)
        payload = json.loads(event["payload"])
        chunks = semantic_render.semantic_chunks(payload["text"], notify_flush.TEXT_CHUNK_WIDTH)
        assert len(chunks) > 1
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        monkeypatch.setattr(notify_sender, "target", lambda *args: "synthetic")
        monkeypatch.setattr(notify_sender, "send_argv", lambda *args, kind=None: [])
        monkeypatch.setattr(notify_sender, "hermes_free", lambda *args: False)
        calls, accepted = [], []

        def send(_argv, text, *args, **kwargs):
            calls.append(text)
            if len(calls) == 2:
                raise notify_sender.SendFailed("synthetic")
            accepted.append(text)

        monkeypatch.setattr(notify_sender, "send", send)
        first = notify_flush.flush(db)
        assert first["failed"] == 1 and accepted == [chunks[0]]
        row = db.db.execute("SELECT progress FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        assert json.loads(row["progress"])["next"] == 1 and json.loads(row["progress"])["sending"] is None
        with db.db:
            db.db.execute("UPDATE notify_outbox SET next_try=0 WHERE event_id=?", (event["event_id"],))
        second = notify_flush.flush(db)
        assert second["sent"] == 1 and accepted == chunks
        assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()[0] == "accepted"
    finally:
        db.close()


def test_failure_handler_never_overwrites_concurrently_accepted_notice(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event = _semantic_event(db)
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event["event_id"],))
        result = {"sent": 0, "failed": 0, "skipped": 0}
        notify_flush._fail_event(db, event, _cfg("enforce"), result, semantic_send_gate.FreezeSend("synthetic"))
        assert result["skipped"] == 1
        assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()[0] == "accepted"
    finally:
        db.close()


@pytest.mark.parametrize("mutation", ["accepted", "sending"])
def test_interactive_to_text_never_changes_a_concurrently_updated_event(tmp_path, monkeypatch, mutation):
    import mcs_util
    import notify_cards

    db = _delivery_db(tmp_path, [_message()])
    try:
        event_id = db.db.execute("SELECT event_id FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        monkeypatch.setattr(notify_flush, "_config", lambda: {})
        monkeypatch.setattr(mcs_util, "hermes_exe", lambda *args: "/bin/sh")
        monkeypatch.setattr(notify_sender, "target", lambda *args: "synthetic")
        sent = []
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: sent.append(True))
        original = notify_cards.revert_to_text

        def race(ledger, event_id, *args, **kwargs):
            with db.db:
                if mutation == "accepted":
                    db.db.execute("UPDATE notify_outbox SET state='accepted' WHERE event_id=?", (event_id,))
                else:
                    db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                                  (json.dumps({"next": 0, "sent": [], "sending": 1}), event_id))
            return original(ledger, event_id, *args, **kwargs)

        monkeypatch.setattr(notify_cards, "revert_to_text", race)
        result = notify_flush.flush(db)
        row = db.db.execute("SELECT state,route,progress FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        assert not sent and result["skipped"] == 1 and row["route"] == "interactive"
        if mutation == "accepted":
            assert row["state"] == "accepted"
        else:
            assert row["state"] == "pending" and json.loads(row["progress"])["sending"] == 1
    finally:
        db.close()


def test_interactive_route_with_inflight_receipt_is_held_without_route_change(tmp_path, monkeypatch):
    import mcs_util

    db = _delivery_db(tmp_path, [_message()])
    try:
        event_id = db.db.execute("SELECT event_id FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        db.outbox_progress(event_id, 0, [], "synthetic", sending=1)
        monkeypatch.setattr(notify_flush, "_config", lambda: {})
        monkeypatch.setattr(mcs_util, "hermes_exe", lambda *args: "/bin/sh")
        monkeypatch.setattr(notify_sender, "target", lambda *args: "synthetic")
        monkeypatch.setattr(notify_sender, "send", lambda *args, **kwargs: pytest.fail("must not send"))
        result = notify_flush.flush(db)
        row = db.db.execute("SELECT state,route,progress FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        assert result.get("uncertain") == 1
        assert row["state"] == "failed" and row["route"] == "interactive"
        assert json.loads(row["progress"])["sending"] == 1
    finally:
        db.close()


@pytest.mark.parametrize('field,value', [
    ('progress', '{"next":0,"sent":[],"fingerprint":"other","sending":1}'),
    ('route', 'interactive'), ('attempts', 3), ('next_try', 42),
])
def test_failure_does_not_replace_concurrently_changed_semantic_receipt(tmp_path, field, value):
    db = _delivery_db(tmp_path, [_message()])
    try:
        event = _semantic_event(db)
        with db.db:
            db.db.execute(f'UPDATE notify_outbox SET {field}=? WHERE event_id=?', (value, event['event_id']))
        before = dict(db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event['event_id'],)).fetchone())
        result = {'sent': 0, 'failed': 0, 'skipped': 0, 'parked': 0}
        notify_flush._fail_event(db, event, _cfg('enforce'), result, OSError('synthetic'))
        after = dict(db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event['event_id'],)).fetchone())
        assert before == after and result['skipped'] == 1
    finally:
        db.close()


def test_failure_can_record_its_own_refunded_send_marker(tmp_path, monkeypatch):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *args: False)
        monkeypatch.setattr(notify_sender, 'send', lambda *args, **kwargs: (_ for _ in ()).throw(
            notify_sender.SendFailed('synthetic')))
        event = _semantic_event(db)
        result = {'sent': 0, 'failed': 0, 'skipped': 0, 'parked': 0}
        with pytest.raises(notify_sender.SendFailed) as caught:
            notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        notify_flush._fail_event(db, event, cfg, result, caught.value)
        row = db.db.execute('SELECT state,progress FROM notify_outbox WHERE event_id=?', (event['event_id'],)).fetchone()
        assert result['failed'] == 1 and row['state'] == 'failed'
        assert json.loads(row['progress'])['sending'] is None
    finally:
        db.close()
