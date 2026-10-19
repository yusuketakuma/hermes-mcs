"""Hermes text delivery defuses untrusted notification fields before upload tags."""
import semantic_render
import html
import hashlib
import subprocess

import pytest

import notify_flush
import notify_sender
from notify_testkit import _attach, _intent, _msg, _patient, led

__all__ = ["led"]


PAYLOAD = "@everyone @here <@U123> <@&456> <!subteam^S123> <https://example.invalid|確認> [確認](https://example.invalid)"


def _assert_plain(text):
    for syntax in ("@everyone", "@here", "<@", "<!subteam", "<https://", "]("):
        assert syntax not in text
    assert "＠everyone" in text
    assert "［MCS" not in text  # Keep the generated notice header unchanged.


@pytest.mark.parametrize("field", ["body_html", "sender_name", "organization", "profession", "patient_name", "summary", "attachment_name"])
def test_message_fields_are_plain_at_hermes_boundary(led, monkeypatch, field):
    _patient(led)
    _msg(led, 100)
    if field == "patient_name":
        led.db.execute("UPDATE patients SET patient_name=?", (PAYLOAD,))
    elif field in {"body_html", "sender_name", "organization", "profession"}:
        value = html.escape(PAYLOAD) if field == "body_html" else PAYLOAD
        led.db.execute(f"UPDATE messages SET {field}=?", (value,))
    monkeypatch.setattr(notify_flush.structured_view, "structured_lines",
                        lambda *a, **kw: [PAYLOAD] if field == "summary" else [])
    monkeypatch.setattr(semantic_render, 'semantic_summary_block', lambda *a: "")
    monkeypatch.setattr(notify_flush, "_attachments_map", lambda *a:
                        {100: [{"name": PAYLOAD, "file_id": 1, "state": "pending"}]}
                        if field == "attachment_name" else {})
    monkeypatch.setattr(notify_flush, "_config", lambda: {"notify_target": "discord:42"})
    event = _intent(led, payload={"message_ids": [100]})
    text, files = notify_flush._format_event(led, event)
    sent = []
    def send(argv, **kwargs):
        sent.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(notify_sender.subprocess, "run", send)
    notify_sender.send(["hermes", "send", "--to", "discord:42"], text, files)
    assert len(sent) == 1
    _assert_plain(sent[0])
    assert "[MCS unread]" in sent[0]


def test_followup_and_verified_media_keep_only_trusted_control_tags(tmp_path):
    attachment = tmp_path / "file"
    attachment.write_bytes(b"synthetic")
    body = notify_sender._compose_body(
        "[MCS] 添付ファイル（後送）\n" + PAYLOAD + "\nMEDIA:/untrusted/path",
        [("report.pdf", str(attachment))])
    _assert_plain(body)
    assert "MEDIA：/untrusted/path" in body
    assert [line for line in body.splitlines() if line.startswith("MEDIA:")] == [f"MEDIA:{attachment}.pdf"]
    assert "[MCS] 添付ファイル（後送）" in body


def test_attachment_followup_filename_is_defused(led, tmp_path):
    _patient(led)
    _msg(led, 100)
    attachment = tmp_path / "downloaded"
    content = b"synthetic attachment"
    attachment.write_bytes(content)
    aid = _attach(led, 100, name=PAYLOAD, local_path=str(attachment),
                  sha256=hashlib.sha256(content).hexdigest(), nbytes=len(content))
    event = _intent(led, kind="attachment_followup", payload={"attachment_id": aid})
    text, files = notify_flush._format_event(led, event)
    body = notify_sender._compose_body(text, files)
    _assert_plain(body)
    assert "添付ファイル（後送）" in body
    assert len([line for line in body.splitlines() if line.startswith("MEDIA:")]) == 1
