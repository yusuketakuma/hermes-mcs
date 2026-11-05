"""Synthetic rejected saves retain only a finite owner-bound selection wait."""
import extract_llm_carry
import extract_llm_context
import extract_llm_errors
import extract_llm_runner
import extract_llm_store
import json

import pytest

import extract_llm as worker
from extract_testkit import _ledger, _message
from test_anthropic_runtime import old, routed as routed
from test_lane_slot_and_lock import _lock_stub


def claim_rows(ledger):
    return [dict(row) for row in ledger.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='extract_claim' ORDER BY message_id")]


@pytest.mark.parametrize('path', ['normal', 'tail', 'carry'])
def test_rejected_save_yields_next_pass_to_healthy_source(routed, monkeypatch, tmp_path, path):
    ledger, _cfg, calls, _reserves, _results = routed
    old(ledger, mid=1, pid=2)
    old(ledger, mid=2, pid=2)
    atomic = worker._commit_extraction_atomic
    rejected = []
    def reject_head(db, row, *args, **kwargs):
        if row['message_id'] == 2:
            assert worker._persist_chunks(db, row, {0: json.loads(args[0])}, args[1],
                chunk_size=row['_chunk_size'], lease=row['_current_lease'], guard=row['_checkpoint_guard'])
            row['_checkpoint_conflict'] = True
            rejected.append(row)
        return atomic(db, row, *args, **kwargs)
    monkeypatch.setattr(worker, '_commit_extraction_atomic', reject_head)
    monkeypatch.setattr(extract_llm_runner, '_commit_extraction_atomic', worker._commit_extraction_atomic)
    lock = _lock_stub(monkeypatch, tmp_path,
                      blocked_after_call=None if path == 'carry' else 60 if path == 'tail' else 0)
    options = {'per_write_lock': True} if path != 'normal' else {}
    if path == 'carry':
        options.update(write_wait_s=1, carry=[])
    first = worker.run_pending(ledger, limit=1, budget_s=180, **options)
    if path == 'carry':
        assert first['done'] == first['deferred'] == 0 and len(first['carry']) == 1
        lock['blocked'] = 0
        settled, count, _pids = worker._settle_carry(ledger, first['carry'], report=False)
        assert count == 1 and not first['carry']
        assert settled['deferred'] == 1 and settled['defer_reasons'] == {'commit_checkpoint': 1}
    else:
        assert first['done'] == first['failed'] == 0 and first['deferred'] == 1
        assert first['defer_reasons'] == {'commit_checkpoint': 1}
    assert len(rejected) == 1
    waiting = claim_rows(ledger)
    assert len(waiting) == 1 and waiting[0]['message_id'] == 2
    now = worker.time.time()
    assert now < waiting[0]['next_try'] <= now + worker._PREPARE_HOLD_S
    evidence = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE message_id=2 ORDER BY artifact_id")]
    assert any(row['kind'] == 'extract_llm_chunk' for row in ledger.db.execute(
        'SELECT kind FROM artifacts WHERE message_id=2'))
    lock['blocked'] = 0
    second = worker.run_pending(ledger, limit=1, budget_s=180)
    assert second['done'] == 1 and second['failed'] == second['deferred'] == 0
    assert len(rejected) == 1 and not calls
    assert claim_rows(ledger) == waiting
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE message_id=2 ORDER BY artifact_id")] == evidence


@pytest.mark.parametrize('boundary', ['owner', 'lease', 'source', 'expired', 'expired_after_owner', 'missing_binding', 'normal'])
def test_release_hold_never_fences_a_foreign_or_changed_occurrence(tmp_path, monkeypatch, boundary):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body='完全合成の対象本文')])
        row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
        lease = worker._claim(ledger, row)
        assert lease is not None
        row['_commit_hold'] = boundary != 'normal'
        # These opaque synthetic receipts/checkpoints must not be touched by selection bookkeeping.
        for kind in ('extract_llm_chunk', 'cloud_usage_reserve', 'cloud_usage_result'):
            ledger.artifact_add(kind, '{}', project_id=1, message_id=1, meta={'synthetic': True})
        evidence = [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts')]
        if boundary == 'owner':
            row['_claim_token'] = 'synthetic-foreign-token'
        elif boundary == 'lease':
            lease += 1
        elif boundary == 'source':
            with ledger.db:
                ledger.db.execute("UPDATE messages SET sender_name='synthetic-new-occurrence' WHERE message_id=1")
        elif boundary == 'expired':
            monkeypatch.setattr(worker.time, 'time', lambda: lease + 1)
        elif boundary == 'expired_after_owner':
            now = [worker.time.time()]
            owns = worker._owns_current_source
            def expires_after_check(*args):
                current = owns(*args)
                assert current
                now[0] = lease + 1
                return current
            monkeypatch.setattr(worker.time, 'time', lambda: now[0])
            monkeypatch.setattr(worker, '_owns_current_source', expires_after_check)
            monkeypatch.setattr(extract_llm_carry, '_owns_current_source', worker._owns_current_source)
            monkeypatch.setattr(extract_llm_context, '_owns_current_source', worker._owns_current_source)
            monkeypatch.setattr(extract_llm_errors, '_owns_current_source', worker._owns_current_source)
            monkeypatch.setattr(extract_llm_runner, '_owns_current_source', worker._owns_current_source)
            monkeypatch.setattr(extract_llm_store, '_owns_current_source', worker._owns_current_source)
        elif boundary == 'missing_binding':
            row.pop('_source_binding')
        before = claim_rows(ledger)
        worker._release(ledger, row, lease)
        assert claim_rows(ledger) == (before if boundary in ('owner', 'lease') else [])
        assert [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts')] == evidence
        assert not ledger.db.in_transaction and '_commit_hold' not in row
    finally:
        ledger.close()


@pytest.mark.parametrize('outcome', ['written', 'held', 'current'])
def test_nonrejected_commit_does_not_keep_a_stale_hold_intent(routed, monkeypatch, outcome):
    ledger, _cfg, _calls, _reserves, _results = routed
    old(ledger, pid=2)
    monkeypatch.setattr(worker, 'llm_extract', lambda *args, **kwargs: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    def completed(_ledger, row, *args, **kwargs):
        assert '_commit_hold' not in row
        return outcome
    prepare = worker._prepare_jobs
    def stale_intent(*args, **kwargs):
        jobs, skipped, blocked = prepare(*args, **kwargs)
        for row, *_rest in jobs:
            row['_commit_hold'] = True
        return jobs, skipped, blocked
    monkeypatch.setattr(worker, '_prepare_jobs', stale_intent)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', worker._prepare_jobs)
    monkeypatch.setattr(worker, '_commit_extraction_atomic', completed)
    monkeypatch.setattr(extract_llm_runner, '_commit_extraction_atomic', worker._commit_extraction_atomic)
    result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert result['deferred'] == 0 and claim_rows(ledger) == []
