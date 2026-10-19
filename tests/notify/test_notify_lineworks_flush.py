"""LINE WORKS text outbox uses its independent CLI and holds uncertain attempts."""
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

import notify_flush
import notify_sender
import mcs_util
from ledger import Ledger
from notify_testkit import stored_attachment


@pytest.mark.parametrize("outcome", ["delivered", "timeout", "partial_failure"])
def test_lineworks_without_hermes_uses_json_sealed_attachments_and_no_ambiguous_retry(
        tmp_path, monkeypatch, outcome):
    cfg = {"notify_target": "lineworks:room-synthetic"}
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: "/nonexistent/hermes")
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

    monkeypatch.setattr(notify_sender.subprocess, "run", child)
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
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _: "/nonexistent/hermes")
    monkeypatch.setattr(notify_sender.subprocess, "run", lambda *a, **kw: pytest.fail("must reject before child"))
    files = [("fixture.txt", str(attachment))]

    def render(*args):
        if change == "before_fingerprint":
            attachment.write_bytes(b"different-synthetic-content")
        return "合成通知", files

    monkeypatch.setattr(notify_flush, "_format_event", render)
    fingerprint = notify_sender.delivery_fingerprint

    def replace_after_fingerprint(*args):
        value = fingerprint(*args)
        attachment.write_bytes(b"different-synthetic-content")
        return value

    if change == "after_fingerprint":
        monkeypatch.setattr(notify_sender, 'delivery_fingerprint', replace_after_fingerprint)
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


# Regression: LINE WORKS CLI exit 75 (nothing accepted) retries; other failures hold.
ARGV = [sys.executable, "/synthetic/lineworks_adapter/__main__.py", "send"]


@pytest.mark.parametrize("returncode", [75, 1])
def test_lineworks_tempfail_is_retried_and_unknown_is_held(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_sender, 'target', lambda *a: "lineworks:synthetic")
    monkeypatch.setattr(notify_sender, 'send_argv', lambda *a: list(ARGV))
    monkeypatch.setattr(notify_flush, "_format_event", lambda *a: ("合成通知", []))
    monkeypatch.setattr(notify_sender.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=returncode, stdout="", stderr=""))
    with pytest.raises(notify_sender.SendFailed if returncode == 75
                       else notify_sender.SendUncertain):
        notify_sender.send(ARGV, "合成通知")
    (tmp_path / "data").mkdir()
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    try:
        eid = led.outbox_add("run_failed", None, {})
        result = notify_flush.flush(led)
        row = led.db.execute("SELECT progress,next_try FROM notify_outbox WHERE event_id=?",
                             (eid,)).fetchone()
    finally:
        led.close()
    hold = json.loads(row["progress"] or "{}").get("hold_reason")
    if returncode == 75:
        assert row["next_try"] is not None and result.get("uncertain", 0) == 0
        assert hold != "send_outcome_unknown"
    else:
        assert row["next_try"] is None and result["uncertain"] == 1
        assert hold == "send_outcome_unknown"
