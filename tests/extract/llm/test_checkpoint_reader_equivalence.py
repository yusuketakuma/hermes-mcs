"""Synthetic writer/reader equivalence at the exact completeness-pending boundary."""
import json
import time

import pytest

import extract_llm as worker
import extraction_context as frames
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding


@pytest.mark.parametrize('context', [None, '完全合成の前の投稿：記録を確認してください。'])
def test_reply_only_checkpoint_is_readable_with_its_exact_context(tmp_path, monkeypatch, context):
    ledger = _ledger(tmp_path)
    body = '承知しました。'
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
    try:
        ledger.save_messages([_message(body=body)])
        row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
        row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=('', 'synthetic-model'),
                   qc_src=None, thin_src=None, human_src=None)
        frame = frames.prepare(ledger, row, model='synthetic-model', contract=worker._context_contract(),
            attempt={'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0},
            enabled=False, base_context=context, version=worker.EXTRACT_VERSION)
        row['_frame'] = frame
        lease = worker._claim(ledger, row)
        assert lease is not None
        worker._capture_checkpoint_state(ledger.db, row, frame['text'])
        def model(*args, **kwargs):
            worker._CALL_NOTES.provider_response = 'local'
            return {'symptoms': [], 'reply': {'kind': 'ack', 'evidence': body}}
        monkeypatch.setattr(worker, '_llm_call', model)
        meta, chunks = {}, {}
        def checkpoint(key, value):
            saved = worker._persist_chunks(ledger, row, {key: value}, frame['text'],
                chunk_size=row['_chunk_size'], lease=lease, guard=row['_checkpoint_guard'])
            assert saved
            return saved
        output = worker.llm_extract(body, hints=worker._rule_hints(row), context=frame['text'],
            posted_at=row['posted_at'], meta_out=meta, chunks_out=chunks, on_chunk=checkpoint,
            chunk_size=row['_chunk_size'], deadline=time.monotonic() + 180)
        assert isinstance(output, dict)
        assert ('reply' in output) is bool(context)
        _, saved = worker._saved_chunk_state(ledger.db, row, frame['text'], chunk_size=row['_chunk_size'])
        assert set(saved) == {0}
        assert ('reply' in saved[0]) is bool(context)
        assert worker._verify_completeness_claim(ledger.db, row, frame['text'], output, meta, tuple(chunks))
        assert worker._commit_extraction_atomic(ledger, row, json.dumps(output, ensure_ascii=False), frame['text'],
            meta, None, None, tuple(chunks), check_current=True) == 'written'
    finally:
        ledger.close()


@pytest.mark.parametrize('kind,body,term,status', [
    ('meds', '本人は合成薬Aを中止したかもしれません。', '合成薬A', 'current'),
    ('symptoms', '本人は発熱かもしれません。', '発熱', 'ongoing'),
])
def test_sealed_unverified_candidate_remains_readable_without_becoming_verified(
        tmp_path, monkeypatch, kind, body, term, status):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
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
        worker._capture_checkpoint_state(ledger.db, row, frame['text'])
        item = {'name' if kind == 'meds' else 'text': term, 'status': status,
                'subject': 'patient', 'negated': False, 'evidence': body}
        if kind == 'meds':
            item.update(dose=None, action='stop')
        candidate = worker.extract_validation.validate_piece({kind: [item]}, body, body)
        assert candidate[kind][0]['unverified'] is True
        assert 'negated' not in candidate[kind][0]
        assert candidate['_evidence_dropped'] > 0
        revalidated = worker.extract_validation.validate(candidate, body)
        def clinical(value):
            return {key: val for key, val in value.items()
                    if key not in ('_items_dropped', '_evidence_dropped')}

        assert clinical(candidate) == clinical(revalidated)
        quality = worker._hint_coverage(candidate, worker._rule_hints(row), body)
        assert quality['state'] == 'unresolved'
        value = worker._EvidenceLeaf({**candidate, '_quality_result': quality},
                                    worker._provider_evidence(['local'], True))
        assert worker._persist_chunks(ledger, row, {0: value}, frame['text'],
            chunk_size=row['_chunk_size'], lease=lease, guard=row['_checkpoint_guard'])
        original = [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        _, saved = worker._saved_chunk_state(ledger.db, row, frame['text'], chunk_size=row['_chunk_size'])
        integrity = {'completeness': {'0': quality}, 'completeness_chunks': 1}
        assert worker._verify_completeness_claim(ledger.db, row, frame['text'], candidate, integrity, (0,)), row.get('_completeness_reject_reason')
        assert set(saved) == {0}
        assert saved[0][kind][0]['unverified'] is True
        assert saved[0]['_evidence_dropped'] == candidate['_evidence_dropped']
        assert saved[0]['_quality_result'] == quality
        assert [tuple(saved) for saved in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == original
        integrity = {'completeness': {'0': quality}, 'completeness_chunks': 1}
        assert worker._verify_completeness_claim(ledger.db, row, frame['text'], candidate, integrity, (0,))
        row['_provider_evidence'] = value.provider_evidence
        assert worker._commit_extraction_atomic(ledger, row, json.dumps(candidate, ensure_ascii=False), frame['text'],
            integrity, None, None, (0,), check_current=True) == 'written'
        final = json.loads(ledger.artifacts(worker.KIND, message_id=1)[-1]['content'])
        assert final[kind][0]['unverified'] is True
        assert 'negated' not in final[kind][0]
        assert final['_evidence_dropped'] == candidate['_evidence_dropped']
    finally:
        ledger.close()


@pytest.mark.parametrize('kind', ['meds', 'symptoms'])
@pytest.mark.parametrize('negated', ['absent', False, True])
@pytest.mark.parametrize('unverified', [False, True])
def test_absent_negation_stays_unverified_and_explicit_values_survive(kind, negated, unverified):
    if kind == 'meds':
        body = '本人は合成薬Aを内服していません。' if negated is True else '本人は合成薬Aを内服しています。'
        item = {'name': '合成薬A', 'dose': None, 'action': 'none', 'status': 'current', 'subject': 'patient', 'evidence': body}
    else:
        body = '本人の発熱はありません。' if negated is True else '本人には発熱があります。'
        item = {'text': '発熱', 'status': 'ongoing', 'subject': 'patient', 'evidence': body}
    if negated != 'absent':
        item['negated'] = negated
    if unverified:
        item['unverified'] = True
    result = worker.extract_validation.validate({kind: [item]}, body)
    assert result is not None
    [clean] = result[kind]
    if negated == 'absent':
        assert clean['negated'] is False and clean['unverified'] is True
    else:
        assert clean['negated'] is negated
    if unverified:
        assert clean['unverified'] is True
    rechecked = worker.extract_validation.validate(result, body)
    assert rechecked == result
