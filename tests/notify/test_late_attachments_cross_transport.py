"""Late downloads join the original Discord thread / LINE WORKS room like Slack."""
import hashlib
import json
from pathlib import Path

import pytest

import ledger
import notify_cmds
import notify_flush
import notify_spool
import notify_sender
import mcs_util
from adapters.common import envelopes
from common_testkit import ledger_fixture
from notify_lineworks_testkit import LINEWORKS, MID as LW_MID
from notify_testkit import CFG as DISCORD, NOW, _dispatch, _intent, _latest_render, _seed_thread

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


led = ledger_fixture(data_dir=True)

# (config, card message id, thread part remote id) per transport. LINE WORKS
# has no Bot thread API: its logical thread part reports the card message.
TRANSPORTS = {
    "discord": (DISCORD, "9001", "thread-9001"),
    "lineworks": (LINEWORKS, LW_MID, LW_MID),
}


def _deliver(led, cfg, card_mid, thread_rid, attachment_result="delivered",
             thread_result="delivered"):
    """Grant and settle the latest render through the real command path."""
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    claim = {"spec": spec, "attempt_id": f"{render['render_rev']:016x}",
             "worker_id": "b" * 16, "payload_hash": render["payload_hash"]}

    def apply(req):
        assert notify_cmds.validate_int(req) is None
        return notify_cmds.dispatch(led, req, cfg, notify_spool.data_root(led), now=NOW)

    assert apply(envelopes.transport_begin(claim))["granted"]
    card = spec["parts"]["manifest"][0]
    assert apply(envelopes.transport_receipt(
        claim, "delivered", message_id=card.get("prior_remote_id") or card_mid))["applied"]
    for part in spec["parts"]["manifest"][1:]:
        if part["kind"] == "thread" and thread_result != "delivered":
            # A rejected thread holds every dependent part without an attempt.
            assert apply(envelopes.part_receipt(
                claim, part, thread_result, error_code="synthetic_rejection"))["applied"]
            break
        if part["kind"] == "attachment_part":
            result, rid = attachment_result, "file:" + part["part_id"]
        else:
            result, rid = "delivered", thread_rid if part["kind"] == "thread" else "rid:" + part["part_id"]
        error = None if result == "delivered" else "synthetic_rejection"
        assert apply(envelopes.part_receipt(
            claim, part, result, remote_id=rid if result == "delivered" else None,
            error_code=error))["applied"]
    return spec


def _late_file(led, monkeypatch, transport, thread_result="delivered"):
    cfg, card_mid, thread_rid = TRANSPORTS[transport]
    monkeypatch.setattr(ledger.time, "time", lambda: NOW)
    _seed_thread(led)
    with led.db:
        aid = led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,state) "
            "VALUES(100,'synthetic-late','synthetic.pdf','pending')").lastrowid
    event = _intent(led)
    assert _dispatch(led, event, cfg)["dispatched"]
    first = _deliver(led, cfg, card_mid, thread_rid, thread_result=thread_result)
    assert not [p for p in first["parts"]["manifest"] if p["kind"] == "attachment_part"]
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event["event_id"],)).fetchone()[0] == "accepted"
    source = Path(notify_spool.data_root(led)) / "attachments" / str(aid)
    source.parent.mkdir(exist_ok=True)
    blob = b"completely synthetic late PDF"
    source.write_bytes(blob)
    monkeypatch.setattr(ledger.time, "time", lambda: NOW + 10)
    led.attachment_saved(aid, str(source), len(blob), hashlib.sha256(blob).hexdigest())
    followup = led.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='attachment_followup'").fetchone()
    assert followup is not None
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(mcs_util, "hermes_exe", lambda *_: "/bin/sh")

    def no_legacy(*args, **kwargs):
        raise AssertionError("late interactive file must use its bound card thread")

    monkeypatch.setattr(notify_sender, "send", no_legacy)
    return cfg, card_mid, thread_rid, aid, blob, int(followup[0])


def _state(led, event_id):
    return led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                          (event_id,)).fetchone()[0]


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_late_file_joins_original_card_thread_and_completes_on_exact_receipt(
        led, monkeypatch, transport):
    cfg, card_mid, thread_rid, aid, blob, event_id = _late_file(led, monkeypatch, transport)
    notify_flush.flush(led)
    assert _state(led, event_id) != "accepted"
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    assert spec["op"] == "update"
    files = [p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part"]
    assert [p["attachment_id"] for p in files] == [aid]
    assert files[0]["sha256"] == hashlib.sha256(blob).hexdigest()
    if transport == "lineworks":
        # A non-editable room must not get a second copy of the same card.
        assert spec["parts"]["manifest"][0]["prior_remote_id"] == LW_MID
    _deliver(led, cfg, card_mid, thread_rid)
    notify_flush.flush(led)
    assert _state(led, event_id) == "accepted"
    card = led.db.execute("SELECT thread_id,thread_state FROM notification_cards").fetchone()
    assert tuple(card) == (thread_rid, "created")


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_late_file_rejection_never_becomes_accepted(led, monkeypatch, transport):
    cfg, card_mid, thread_rid, _, _, event_id = _late_file(led, monkeypatch, transport)
    notify_flush.flush(led)
    _deliver(led, cfg, card_mid, thread_rid, attachment_result="not_sent")
    notify_flush.flush(led)
    assert _state(led, event_id) != "accepted"


def test_discord_without_card_thread_keeps_text_file_delivery(tmp_path, monkeypatch):
    cfg = {**DISCORD, "notify": {**DISCORD["notify"], "card_thread": False}}
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    try:
        source = tmp_path / "synthetic.pdf"
        source.write_bytes(b"completely synthetic PDF")
        event_id = db.outbox_add("attachment_followup", 70, {"message_id": 1})
        monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
        monkeypatch.setattr(mcs_util, "hermes_exe", lambda *_: "/bin/sh")
        monkeypatch.setattr(notify_sender, "target", lambda *_: "discord:42")
        monkeypatch.setattr(notify_flush, "_format_event",
                            lambda *_: ("synthetic file notice", [(source.name, str(source))]))
        calls = []
        monkeypatch.setattr(notify_sender, "send",
                            lambda argv, content, files=None, **kw: calls.append(files))
        assert notify_flush.flush(db)["sent"] == 1
        assert calls == [[(source.name, str(source))]]
        state = db.db.execute("SELECT state,route FROM notify_outbox WHERE event_id=?",
                              (event_id,)).fetchone()
        assert tuple(state) == ("accepted", "text")
    finally:
        db.close()


def test_discord_rejected_thread_keeps_legacy_file_fallback(led, monkeypatch):
    cfg, _, _, aid, blob, event_id = _late_file(
        led, monkeypatch, "discord", thread_result="not_sent")
    assert led.db.execute("SELECT thread_state FROM notification_cards").fetchone()[0] == "failed"
    monkeypatch.setattr(notify_sender, "target", lambda *_: "discord:42")
    calls = []
    monkeypatch.setattr(notify_sender, "send",
                        lambda argv, content, files=None, **kw: calls.append(files))
    notify_flush.flush(led)
    assert len(calls) == 1 and [hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                for _, p in calls[0]] == [hashlib.sha256(blob).hexdigest()]
    assert tuple(led.db.execute("SELECT state,route FROM notify_outbox WHERE event_id=?",
                                (event_id,)).fetchone()) == ("accepted", "text")
    assert not led.db.execute("SELECT 1 FROM notification_intent_batches WHERE event_id=?",
                              (event_id,)).fetchone()
