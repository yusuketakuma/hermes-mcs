"""Extraction retries a result whose per-write lock wait timed out."""
import os

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
