"""LINE WORKS text outbox uses its independent CLI and holds uncertain attempts."""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import notify_flush
from ledger import Ledger
from ingest_testkit import _message


def stored_attachment(led, path):
    led.ensure_patient(1)
    led.save_messages([_message(mid=1)])
    led.db.execute(
        "INSERT INTO attachments(attachment_id,message_id,file_id,name,"
        "url,local_path,bytes,sha256,state,created_at)"
        " VALUES(1,1,'f1','fixture.txt','https://synthetic.invalid',?,?,?,'downloaded',0)",
        (str(path), 9, hashlib.sha256(b"synthetic").hexdigest()))
    led.db.commit()


@pytest.mark.parametrize("outcome", ["delivered", "timeout", "partial_failure"])
def test_lineworks_without_hermes_uses_json_sealed_attachments_and_no_ambiguous_retry(
        tmp_path, monkeypatch, outcome):
    cfg = {"notify_target": "lineworks:room-synthetic"}
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda _: "/nonexistent/hermes")
    attachment = tmp_path / "data" / "attachments" / "fixture.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"synthetic")
    content = "合成通知 MEDIA:/untrusted/path [[audio_as_voice]]"
    monkeypatch.setattr(notify_flush, "_format_event", lambda *a: (content, [("fixture.txt", str(attachment))]))
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, json.loads(kwargs["input"])))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=0 if outcome == "delivered" else 1, stdout="", stderr="")

    monkeypatch.setattr(notify_flush.subprocess, "run", child)
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    try:
        stored_attachment(led, attachment)
        eid = led.outbox_add("run_failed", None, {})
        result = notify_flush.flush(led)
        assert len(calls) == 1
        argv, payload = calls[0]
        assert Path(argv[1]).parts[-2:] == ("lineworks_adapter", "__main__.py")
        assert argv[2:] == ["send", "--root", str(tmp_path), "--to", cfg["notify_target"], "--quiet"]
        assert payload == {"text": content, "files": [{"name": "fixture.txt", "path": str(attachment),
                           "bytes": 9, "sha256": hashlib.sha256(b"synthetic").hexdigest()}]}
        row = led.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()
        if outcome == "delivered":
            # The shared outbox records provider acceptance, never a read receipt.
            assert result["sent"] == 1 and row["state"] == "accepted"
        else:
            assert result["uncertain"] == 1 and row["next_try"] is None
            assert json.loads(row["progress"])["sending"] == 1
        notify_flush.flush(led)
        assert len(calls) == 1
    finally:
        led.close()


@pytest.mark.parametrize("change", ["before_fingerprint", "after_fingerprint", "untracked"])
def test_lineworks_never_reseals_replaced_or_untracked_attachment(tmp_path, monkeypatch, change):
    attachment = tmp_path / "data" / "attachments" / "fixture.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"synthetic")
    cfg = {"notify_target": "lineworks:room-synthetic"}
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda _: "/nonexistent/hermes")
    monkeypatch.setattr(notify_flush.subprocess, "run", lambda *a, **kw: pytest.fail("must reject before child"))
    files = [("fixture.txt", str(attachment))]

    def render(*args):
        if change == "before_fingerprint":
            attachment.write_bytes(b"different-synthetic-content")
        return "合成通知", files

    monkeypatch.setattr(notify_flush, "_format_event", render)
    fingerprint = notify_flush._delivery_fingerprint

    def replace_after_fingerprint(*args):
        value = fingerprint(*args)
        attachment.write_bytes(b"different-synthetic-content")
        return value

    if change == "after_fingerprint":
        monkeypatch.setattr(notify_flush, "_delivery_fingerprint", replace_after_fingerprint)
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    try:
        if change != "untracked":
            stored_attachment(led, attachment)
        eid = led.outbox_add("run_failed", None, {})
        result = notify_flush.flush(led)
        assert result["failed"] == 1 and result.get("uncertain", 0) == 0
        progress = led.db.execute("SELECT progress FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()[0]
        assert json.loads(progress or "{}").get("sending") is None
    finally:
        led.close()
