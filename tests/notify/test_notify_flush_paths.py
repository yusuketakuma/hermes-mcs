"""notify_flush paths — flush queue ordering, event formatting,
quarantine, media file gates, send fallback rules.  ``hermes send`` is
never invoked; delivery is stubbed."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import notify_flush
import structured_view
from ingest_testkit import _ledger, _message, _unread_patient
from ledger import Ledger


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
    assert Path(p).read_bytes() == b"jpg-bytes"
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


def _synthetic_msg_ledger(tmp_path, messages):
    from ledger import Ledger
    (tmp_path / "data").mkdir()
    db = Ledger(str(tmp_path / "data" / "ledger.db"))
    db.db.execute("INSERT INTO patients(project_id,patient_name,is_archived)"
                  " VALUES(1,'合成患者',0)")
    for mid, posted_at, parent in messages:
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,sender_name,"
            "posted_at,body_html,body_text,content_hash,body_state,"
            "parent_id) VALUES(?,1,'職員',?,'<p>本文</p>','本文',?,'full',?)",
            (mid, posted_at, f"{mid:064x}", parent))
    db.db.commit()
    return db


def _event(db, mids):
    eid = db.outbox_add("new_messages", 1, {"message_ids": mids})
    return db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                         (eid,)).fetchone()


def test_unsent_attachments_are_marked_in_text(tmp_path, monkeypatch):
    """U03-F04: a downloaded file dropped by the cumulative cap or a hash
    mismatch must be marked 未送信, not listed as if attached; more than
    five files show a remainder count."""
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(notify_flush, "_MAX_FILES_BYTES", 12)
    db = _synthetic_msg_ledger(tmp_path, [(1, "2026-09-24T08:00", None)])
    try:
        specs = [(b"12345678", True), (b"abcdefgh", True), (b"zzzz", False)]
        specs += [(b"", False)] * 4           # not yet downloaded
        for i, (content, good) in enumerate(specs):
            path = tmp_path / f"att{i}"
            path.write_bytes(content)
            digest = (hashlib.sha256(content).hexdigest() if good
                      else "0" * 64)
            db.db.execute(
                "INSERT INTO attachments(message_id,file_id,name,state,"
                "local_path,bytes,sha256) VALUES(1,?,?,?,?,?,?)",
                (f"f{i}", f"file{i}.pdf",
                 "downloaded" if i < 3 else "pending",
                 str(path), len(content), digest))
        db.db.commit()
        text, files = notify_flush._format_event(db, _event(db, [1]))
        assert [name for name, _ in files] == ["file0.pdf"]
        line = next(ln for ln in text.splitlines() if ln.startswith("📎"))
        assert "file0.pdf、" in line and "file0.pdf (" not in line
        assert "file1.pdf (容量上限·未送信)" in line
        assert "file2.pdf (未送信)" in line
        assert line.endswith("、他2件")
    finally:
        db.close()


def test_null_posted_at_does_not_sink_text_notice(tmp_path, monkeypatch):
    """U03-F05: a message with NULL posted_at (still announced by the
    ledger) must render instead of failing the whole event."""
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    db = _synthetic_msg_ledger(tmp_path, [
        (1, "2026-09-24T08:00", None), (2, None, None),
        (3, None, 1), (4, "2026-09-24T08:05", 1)])
    try:
        text, _ = notify_flush._format_event(db, _event(db, [1, 2, 3, 4]))
        assert "新着 4 件" in text and "— ?" in text
    finally:
        db.close()


_CARD_CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                       "discord": {"profile": "mcs", "application_id": "a",
                                   "guild_id": "g", "channel_id": "c"}}}


def _seed_card_patients(db, n):
    for i in range(n):
        db.db.execute(
            "INSERT INTO patients(project_id,patient_name,is_archived)"
            " VALUES(?,?,0)", (1 + i, f"合成患者{i}"))
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,sender_name,"
            "posted_at,posted_at_ts,body_text,content_hash,body_state)"
            " VALUES(?,?,'職員','2026-09-24T08:00',1790000000,'本文',?,"
            "'full')", (100 + i, 1 + i, f"{i:064x}"))
    db.db.commit()


def test_resealed_pending_intents_do_not_starve_text_events(tmp_path,
                                                            monkeypatch):
    """U03-F01: sealed intents whose cards stay undelivered past RESEAT_S
    must be re-seated on re-entry, not stay due forever and fill every
    outbox_due slot ahead of a later text event."""
    db = Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_config", lambda: _CARD_CFG)
    monkeypatch.setattr(notify_flush, "_target", lambda *args: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *args: ["hermes"])
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda *args: ("synthetic", []))
    monkeypatch.setattr(notify_flush, "_semantic_render_state", lambda *args: ())
    sent = []
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *args, **kwargs: sent.append(1))
    try:
        _seed_card_patients(db, 10)
        for i in range(10):
            db.outbox_add("new_messages", 1 + i, {"message_ids": [100 + i]})
        assert notify_flush.flush(db, limit=10).get("dispatched") == 10
        # an hour on, the cards are still undelivered (worker down)
        db.db.execute("UPDATE notify_outbox SET next_try=? "
                      "WHERE route='interactive'", (time.time() - 1,))
        db.db.commit()
        eid = db.outbox_add("run_failed", None, {"run_id": 1})
        notify_flush.flush(db, limit=10)       # re-entry re-seats them
        notify_flush.flush(db, limit=10)
        row = db.db.execute("SELECT state FROM notify_outbox "
                            "WHERE event_id=?", (eid,)).fetchone()
        assert row["state"] == "accepted" and sent
        pending = db.db.execute(
            "SELECT next_try FROM notify_outbox WHERE route='interactive'"
        ).fetchall()
        assert len(pending) == 10
        assert all(r["next_try"] > time.time() for r in pending)
    finally:
        db.close()


def test_invalid_interactive_payload_is_held_not_retried(tmp_path,
                                                        monkeypatch):
    """U03-F07: a non-dict frozen payload is quarantined by dispatch; the
    flush must not re-arm it with an hourly retry forever."""
    db = Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_config", lambda: _CARD_CFG)
    try:
        _seed_card_patients(db, 1)
        eid = db.outbox_add("new_messages", 1, {"message_ids": [100]})
        db.db.execute("UPDATE notify_outbox SET payload='[]' "
                      "WHERE event_id=?", (eid,))
        db.db.commit()
        assert notify_flush.flush(db)["failed"] == 1
        row = db.db.execute("SELECT state,next_try FROM notify_outbox "
                            "WHERE event_id=?", (eid,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
    finally:
        db.close()
