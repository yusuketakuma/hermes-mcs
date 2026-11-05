"""Synthetic persistence checks retain exact reasons without weakening completeness."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_proofs
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import copy
import json
import time

import pytest

import extract_llm as worker
import extraction_context as frames
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding


@pytest.mark.parametrize('boundary', ['shape', 'layout', 'pending', 'adopted', 'terminal', 'coverage', 'validation', 'valid'])
def test_completeness_boundary_is_fixed_and_success_clears_old_reason(monkeypatch, boundary):
    row = {'body_text': '完全合成の記録。', 'posted_at': None, '_chunk_size': 100,
           '_completeness_reject_reason': 'synthetic-private-detail'}
    claim = {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}
    saved = {0: {'summary': '合成', '_quality_result': claim}}
    integrity = {'completeness': {'0': claim}, 'completeness_chunks': 1}
    splits = {}
    if boundary == 'shape':
        integrity['completeness'] = None
        used = ()
    else:
        used = (0,)
    if boundary == 'layout':
        splits[0] = 100
    elif boundary == 'pending':
        saved[0] = {'summary': '合成', '_quality_status': 'pending'}
    elif boundary == 'adopted':
        integrity['completeness_chunks'] = 2
    elif boundary == 'terminal':
        integrity['completeness'] = {'0': {'state': 'unresolved', 'remaining_hint_candidates': 0}}
    elif boundary == 'coverage':
        saved[0]['_quality_result'] = {'state': 'unresolved', 'remaining_hint_candidates': 0}
        integrity['completeness'] = {'0': saved[0]['_quality_result']}
    elif boundary == 'validation':
        def unavailable(*args):
            raise ValueError('synthetic-private-detail')
        monkeypatch.setattr(worker, '_rule_hints', unavailable)
        monkeypatch.setattr(extract_llm_engine, '_rule_hints', worker._rule_hints)
        monkeypatch.setattr(extract_llm_prepare, '_rule_hints', worker._rule_hints)
        monkeypatch.setattr(extract_llm_proofs, '_rule_hints', worker._rule_hints)
        monkeypatch.setattr(extract_llm_runner, '_rule_hints', worker._rule_hints)
    accepted = worker._verify_completeness_claim(None, row, None, {}, integrity, used,
        saved_state=(splits, saved))
    assert accepted is (boundary == 'valid')
    if boundary == 'valid':
        assert '_completeness_reject_reason' not in row
    else:
        assert row['_completeness_reject_reason'] == 'completeness_' + boundary
        assert worker._commit_rejected(row, 'completeness') == 'rejected'
        assert row['_commit_reject_reason'] == 'commit_completeness_' + boundary
        assert 'synthetic-private-detail' not in json.dumps(row)


@pytest.mark.parametrize('body,answer,chunk_size', [
    ('記録確認をお願いします。', {'requests': [{'to': None, 'kind': 'request', 'action': '記録確認',
        'evidence': '記録確認をお願いします'}]}, None),
    ('本人は架空薬A5mgを内服中です。', {'meds': [{'name': '架空薬A', 'dose': '5mg', 'status': 'current',
        'subject': 'patient', 'action': 'none', 'negated': False, 'evidence': '架空薬A5mgを内服中'}]}, None),
    ('本人に発熱と咳があります。', {'symptoms': [{'text': '発熱', 'status': 'ongoing',
        'subject': 'patient', 'evidence': '発熱'}, {'text': '咳', 'status': 'ongoing',
        'subject': 'patient', 'evidence': '咳'}]}, None),
    ('本人の血圧120/80です。', {'vitals': {'sbp': 120, 'dbp': 80}}, None),
    ('家族自身は架空薬Aを服用しています。本人は服用していません。', {'meds': [{'name': '架空薬A',
        'status': 'current', 'subject': 'family', 'action': 'none', 'negated': False,
        'evidence': '家族自身は架空薬Aを服用しています。本人は服用していません。'}]}, None),
    ('本人に発熱があります。\n本人に咳があります。', {'symptoms': [{'text': '発熱', 'status': 'ongoing',
        'subject': 'patient', 'evidence': '発熱'}, {'text': '咳', 'status': 'ongoing',
        'subject': 'patient', 'evidence': '咳'}]}, 13),
])
def test_fresh_candidate_persists_then_revalidates_before_atomic_save(tmp_path, monkeypatch, body, answer, chunk_size):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', worker._rerender_cards)
    try:
        ledger.save_messages([_message(body=body)])
        row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
        row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=('', 'synthetic-model'),
                   qc_src=None, thin_src=None, human_src=None)
        frame = frames.prepare(ledger, row, model='synthetic-model', contract=worker._context_contract(),
            attempt={'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0},
            enabled=False, base_context=None, version=worker.EXTRACT_VERSION)
        row['_frame'] = frame
        lease = worker._claim(ledger, row)
        assert lease is not None
        if chunk_size is not None:
            monkeypatch.setattr(worker, '_CHUNK_SIZE', chunk_size)
            monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', worker._CHUNK_SIZE)
            monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', worker._CHUNK_SIZE)
            monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', worker._CHUNK_SIZE)
            monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', worker._CHUNK_SIZE)
            monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', worker._CHUNK_SIZE)
        worker._capture_checkpoint_state(ledger.db, row, frame['text'])
        def model(*args, **kwargs):
            worker._CALL_NOTES.provider_response = 'local'
            return {**json.loads(worker._PREFILTER_CONTENT), **copy.deepcopy(answer)}
        monkeypatch.setattr(worker, '_llm_call', model)
        monkeypatch.setattr(extract_llm_engine, '_llm_call', worker._llm_call)
        monkeypatch.setattr(extract_llm_runner, '_llm_call', worker._llm_call)
        meta, chunks = {}, {}
        def checkpoint(key, value):
            accepted = worker._persist_chunks(ledger, row, {key: value}, frame['text'],
                chunk_size=row['_chunk_size'], lease=lease, guard=row['_checkpoint_guard'])
            assert accepted
            return accepted
        output = worker.llm_extract(body, hints=worker._rule_hints(row), context=frame['text'],
            posted_at=row['posted_at'], meta_out=meta, chunks_out=chunks, on_chunk=checkpoint,
            chunk_size=row['_chunk_size'], deadline=time.monotonic() + 180)
        assert isinstance(output, dict)
        assert worker._verify_completeness_claim(ledger.db, row, frame['text'], output, meta, tuple(chunks)), row.get('_completeness_reject_reason')
        assert worker._commit_extraction_atomic(ledger, row, json.dumps(output, ensure_ascii=False), frame['text'],
            meta, None, None, tuple(chunks), check_current=True) == 'written', row.get('_commit_reject_reason')
    finally:
        ledger.close()


@pytest.mark.parametrize('accepted', [False, None, True])
def test_fresh_checkpoint_refusal_never_becomes_a_completed_inference(monkeypatch, accepted):
    body = '完全合成の記録です。'
    monkeypatch.setattr(worker, '_llm_call', lambda *args, **kwargs: json.loads(worker._PREFILTER_CONTENT))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', worker._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', worker._llm_call)
    meta, chunks = {}, {}
    result = worker.llm_extract(body, hints=worker._rule_hints({'body_text': body, 'posted_at': None}),
        meta_out=meta, chunks_out=chunks, on_chunk=lambda *args: accepted,
        deadline=time.monotonic() + 180)
    assert chunks
    if accepted is False:
        assert result is worker._DEFERRED
        assert worker._CALL_NOTES.defer_reason == 'checkpoint_unverified'
        assert meta['_quality_hold'] is True
    else:
        assert isinstance(result, dict)
        assert '_quality_hold' not in meta
