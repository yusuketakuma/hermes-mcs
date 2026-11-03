"""Realtime transport yields a borrowed writer lock without writing after fence loss."""
from contextlib import closing
import os

import pytest

import extract_llm
from extract_testkit import _ledger, _message
import writer_lock


@pytest.fixture
def locked(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    generation = [1]
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: generation[0])
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", writer_lock.updater_state())
    monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0)
    path = str(tmp_path / "run.lock")
    fd = writer_lock.acquire_run_lock(path)
    with closing(_ledger(tmp_path)) as ledger:
        ledger.save_messages([_message(body="完全合成の経過記録。")])
        try:
            yield ledger, fd, path, generation
        finally:
            os.close(fd)


@pytest.mark.parametrize("changed", [False, True])
def test_only_transport_yields_and_source_changes_cannot_be_published(locked, monkeypatch, changed):
    ledger, fd, path, _ = locked
    calls = []

    def request(_endpoint, method, _body, _timeout, _deadline):
        assert not ledger.db.in_transaction
        other = writer_lock.acquire_run_lock(path)
        assert other is not None, "local inference must not block another writer"
        os.close(other)
        calls.append(method)
        if changed:
            ledger.save_messages([_message(body="完全合成の更新後記録。")])
        return 200, {}, b"{}"

    def infer(_body, **kw):
        assert writer_lock.acquire_run_lock(path) is None
        for method in ("GET", "POST"):
            extract_llm._opener_request("http://localhost", method, {}, 1, kw["deadline"])
            assert writer_lock.acquire_run_lock(path) is None
        return {"summary": "完全合成の経過記録。"}

    monkeypatch.setattr(extract_llm.local_llm, "bounded_request", request)
    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    outcome = extract_llm.run_pending(ledger, limit=1, budget_s=120, admitted_ids={1},
                                     allow_cloud=False, run_lock_fd=fd)
    assert calls == ["GET", "POST"]
    assert outcome["done"] == (0 if changed else 1)
    assert bool(ledger.artifacts(extract_llm.KIND, message_id=1)) is not changed
    assert writer_lock.acquire_run_lock(path) is None


@pytest.mark.parametrize("updated", [False, True])
def test_lost_transport_lock_never_flushes_or_releases_claims(locked, monkeypatch, updated):
    ledger, fd, path, generation = locked
    others, observed = [], []

    def request(*_args):
        if updated:
            generation[0] += 1
        else:
            others.append(writer_lock.acquire_run_lock(path))
            assert others[-1] is not None
        observed.append(ledger.db.total_changes)
        return 200, {}, b"{}"

    def infer(_body, **kw):
        extract_llm._opener_request("http://localhost", "POST", {}, 1, kw["deadline"])
        pytest.fail("lost writer fence must leave inference immediately")

    monkeypatch.setattr(extract_llm.local_llm, "bounded_request", request)
    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    try:
        with pytest.raises(writer_lock.RunLockLost) as caught:
            extract_llm.run_pending(ledger, limit=1, budget_s=120, admitted_ids={1},
                                    allow_cloud=False, run_lock_fd=fd)
        assert caught.value.held is updated
        assert observed and ledger.db.total_changes == observed[-1]
        assert ledger.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 1
        assert not ledger.artifacts(extract_llm.KIND, message_id=1)
    finally:
        for other in others:
            os.close(other)


@pytest.mark.parametrize("options", [{"workers": 2}, {"allow_cloud": True},
                                      {"per_write_lock": True}, {"cloud_only": True}])
def test_borrowed_lock_rejects_parallel_or_cloud_dispatch_before_writes(locked, options):
    ledger, fd, _, _ = locked
    before = ledger.db.total_changes
    with pytest.raises(ValueError, match="borrowed_run_lock_requires_serial_local"):
        extract_llm.run_pending(ledger, **{"allow_cloud": False, **options, "run_lock_fd": fd})
    assert ledger.db.total_changes == before
