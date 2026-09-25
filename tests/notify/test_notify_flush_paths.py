"""notify_flush paths — flush queue ordering, event formatting,
quarantine, media file gates, send fallback rules.  ``hermes send`` is
never invoked; delivery is stubbed."""

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import notify_flush
import structured_view
from ingest_testkit import _ledger, _message, _unread_patient


def test_attachment_collection_has_cumulative_real_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_flush, "_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(notify_flush, "_MAX_FILES_BYTES", 10)
    paths = []
    for i in range(2):
        path = tmp_path / str(i)
        path.write_bytes(b"123456")
        paths.append(path)
    att_map = {1: [
        {"state": "downloaded", "local_path": str(paths[0]),
         "bytes": 1, "name": "a", "file_id": "1",
         "sha256": hashlib.sha256(b"123456").hexdigest()},
        {"state": "downloaded", "local_path": str(paths[1]),
         "bytes": 1, "name": "b", "file_id": "2",
         "sha256": hashlib.sha256(b"123456").hexdigest()},
    ]}
    assert len(notify_flush._collect_files(att_map, [1])) == 1


def test_notifier_ignores_artifact_for_old_body(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="current")])
    db.artifact_add("extract_v1", '{"urgency":"high"}', project_id=1,
                    message_id=1, meta={"hash": "old"})

    assert structured_view.latest_artifact(db.db, "extract_v1", 1) is None
    db.close()


def test_changed_partial_notification_is_quarantined(monkeypatch, tmp_path):
    db = _ledger(tmp_path)
    eid = db.outbox_add("new", 1, {})
    db.outbox_progress(eid, 1, ["1"], "old")
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda ledger, event: ("changed", []))
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *args: pytest.fail("must not send"))
    try:
        assert notify_flush.flush(db)["failed"] == 1
        row = db.db.execute("SELECT state,next_try FROM notify_outbox WHERE event_id=?",
                            (eid,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
    finally:
        db.close()


def test_missing_notification_channel_stays_retryable(monkeypatch):
    marked = []

    class Outbox:
        def outbox_due(self, limit):
            return [{"event_id": 1, "kind": "new_messages", "project_id": 1,
                     "payload": "{}", "attempts": 0, "progress": None}]

        def is_archived(self, project_id):
            return False

        def outbox_mark(self, event_id, state, retry_in=60):
            marked.append((event_id, state, retry_in))

        def outbox_hold(self, event_id):
            pytest.fail("a recoverable config error must not discard retries")

    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: None)

    assert notify_flush.flush(Outbox()) == {"sent": 0, "failed": 1,
                                        "skipped": 0, "suppressed": 0,
                                        "parked": 0}
    assert marked == [(1, "failed", 3600)]


def test_notify_file_rejection_falls_back_to_text(tmp_path, monkeypatch):
    """A usage rejection of the file-bearing send (hermes send exit 2 —
    never a delivery failure where acceptance is unknown) must not sink
    the notification — drop the files and retry the chunk text-only."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(70))
    f = tmp_path / "f.txt"
    f.write_bytes(b"x")
    db.outbox_add("new_messages", 70, {"message_ids": [1]})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda led, ev: ("body text", [("f.txt", str(f))]))
    calls = []

    def fake_send(argv, content, files=None, **kw):
        calls.append(bool(files))
        if files:
            raise notify_flush._SendUsage("media rejected")

    monkeypatch.setattr(notify_flush, "_send", fake_send)
    res = notify_flush.flush(db)
    assert res["sent"] == 1
    assert calls == [True, False]
    assert db.db.execute(
        "SELECT state FROM notify_outbox").fetchone()[0] == "accepted"
    db.close()


def test_media_path_aliases_extensionless_files(tmp_path):
    """Attachments are stored extensionless (attachments/<id>) but
    platforms derive the upload filename from the path basename — the
    MEDIA path must carry the original extension or Discord renders a
    generic blob instead of an image."""
    src = tmp_path / "18156"
    src.write_bytes(b"jpg-bytes")
    p = notify_flush._media_path("IMG_1.JPG", str(src))
    assert p == str(src) + ".jpg"
    assert os.path.exists(p)                 # alias created
    assert open(p, "rb").read() == b"jpg-bytes"
    assert notify_flush._media_path("IMG_1.JPG", str(src)) == p  # idempotent
    # name without a sane extension, or path already carrying one -> unchanged
    assert notify_flush._media_path("noext", str(src)) == str(src)
    assert notify_flush._media_path("x.png", str(src) + ".jpg") == str(src) + ".jpg"


@pytest.mark.parametrize("alias_kind", ["old_hardlink", "copy", "symlink", "copy_fallback"])
def test_upload_alias_uses_verified_payload_after_replacement(tmp_path, monkeypatch, alias_kind):
    source = tmp_path / "42"
    source.write_bytes(b"previous-payload")
    alias = tmp_path / "42.pdf"
    other = tmp_path / "unrelated"
    other.write_bytes(b"not-an-attachment")
    if alias_kind == "old_hardlink":
        os.link(source, alias)
    elif alias_kind in ("copy", "copy_fallback"):
        alias.write_bytes(b"previous-payload")
    else:
        alias.symlink_to(other)
    replacement = tmp_path / "42.part"
    replacement.write_bytes(b"verified-current-payload")
    os.replace(replacement, source)
    if alias_kind == "copy_fallback":
        def no_hardlinks(*args):
            raise OSError("synthetic unsupported hardlinks")
        monkeypatch.setattr(notify_flush.os, "link", no_hardlinks)
    files = notify_flush._collect_files({1: [{
        "state": "downloaded", "local_path": str(source), "name": "a.pdf",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]}, [1])

    body = notify_flush._compose_body("synthetic", files)
    upload = Path(body.split("\nMEDIA:", 1)[1])
    assert upload.read_bytes() == source.read_bytes()
    assert not upload.is_symlink()
    assert other.read_bytes() == b"not-an-attachment"


def test_send_writes_media_tags_with_extension(tmp_path, monkeypatch):
    """_send must emit MEDIA: lines on the aliased (extension-carrying)
    path so the platform upload keeps a real filename."""
    src = tmp_path / "99"
    src.write_bytes(b"x")
    sent = {}
    monkeypatch.setattr(
        notify_flush.subprocess, "run",
        lambda argv, **kw: sent.update(argv=argv, body=kw["input"])
        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    notify_flush._send(["hermes", "send"], "text",
                   [("photo.jpg", str(src))])
    assert f"MEDIA:{src}.jpg" in sent["body"]
    assert "text" in sent["body"]


def test_notify_no_fallback_on_ambiguous_errors(tmp_path, monkeypatch):
    """Delivery failure (hermes send exit 1) means acceptance is
    unknown — retrying text-only could duplicate, so no fallback."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(71))
    f = tmp_path / "f.txt"
    f.write_bytes(b"x")
    db.outbox_add("new_messages", 71, {"message_ids": [1]})
    db.outbox_add("new_messages", 71, {"message_ids": [2]})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda led, ev: ("body text", [("f.txt", str(f))]))
    calls = []

    def fake_send(argv, content, files=None, **kw):
        calls.append(bool(files))
        raise notify_flush._SendFailed("delivery failed")

    monkeypatch.setattr(notify_flush, "_send", fake_send)
    res = notify_flush.flush(db)
    assert res["sent"] == 0 and res["failed"] == 2
    assert calls == [True, True]  # never retried without files
    db.close()


def test_flush_unexpected_event_error_does_not_starve_queue(monkeypatch, tmp_path):
    """Retry an unexpected failure, quarantine after five attempts,
    and continue delivering later queued events."""
    db = _ledger(tmp_path)
    first = db.outbox_add("semantic_notice", 1, {})
    second = db.outbox_add("new_messages", 1, {"message_ids": []})
    sent = []

    def boom(ledger, event):
        if event["event_id"] == first:
            raise RuntimeError("semantic layer exploded")
        return ("text", [])

    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_format_event", boom)
    monkeypatch.setattr(notify_flush, "_semantic_render_state", lambda *a: ())
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *a, **k: sent.append(a) or None)
    try:
        res = notify_flush.flush(db)
        row = db.db.execute("SELECT state,next_try FROM notify_outbox WHERE event_id=?",
                            (first,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is not None
        assert len(sent) == 1 and res["sent"] == 1 and res["failed"] == 1
        assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                             (second,)).fetchone()[0] == "accepted"
        db.db.execute("UPDATE notify_outbox SET attempts=4,next_try=0 WHERE event_id=?",
                      (first,))
        db.db.commit()
        res2 = notify_flush.flush(db)
        row = db.db.execute("SELECT state,next_try FROM notify_outbox WHERE event_id=?",
                            (first,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
        assert res2["failed"] == 1 and len(sent) == 1
    finally:
        db.close()


# ---------- F11 follow-up rendering ----------


def test_attachment_followup_format_and_stale(tmp_path):
    """F11: the follow-up renders text+file while downloaded, and is a
    terminal drop once the file is gone."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=5)])
    h = hashlib.sha256(b"data").hexdigest()
    path = tmp_path / "7"
    path.write_bytes(b"data")
    db.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,file_id,name,"
        "url,local_path,bytes,sha256,state,downloaded_at,created_at)"
        " VALUES(9,5,'f1','a.pdf','https://x',?,?,?,'downloaded',0,0)",
        (str(path), 4, h))
    db.db.commit()
    ev = {"kind": "attachment_followup",
          "payload": json.dumps({"attachment_id": 9, "message_id": 5}),
          "project_id": 1}
    text, files = notify_flush._format_event(db, ev)
    assert "添付ファイル（後送）" in text and files == [("a.pdf", str(path))]
    db.db.execute(
        "UPDATE attachments SET state='pruned',local_path=NULL "
        "WHERE attachment_id=9")
    db.db.commit()
    with pytest.raises(notify_flush._StaleSend):
        notify_flush._format_event(db, ev)
    db.close()


# ---------- F18: signal evidence fingerprint ----------


def test_signal_notice_rerenders_on_evidence_move(tmp_path, monkeypatch):
    """F18: queued signal text is pinned to an evidence fingerprint —
    a superseded signal re-renders the WHOLE body from the current row
    instead of mixing the frozen note with new evidence."""
    import mcs_signals
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    sig_old = {"type": "t", "project_id": 1, "note": "old note",
               "evidence": {"message_id": 5}}
    sig_new = {"type": "t", "project_id": 1, "note": "new note",
               "evidence": {"message_id": 9}}
    payload = {"signal_key": "k1", "project_id": 1,
               "text": mcs_signals.signal_notice_text(sig_old),
               "evidence_fp": mcs_signals.evidence_fp(sig_old["evidence"])}
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
        " VALUES('signal_v1',1,?,json_object('key','k1'),0)",
        (json.dumps(dict(sig_new, state="open")),))
    db.db.commit()
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    text, _ = notify_flush._format_event(
        db, {"kind": "signal", "payload": json.dumps(payload),
             "project_id": 1})
    assert "new note" in text and "old note" not in text
    # A legacy intent without a fingerprint and a note update over the
    # same evidence must also use one coherent current signal row.
    for fp in (None, mcs_signals.evidence_fp(sig_new["evidence"])):
        if fp is None:
            payload.pop("evidence_fp")
        else:
            payload["evidence_fp"] = fp
        text2, _ = notify_flush._format_event(
            db, {"kind": "signal", "payload": json.dumps(payload),
                 "project_id": 1})
        assert "new note" in text2 and "old note" not in text2
    db.close()


# ---------- F19: uncertain in-flight delivery ----------


def test_flush_holds_uncertain_inflight_send(tmp_path, monkeypatch):
    """F19: a progress row whose last write marked a chunk in-flight but
    never recorded the ack = possible duplicate — hold it for human
    reconciliation, never blind-resend."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    m = _message(mid=5)
    db.save_messages([m])
    eid = db.outbox_add("new_messages", 1, {"message_ids": [5]})
    fp = "x" * 64
    db.outbox_progress(eid, 0, [], fp, sending=1)
    calls = []
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *a, **k: calls.append(a) or None)
    res = notify_flush.flush(db)
    assert calls == [] and res.get("uncertain") == 1
    assert db.db.execute(
        "SELECT state FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()[0] == "failed"
    db.close()


def test_send_marked_clears_marker_on_reported_failure(tmp_path):
    """F19: a REPORTED send failure clears the in-flight marker so the
    scheduled retry is not held as an uncertain crash-window send."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    eid = db.outbox_add("new_messages", 1, {"message_ids": [5]})
    ev = {"event_id": eid}
    def boom(*a, **k):
        raise notify_flush._SendFailed("down")
    old = notify_flush._send
    notify_flush._send = boom
    try:
        with pytest.raises(notify_flush._SendFailed):
            notify_flush._send_marked(db, ev, 0, [], "fp",
                                  ["hermes", "send"], "chunk", None, None)
    finally:
        notify_flush._send = old
    progress = json.loads(db.db.execute(
        "SELECT progress FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()[0])
    assert progress["sending"] is None and progress["next"] == 0
    db.close()
