"""Regression: transient pre-send refusals hold on elapsed time, not on a retry count."""
import json
import sys
from types import SimpleNamespace

import pytest

import notify_flush
import notify_sender
from ledger import Ledger

STANDALONE = [sys.executable, "/synthetic/mcs_standalone/__main__.py", "send", "--to", "slack:C1"]
LINEWORKS = [sys.executable, "/synthetic/lineworks_adapter/__main__.py", "send"]


def _flush_once(tmp_path, monkeypatch, argv, stderr, attempts, returncode=75):
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_sender, 'target', lambda *a: "slack:C1")
    monkeypatch.setattr(notify_sender, 'send_argv', lambda *a: list(argv))
    monkeypatch.setattr(notify_flush, "_format_event", lambda *a: ("合成通知", []))
    monkeypatch.setattr(notify_sender.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=returncode, stdout="", stderr=stderr))
    (tmp_path / "data").mkdir(exist_ok=True)
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    try:
        eid = led.outbox_add("run_failed", None, {})
        led.db.execute("UPDATE notify_outbox SET attempts=? WHERE event_id=?", (attempts, eid))
        led.db.commit()
        notify_flush.flush(led)
        row = led.db.execute("SELECT state,attempts,next_try,progress FROM notify_outbox "
                             "WHERE event_id=?", (eid,)).fetchone()
    finally:
        led.close()
    return row, json.loads(row["progress"] or "{}").get("hold_reason")


def test_sender_code_is_last_stderr_token(monkeypatch):
    monkeypatch.setattr(notify_sender.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=75, stdout="", stderr="log line\nnot_sent delivery_not_accepted\n"))
    with pytest.raises(notify_sender.SendFailed) as caught:
        notify_sender.send(STANDALONE, "合成通知")
    assert caught.value.code == "delivery_not_accepted"
    assert notify_flush._transient_pre_send(caught.value)
    assert not notify_flush._transient_pre_send(notify_sender.SendFailed("attachment_unavailable"))
    assert not notify_flush._transient_pre_send(notify_sender.SendBudget("send_budget_insufficient"))


def test_failing_for_is_a_lower_bound_of_the_backoff_schedule():
    assert notify_flush._transient_failing_for(0) == 0
    assert notify_flush._transient_failing_for(5) == 60 + 120 + 240 + 480 + 960
    # 5 attempts (~31 min) is far below a day; the ceiling is reached later.
    assert notify_flush._transient_failing_for(5) < notify_flush.TRANSIENT_HOLD_AFTER_S
    first = next(n for n in range(200) if notify_flush._transient_failing_for(n)
                 >= notify_flush.TRANSIENT_HOLD_AFTER_S)
    assert 20 < first < 40


@pytest.mark.parametrize(("argv", "stderr"), [
    (STANDALONE, "not_sent delivery_not_accepted\n"),
    (STANDALONE, "not_sent slack_identity_unreachable\n"),
    (STANDALONE, "not_sent discord_unreachable\n"),
    (STANDALONE, "not_sent http_429\n"),
    (LINEWORKS, "transport_unknown\n"),
    (LINEWORKS, "rate_limited\n"),
    (LINEWORKS, "sender_busy\n"),
])
def test_network_outage_beyond_five_attempts_keeps_retrying(tmp_path, monkeypatch, argv, stderr):
    row, hold = _flush_once(tmp_path, monkeypatch, argv, stderr, attempts=8)
    assert hold is None
    assert row["state"] == "failed" and row["next_try"] is not None
    assert row["attempts"] == 9


def test_transient_failure_holds_after_a_day(tmp_path, monkeypatch):
    attempts = next(n for n in range(200) if notify_flush._transient_failing_for(n)
                    >= notify_flush.TRANSIENT_HOLD_AFTER_S)
    row, hold = _flush_once(tmp_path, monkeypatch, STANDALONE,
                            "not_sent delivery_not_accepted\n", attempts=attempts)
    assert hold == "pre_send_failure" and row["next_try"] is None


@pytest.mark.parametrize("stderr", ["credentials_invalid\n", "http_error\n", "",
                                    "not_sent slack_identity_unverified\n",
                                    "not_sent loginfailure\n"])
def test_auth_or_config_refusal_keeps_the_attempt_cap(tmp_path, monkeypatch, stderr):
    row, hold = _flush_once(tmp_path, monkeypatch, LINEWORKS, stderr, attempts=4)
    assert hold == "pre_send_failure" and row["next_try"] is None


def test_unknown_outcome_is_still_held_at_once(tmp_path, monkeypatch):
    row, hold = _flush_once(tmp_path, monkeypatch, STANDALONE, "transport_unknown\n",
                            attempts=0, returncode=1)
    assert hold == "send_outcome_unknown" and row["next_try"] is None
