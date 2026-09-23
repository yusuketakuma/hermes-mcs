"""notifier — delivery-boundary safety for `hermes send` stdin.

V01: MCS post content is untrusted input. `hermes send` parses MEDIA:
tags and [[as_document]]/[[audio_as_voice]] directives from the whole
stdin stream, so a crafted post could otherwise attach an arbitrary
readable file or force a delivery mode. The notifier must defuse
control syntax in the composed body while still appending verified
attachment paths as real directives.
"""
import sys
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import notifier
from ledger import Ledger


def test_defuse_media_tag_from_post_body():
    body = notifier._compose_body(
        "薬を確認してください\nMEDIA:/etc/master.passwd", None)
    assert "MEDIA:/etc/master.passwd" not in body
    assert "MEDIA：/etc/master.passwd" in body  # visible, non-parsing


def test_defuse_media_tag_variants():
    for tag in ("MEDIA:~/x.png", "media:/tmp/a.pdf",
                "**MEDIA:/tmp/a.pdf**", "`MEDIA:/etc/hosts`",
                "MEDIA:  /tmp/a.pdf"):
        out = notifier._compose_body(f"text\n{tag}\nmore", None)
        assert "MEDIA:" not in out.replace("MEDIA：", ""), tag


def test_defuse_bracket_directives():
    out = notifier._compose_body(
        "note [[as_document]] and [[audio_as_voice]] end", None)
    assert "[[as_document]]" not in out
    assert "[[audio_as_voice]]" not in out
    assert "[as_document]" in out and "[audio_as_voice]" in out


def test_verified_attachments_stay_real_tags(tmp_path):
    f = tmp_path / "42"
    f.write_bytes(b"x")
    body = notifier._compose_body(
        "post body", [("photo.png", str(f))])
    assert f"\nMEDIA:{f}.png" in body          # alias carries the ext
    assert "MEDIA：" not in body               # nothing defused


def test_verified_attachment_survives_hostile_content(tmp_path):
    f = tmp_path / "9"
    f.write_bytes(b"x")
    body = notifier._compose_body(
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
    monkeypatch.setattr(notifier, "_artifact", lambda db, kind, mid: artifacts[kind])
    lines = notifier._structured_lines(None, 1)
    assert not any(line.startswith(("薬剤", "症状:")) for line in lines)


def test_rule_only_medication_is_labeled_unverified(monkeypatch):
    artifacts = {
        "extract_llm": {},
        "extract_v1": {
            "medications": [{"name": "合成薬", "dose": "1mg"}],
            "rx_actions": [{"action": "start", "ctx": "合成薬を開始"}],
        },
    }
    monkeypatch.setattr(notifier, "_artifact", lambda db, kind, mid: artifacts[kind])
    lines = notifier._structured_lines(None, 1)
    assert not any(line.startswith("薬剤:") for line in lines)
    assert any(line.startswith("薬剤候補（未確認）:") and "合成薬" in line
               for line in lines)


@pytest.mark.parametrize("outcome", ["timeout", "partial_failure"])
def test_uncertain_child_delivery_is_held_without_retry(tmp_path, monkeypatch,
                                                       outcome):
    """A child may deliver before timing out or reporting a partial failure."""
    db = Ledger(str(tmp_path / "ledger.db"))
    calls = []
    monkeypatch.setattr(notifier, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notifier, "_target", lambda cfg, kind: "synthetic")
    monkeypatch.setattr(notifier, "_send_argv", lambda cfg, target: ["hermes"])
    monkeypatch.setattr(notifier, "_config", lambda: {})
    monkeypatch.setattr(notifier, "_format_event", lambda *a: ("synthetic", []))

    def child(argv, **kwargs):
        calls.append(kwargs["input"])
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout="", stderr="partial failure")

    monkeypatch.setattr(notifier.subprocess, "run", child)
    try:
        eid = db.outbox_add("run_failed", None, {})
        result = notifier.flush(db)
        row = db.db.execute(
            "SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone()
        assert result["uncertain"] == 1 and result["failed"] == 1
        assert row["state"] == "failed" and row["next_try"] is None
        assert json.loads(row["progress"])["sending"] == 1
        notifier.flush(db)
        assert calls == ["synthetic"]
    finally:
        db.close()
