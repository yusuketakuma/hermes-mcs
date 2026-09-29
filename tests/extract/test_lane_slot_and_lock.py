"""Extraction lane regressions: --lend-rt never pins a busy background
slot, batching is off by default, and a result whose per-write lock
wait timed out is retried in-run instead of silently discarded."""
import json
import os

import extract_llm
import local_llm
from extract_testkit import _ledger, _message

BG, RT = local_llm.BACKGROUND_SLOT, local_llm.REALTIME_SLOT


def _lend(monkeypatch, samples):
    """Arm --lend-rt with scripted /slots samples ({id: busy})."""
    monkeypatch.setattr(extract_llm, "_SLOT_OVERRIDE", None)
    monkeypatch.setattr(extract_llm, "_LEND_RT", True)
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    seen = []

    def req(*_a, **_k):
        busy = samples[min(len(seen), len(samples) - 1)]
        seen.append(busy)
        return 200, {}, json.dumps(
            [{"id": i, "is_processing": b} for i, b in busy.items()]).encode()

    monkeypatch.setattr(extract_llm, "_opener_request", req)
    sleeps = []
    monkeypatch.setattr(extract_llm.time, "sleep", sleeps.append)
    return seen, sleeps


def test_lend_both_busy_waits_then_takes_idle_background(monkeypatch):
    seen, sleeps = _lend(monkeypatch, [{BG: True, RT: True}] * 3
                         + [{BG: False, RT: True}])
    assert extract_llm._choose_slot() == BG
    assert len(seen) == 4 and len(sleeps) == 3


def test_lend_both_busy_waits_then_takes_idle_rt(monkeypatch):
    _lend(monkeypatch, [{BG: True, RT: True}, {BG: True, RT: False}])
    assert extract_llm._choose_slot() == RT


def test_lend_never_pins_busy_background_before_deadline(monkeypatch):
    """Both slots stay busy: the chooser keeps polling up to the
    deadline — it never hands back slot 0 while slot 0 is busy with
    time left (that pin is what aborts llama-server)."""
    seen, sleeps = _lend(monkeypatch, [{BG: True, RT: True}])
    clock = [1000.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda s: (sleeps.append(s),
                                   clock.__setitem__(0, clock[0] + s)))
    extract_llm._choose_slot(deadline=1030.0)
    # returned only once the deadline was reached — the call is then
    # refused before it reaches the wire
    assert clock[0] + extract_llm._LEND_POLL_S >= 1030.0
    assert len(seen) >= 29


def test_lend_probe_failure_keeps_background_fallback(monkeypatch):
    _lend(monkeypatch, [{}])

    def boom(*_a, **_k):
        raise OSError("server down")

    monkeypatch.setattr(extract_llm, "_opener_request", boom)
    assert extract_llm._choose_slot() == BG


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
