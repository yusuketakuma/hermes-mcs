"""notify_flush — delivery-boundary safety for `hermes send` stdin.

V01: MCS post content is untrusted input. `hermes send` parses MEDIA:
tags and [[as_document]]/[[audio_as_voice]] directives from the whole
stdin stream, so a crafted post could otherwise attach an arbitrary
readable file or force a delivery mode. The notify_flush must defuse
control syntax in the composed body while still appending verified
attachment paths as real directives.
"""
import sys
import json
import subprocess
from types import SimpleNamespace

import pytest

import notify_flush
import structured_view
from ledger import Ledger


def test_defuse_media_tag_from_post_body():
    body = notify_flush._compose_body(
        "薬を確認してください\nMEDIA:/etc/master.passwd", None)
    assert "MEDIA:/etc/master.passwd" not in body
    assert "MEDIA：/etc/master.passwd" in body  # visible, non-parsing


def test_defuse_media_tag_variants():
    for tag in ("MEDIA:~/x.png", "media:/tmp/a.pdf",
                "**MEDIA:/tmp/a.pdf**", "`MEDIA:/etc/hosts`",
                "MEDIA:  /tmp/a.pdf"):
        out = notify_flush._compose_body(f"text\n{tag}\nmore", None)
        assert "MEDIA:" not in out.replace("MEDIA：", ""), tag


def test_defuse_bracket_directives():
    out = notify_flush._compose_body(
        "note [[as_document]] and [[audio_as_voice]] end", None)
    assert "[[as_document]]" not in out
    assert "[[audio_as_voice]]" not in out
    assert "[as_document]" in out and "[audio_as_voice]" in out


def test_verified_attachments_stay_real_tags(tmp_path):
    f = tmp_path / "42"
    f.write_bytes(b"x")
    body = notify_flush._compose_body(
        "post body", [("photo.png", str(f))])
    assert f"\nMEDIA:{f}.png" in body          # alias carries the ext
    assert "MEDIA：" not in body               # nothing defused


def test_verified_attachment_survives_hostile_content(tmp_path):
    f = tmp_path / "9"
    f.write_bytes(b"x")
    body = notify_flush._compose_body(
        "MEDIA:/etc/master.passwd を参照", [("scan.pdf", str(f))])
    lines = [ln for ln in body.splitlines() if ln.startswith("MEDIA:")]
    assert lines == [f"MEDIA:{f}.pdf"]        # only the verified tag


@pytest.mark.parametrize("excluded", [
    {"subject": "family"}, {"subject": "other"}, {"unverified": True},
    {"status": "past"},
])
def test_typed_exclusions_do_not_reappear_through_rule_fallback(monkeypatch,
                                                               excluded):
    artifacts = {
        "extract_llm": {
            "meds": [{"name": "合成薬", **excluded}],
            "symptoms": [{"text": "合成症状", **excluded}],
        },
        "extract_v1": {
            "medications": [{"name": "合成薬", "dose": "1mg"}],
            "rx_actions": [{"action": "start", "ctx": "合成薬を開始"}],
            "symptoms": ["合成症状"],
        },
    }
    monkeypatch.setattr(structured_view, "latest_artifact",
                       lambda db, kind, mid: artifacts[kind])
    monkeypatch.setattr(structured_view, "latest_fact_artifact",
                       lambda db, mid: artifacts["extract_llm"])
    lines = structured_view.structured_lines(None, 1)
    assert not any(line.startswith(("薬剤", "症状:")) for line in lines)


def test_rule_only_medication_is_labeled_unverified(monkeypatch):
    artifacts = {
        "extract_llm": {},
        "extract_v1": {
            "medications": [{"name": "合成薬", "dose": "1mg"}],
            "rx_actions": [{"action": "start", "ctx": "合成薬を開始"}],
        },
    }
    monkeypatch.setattr(structured_view, "latest_artifact",
                       lambda db, kind, mid: artifacts[kind])
    monkeypatch.setattr(structured_view, "latest_fact_artifact",
                       lambda db, mid: artifacts["extract_llm"])
    lines = structured_view.structured_lines(None, 1)
    assert not any(line.startswith("薬剤:") for line in lines)
    assert any(line.startswith("薬剤候補（未確認）:") and "合成薬" in line
               for line in lines)


def test_source_less_display_keeps_high_unconfirmed_and_suppresses_patient_values(monkeypatch):
    document = {"urgency": "high", "urgency_evidence": ["本人が急変"],
                "vitals": {"spo2": 80}, "vital_flags": [{"key": "spo2", "value": 80}]}
    monkeypatch.setattr(structured_view, "latest_artifact", lambda db, kind, mid: document if kind == "extract_llm" else {})
    monkeypatch.setattr(structured_view, "latest_fact_artifact", lambda db, mid: document)
    lines = structured_view.structured_lines(None, 1)
    assert any("要確認" in line for line in lines)
    assert not any("緊急度: 高" in line or "SpO2" in line for line in lines)


@pytest.mark.parametrize("outcome", ["timeout", "partial_failure"])
def test_uncertain_child_delivery_is_held_without_retry(tmp_path, monkeypatch,
                                                       outcome):
    """A child may deliver before timing out or reporting a partial failure."""
    db = Ledger(str(tmp_path / "ledger.db"))
    calls = []
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda cfg, target: ["hermes"])
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_format_event", lambda *a: ("synthetic", []))

    def child(argv, **kwargs):
        calls.append(kwargs["input"])
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout="", stderr="partial failure")

    monkeypatch.setattr(notify_flush.subprocess, "run", child)
    try:
        eid = db.outbox_add("run_failed", None, {})
        result = notify_flush.flush(db)
        row = db.db.execute(
            "SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone()
        assert result["uncertain"] == 1 and result["failed"] == 1
        assert row["state"] == "failed" and row["next_try"] is None
        assert json.loads(row["progress"])["sending"] == 1
        notify_flush.flush(db)
        assert calls == ["synthetic"]
    finally:
        db.close()


def test_missing_exe_does_not_starve_interactive(tmp_path, monkeypatch):
    """hermes exe missing: a text event ahead of an interactive one must
    not break the loop — the card still dispatches (regression: the old
    bulk-skip break swallowed every later event)."""
    db = Ledger(str(tmp_path / "ledger.db"))
    cfg = {"notify": {"interactive": "discord", "route_epoch": 1,
                      "discord": {"profile": "mcs", "application_id": "a",
                                  "guild_id": "g", "channel_id": "c"}}}
    monkeypatch.setattr(notify_flush, "_hermes_exe",
                        lambda cfg: "/nonexistent/hermes")
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    db.db.execute(
        "INSERT INTO patients(project_id,patient_name,is_archived)"
        " VALUES(1,'合成患者',0)")
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,sender_name,"
        "posted_at,posted_at_ts,body_text,content_hash,body_state)"
        " VALUES(100,1,'職員','2026-09-24T08:00',1790000000,'本文',"
        f"{'a' * 64!r},'full')")
    db.db.commit()
    try:
        db.outbox_add("run_failed", None, {})
        db.outbox_add("new_messages", 1, {"message_ids": [100]})
        res = notify_flush.flush(db)
        # the text event is skipped; the interactive card dispatched
        assert res["skipped"] == 1
        assert res.get("dispatched") == 1
        assert db.db.execute(
            "SELECT COUNT(*) c FROM notification_renders"
        ).fetchone()["c"] == 1
        # a second flush re-enters the sealed intent idempotently
        res2 = notify_flush.flush(db)
        assert res2.get("dispatched", 0) <= 1
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["uncertain", "partial_usage", "corrupt"])
def test_hold_uses_current_delivery_receipt(tmp_path, monkeypatch, failure):
    """A freshly written receipt must prevent a rescue of an ambiguous send."""
    db = Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_target", lambda *args: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *args: ["hermes"])
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda *args: ("x" * (notify_flush._MAX_LEN + 1), []))
    monkeypatch.setattr(notify_flush, "_semantic_render_state", lambda *args: ())
    calls = []

    def send(*args, **kwargs):
        calls.append(1)
        if failure == "uncertain":
            raise notify_flush._SendUncertain("synthetic")
        if len(calls) == 2:
            raise notify_flush._SendUsage("synthetic")

    monkeypatch.setattr(notify_flush, "_send", send)
    try:
        eid = db.outbox_add("new_messages", None, {"message_ids": [1]})
        if failure == "corrupt":
            db.db.execute("UPDATE notify_outbox SET progress='[]'")
            db.db.commit()
        result = notify_flush.flush(db)
        assert result["failed"] == 1
        rows = db.db.execute("SELECT * FROM notify_outbox").fetchall()
        assert len(rows) == 1, "unknown or partially delivered intent was rescued"
        assert rows[0]["event_id"] == eid
        assert rows[0]["state"] == "failed" and rows[0]["next_try"] is None
        assert len(calls) == {"uncertain": 1, "partial_usage": 2, "corrupt": 0}[failure]
    finally:
        db.close()


def test_deleted_message_cannot_supply_notification_content(tmp_path):
    """Tombstones retain source bytes for audit, never for delivery."""
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'合成患者')")
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,body_state,body_text,body_html) "
            "VALUES(1,1,'deleted','合成削除本文','<p>合成削除本文</p>')")
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,file_id,name,state,local_path) "
            "VALUES(1,1,'file1','synthetic.txt','downloaded','synthetic')")
        db.db.commit()
        signal = notify_flush._signal_text(
            db, {"text": "候補\n場所", "project_id": 1},
            {"evidence": {"message_ids": [1]}})
        assert "合成削除本文" not in signal
        for kind, payload in (("new_messages", {"message_ids": [1]}),
                              ("attachment_followup", {"attachment_id": 1})):
            with pytest.raises(notify_flush._StaleSend):
                notify_flush._format_event(db, {"kind": kind, "project_id": 1,
                                            "payload": json.dumps(payload)})
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["new_messages", "attachment_followup"])
def test_event_cannot_read_another_patient(tmp_path, kind):
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.ensure_patient(1)
        db.ensure_patient(2)
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,body_state,body_text,body_html,posted_at) "
            "VALUES(200,2,'full','OTHER','<p>OTHER</p>','2026-09-24T08:00')")
        path = tmp_path / "synthetic"
        path.write_bytes(b"OTHER")
        import hashlib
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,file_id,name,state,local_path,sha256) "
            "VALUES(1,200,'f','synthetic.txt','downloaded',?,?)",
            (str(path), hashlib.sha256(b"OTHER").hexdigest()))
        db.db.commit()
        payload = {"message_ids": [200]} if kind == "new_messages" else {"attachment_id": 1}
        with pytest.raises(notify_flush._StaleSend):
            notify_flush._format_event(db, {"kind": kind, "project_id": 1,
                                           "payload": json.dumps(payload)})
    finally:
        db.close()


def test_session_recovered_renders_as_resolved_notice(tmp_path):
    """The recovery notice is a plain system text event — it reports the
    expiry AS resolved and rides notify_system_target like
    session_expired."""
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        eid = db.outbox_add("session_recovered", None,
                            {"run_id": 5,
                             "detail": "history_jobs: "
                                       "session_expired(status=403)"})
        ev = db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                           (eid,)).fetchone()
        text, files = notify_flush._format_event(db, ev)
        assert "自動再ログインで復旧" in text
        assert "run 5" in text and "history_jobs" in text
        assert "手動再ログインが必要" not in text
        assert files == []
        cfg = {"notify_target": "discord:1",
               "notify_system_target": "slack:#ops"}
        assert notify_flush._target(cfg, "session_recovered") == \
            "slack:#ops"
    finally:
        db.close()
