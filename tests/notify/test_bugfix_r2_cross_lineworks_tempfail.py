"""Regression: LINE WORKS CLI exit 75 (nothing accepted) retries; other failures hold."""
import json
import sys
from types import SimpleNamespace

import pytest

import notify_flush
from ledger import Ledger

ARGV = [sys.executable, "/synthetic/lineworks_adapter/__main__.py", "send"]


@pytest.mark.parametrize("returncode", [75, 1])
def test_lineworks_tempfail_is_retried_and_unknown_is_held(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_flush, "_target", lambda *a: "lineworks:synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *a: list(ARGV))
    monkeypatch.setattr(notify_flush, "_format_event", lambda *a: ("合成通知", []))
    monkeypatch.setattr(notify_flush.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=returncode, stdout="", stderr=""))
    with pytest.raises(notify_flush._SendFailed if returncode == 75
                       else notify_flush._SendUncertain):
        notify_flush._send(ARGV, "合成通知")
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
