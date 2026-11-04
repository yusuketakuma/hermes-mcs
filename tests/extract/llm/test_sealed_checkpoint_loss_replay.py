"""Exact sealed clinical payloads may retain known loss; new loss and unknown proof cannot."""
import json
import time

import pytest

import extract_llm as worker
import extraction_context as frames
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding


@pytest.fixture
def uncertain(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    body = '本人は合成薬Aを中止したかもしれません。'
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
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
    worker._capture_checkpoint_state(ledger.db, row, None)
    candidate = worker.extract_validation.validate_piece({'meds': [{'name': '合成薬A', 'dose': None,
        'status': 'current', 'subject': 'patient', 'negated': False, 'action': 'stop', 'evidence': body}]}, body, body)
    assert candidate['_evidence_dropped'] == 2 and 'negated' not in candidate['meds'][0]
    try:
        yield ledger, row, lease, candidate
    finally:
        ledger.close()


@pytest.mark.parametrize('fault', ['missing_loss', 'understated_loss', 'invalid_loss', 'clinical_change',
                                 'missing_seal', 'changed_seal', 'unknown_provider', 'wrong_model', 'wrong_source'])
def test_repeated_loss_never_authorizes_new_clinical_loss_or_unknown_proof(uncertain, fault):
    ledger, row, lease, candidate = uncertain
    if fault == 'missing_loss':
        candidate.pop('_evidence_dropped')
    elif fault == 'understated_loss':
        candidate['_evidence_dropped'] = 0
    elif fault == 'invalid_loss':
        candidate['_evidence_dropped'] = True
    elif fault == 'clinical_change':
        candidate['meds'][0]['dose'] = '123mg'
        checked = worker.extract_validation.validate(candidate, row['body_text'])
        # Even no net increase in the loss count cannot hide a changed fact.
        assert checked['_evidence_dropped'] <= candidate['_evidence_dropped']
        assert checked['meds'][0]['dose'] is None
    quality = worker._hint_coverage(candidate, worker._rule_hints(row), row['body_text'])
    value = worker._EvidenceLeaf({**candidate, '_quality_result': quality}, worker._provider_evidence(['local'], True))
    assert worker._persist_chunks(ledger, row, {0: value}, None, chunk_size=row['_chunk_size'],
        lease=lease, guard=row['_checkpoint_guard'])
    raw = ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(raw['meta'])
    if fault == 'missing_seal':
        meta.pop('provider_leaf')
    elif fault == 'changed_seal':
        meta['provider_leaf']['result_hash'] = '0' * 64
    elif fault == 'unknown_provider':
        meta['provider_leaf']['providers'] = ['unobserved']
    elif fault == 'wrong_source':
        meta['provider_leaf']['source_binding'] = '0' * 64
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=?, model=? WHERE artifact_id=?',
            (json.dumps(meta), 'different-synthetic-model' if fault == 'wrong_model' else raw['model'], raw['artifact_id']))
    before = [tuple(item) for item in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    _, saved = worker._saved_chunk_state(ledger.db, row, None, chunk_size=row['_chunk_size'])
    assert saved == {}
    assert [tuple(item) for item in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_started_uncertainty_stays_unknown_without_another_dispatch(uncertain):
    ledger, row, lease, candidate = uncertain
    value = worker._EvidenceLeaf({**candidate, '_quality_status': 'started'}, worker._provider_evidence(['local'], True))
    assert worker._persist_chunks(ledger, row, {0: value}, None, chunk_size=row['_chunk_size'],
        lease=lease, guard=row['_checkpoint_guard'])
    before = [tuple(item) for item in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    _, saved = worker._saved_chunk_state(ledger.db, row, None, chunk_size=row['_chunk_size'])
    assert saved[0]['_quality_status'] == 'started'
    assert saved[0]['_evidence_dropped'] == candidate['_evidence_dropped']
    def no_dispatch(*args, **kwargs):
        pytest.fail('an unknown repair must not be resent')
    meta = {}
    result = worker.llm_extract(row['body_text'], hints=worker._rule_hints(row), chunks_in=saved,
        provider=no_dispatch, meta_out=meta, deadline=time.monotonic() + 180)
    assert isinstance(result, dict) and result['meds'][0]['unverified'] is True
    assert meta['completeness']['0']['state'] == 'dispatch_unknown'
    assert [tuple(item) for item in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
