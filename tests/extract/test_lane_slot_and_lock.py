"""Extraction retries a result whose per-write lock wait timed out."""
import os

import pytest

import extract_llm
from extract_testkit import _ledger, _message


def test_batch_default_off():
    assert extract_llm._BATCH_K == 0


def _lock_stub(monkeypatch, tmp_path, blocked_after_call):
    """acquire_run_lock stub: free, until the LLM call starts — then the
    tick holds it for `blocked_after_call` attempts."""
    state = {"blocked": 0}

    def acquire():
        if state["blocked"] > 0:
            state["blocked"] -= 1
            return None
        return os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR)

    def extract(body, **_kw):
        state["blocked"] = blocked_after_call
        return {"summary": "合成", "vitals": {"bt": 37.0},
                "symptoms": [{"text": "咳"}]}

    monkeypatch.setattr(extract_llm, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "llm_extract", extract)
    monkeypatch.setattr(extract_llm.time, "sleep", lambda s: None)


def test_lock_lost_result_is_written_later_in_run(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成の本文です。" * 20)])
    # the in-handler wait (60 tries) all fail; the end-of-run retry wins
    _lock_stub(monkeypatch, tmp_path, blocked_after_call=60)
    res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                  per_write_lock=True)
    assert res["lock_lost"] == 1 and res["done"] == 1
    assert res["deferred"] == 0 and res["failed"] == 0
    assert len(db.artifacts("extract_llm", message_id=1)) == 1
    db.close()


def test_lock_lost_twice_defers_without_burning_attempts(tmp_path,
                                                         monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成の本文です。" * 20)])
    _lock_stub(monkeypatch, tmp_path, blocked_after_call=10 ** 6)
    res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                  per_write_lock=True)
    assert res["lock_lost"] == 1 and res["deferred"] == 1
    assert res["done"] == 0 and res["failed"] == 0
    # no error row — the message simply stays pending
    assert db.artifacts("extract_llm", message_id=1) == []
    assert res["left"] == 1
    db.close()


def test_elastic_hold_follows_the_realtime_slot(monkeypatch):
    """Slot 2 idles while realtime decodes and for the quiet window
    after; a failed probe never holds the lane."""
    probe = {"busy": {0: True, 1: True, 2: False}}
    monkeypatch.setattr(extract_llm, "_slots_busy", lambda d: probe["busy"])
    gate = {}
    assert extract_llm._elastic_hold(gate, 100.0) is True
    probe["busy"] = {0: True, 1: False, 2: False}
    assert extract_llm._elastic_hold(gate, 100.0 + extract_llm._ELASTIC_QUIET_S - 1)
    assert not extract_llm._elastic_hold(gate, 100.0 + extract_llm._ELASTIC_QUIET_S)
    probe["busy"] = None
    assert not extract_llm._elastic_hold({}, 5.0)


@pytest.mark.parametrize("slot, expect_calls", [(2, 1), (0, 4)])
def test_resident_slot2_pauses_while_realtime_busy(monkeypatch, capsys,
                                                    slot, expect_calls):
    """Only the slot-2 worker is elastic: with realtime busy for the
    first stretch it starts no item until the quiet window passes, while
    the slot-0 worker keeps draining."""
    now = [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(extract_llm, "_slots_busy", lambda d: {
        1: now[0] < 60})
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv", [
        "extract_llm", "--all", "--slot", str(slot), "--stop-after", "200"])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    calls = []

    def run(*args, **kwargs):
        calls.append(now[0])
        now[0] += 60
        return {"done": 1, "failed": 0, "left": 1, "selected": 1}

    monkeypatch.setattr(extract_llm, "run_pending", run)
    assert extract_llm.main() == 0
    assert len(calls) == expect_calls
    if slot == 2:
        # last busy sample at t=55 (5 s polls), then 120 s quiet
        assert 55 + extract_llm._ELASTIC_QUIET_S <= calls[0] \
            < 60 + extract_llm._ELASTIC_QUIET_S + extract_llm._ELASTIC_POLL_S
        out = capsys.readouterr().out
        assert '"elastic_hold"' in out and '"elastic_resume"' in out
