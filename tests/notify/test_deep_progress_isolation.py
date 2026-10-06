"""Corrupt progress stays quarantined without blocking later notifications."""
import json
import sys

import pytest

from ledger import Ledger
import notify_flush


@pytest.mark.parametrize("archived,progress", [(False, "[" * 20000 + "]" * 20000),
                                               (True, "[" * 20000 + "]" * 20000),
                                               (True, "[]")],
                         ids=["active-deep", "archived-deep", "archived-array"])
def test_corrupt_progress_holds_and_later_event_delivers(tmp_path, monkeypatch, archived, progress):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    sent = []
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda _cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_target", lambda *_args: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *_args: ["synthetic"])
    monkeypatch.setattr(notify_flush, "_format_event", lambda *_args: ("synthetic", []))
    monkeypatch.setattr(notify_flush, "_semantic_render_state", lambda *_args: ())
    monkeypatch.setattr(notify_flush, "_send", lambda *_args, **_kwargs: sent.append(1))
    try:
        ledger.db.execute("INSERT INTO patients(project_id,patient_name,is_archived) VALUES(1,?,?)",
                          ("Synthetic", int(archived)))
        ledger.db.commit()
        bad = ledger.outbox_add("new_messages", 1, {"message_ids": [1]})
        good = ledger.outbox_add("new_messages", None, {"message_ids": [2]})
        ledger.db.execute("UPDATE notify_outbox SET progress=?,attempts=4 WHERE event_id=?", (progress, bad))
        ledger.db.commit()
        result = notify_flush.flush(ledger)
        row = ledger.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                                (bad,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
        assert json.loads(row["progress"])["invalid_progress"] == progress
        assert result["sent"] == 1 and result["failed"] == 1 and sent == [1]
        assert ledger.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (good,)).fetchone()[0] == "accepted"
        assert ledger.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 2
    finally:
        ledger.close()
