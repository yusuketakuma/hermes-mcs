"""Synthetic bounded local passes resume exact OCR chunks without publishing partial work."""
import json

import pytest

import attachment_inference as worker
import attachment_ocr as ocr
import original_source_keys
from test_attachment_inference import page_store

__all__ = ["page_store"]
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401


def _long_page(ledger, key, text):
    artifact_id = original_source_keys.parse(key)[1]
    doc = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()[0])
    doc['pages'][0].update(text=text, text_hash=ocr._hash(text))
    doc['text_hash'] = ocr._hash(json.dumps(doc['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), artifact_id))


def _run(ledger, cfg):
    result = worker.run_pending(ledger, cfg, stage='local')
    if not result['done'] and not result['deferred']:
        result = worker.run_pending(ledger, cfg, stage='local')
    return result


@pytest.mark.parametrize('nonempty', [False, True], ids=['empty-clinical-result', 'clinical-facts'])
def test_120_second_local_pass_resumes_two_chunks_and_only_then_is_ready(page_store, monkeypatch, nonempty):
    ledger, cfg, key, calls, _, answers = page_store
    body = ('合成記録A。眠気はありません。\n合成記録B。頭痛はありません。\n' if nonempty
            else '完全合成の経過記録です。\n' * 4)
    _long_page(ledger, key, body)
    size = len(body) // 2
    monkeypatch.setattr(worker.extract_llm, '_CHUNK_SIZE', size)
    assert len(worker.extract_llm.plan_chunks(body, size)) == 2
    clock = [1000.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    original = worker.extract_llm.local_llm.chat
    def slow(prompt, **kwargs):
        if nonempty:
            target = prompt.rsplit('<<<', 1)[1].split('>>>')[0]
            symptom = '眠気' if '眠気' in target else '頭痛'
            answers['local'] = {'symptoms': [{'text': symptom, 'negated': True, 'subject': 'patient',
                'status': 'ongoing', 'evidence': symptom + 'はありません。'}]}
        response = original(prompt, **kwargs)
        clock[0] += 80
        return response
    monkeypatch.setattr(worker.extract_llm.local_llm, 'chat', slow)
    monkeypatch.setattr(worker.extract_llm.local_llm, 'admitted_chat', lambda client, prompt, **kw: slow(prompt, **kw))
    assert _run(ledger, cfg)['deferred'] == 1
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert ledger.artifacts(worker._CHECKPOINT_KIND)
    source = original_source_keys.source(ledger.db, 1, key)
    kwargs = worker._local_checkpoints(ledger, source, cfg, worker.writer_lock.updater_state())
    assert list(kwargs['chunks_in']) == [0]
    leaf = kwargs['chunks_in'][0]
    assert isinstance(leaf, worker.extract_llm._EvidenceLeaf)
    assert leaf.provider_evidence == {'providers': ['local'], 'complete': True}
    assert leaf['_quality_result']['state'] == 'hint_gaps_resolved'
    first_call_count = len(calls)
    assert _run(ledger, cfg)['done'] == 1
    assert len(calls) == first_call_count + 1
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is True


def test_checkpoint_split_and_pending_candidate_stay_bound_and_pending(page_store, monkeypatch):
    ledger, cfg, key, *_ = page_store
    assert _run(ledger, cfg)['done'] == 1
    source = original_source_keys.source(ledger.db, 1, key)
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    generation = worker.writer_lock.updater_state()
    checkpoint = worker._local_checkpoints(ledger, source, cfg, generation)
    leaf = worker.extract_llm._EvidenceLeaf(result['document'], {'providers': ['local'], 'complete': True})
    leaf['_quality_status'] = 'started'
    assert checkpoint['on_chunk'](0, leaf)
    restored = worker._local_checkpoints(ledger, source, cfg, generation)['chunks_in'][0]
    assert restored['_quality_status'] == 'pending'
    assert '_quality_result' not in restored
    smaller = max(1, len(source['body_text']) // 2)
    assert checkpoint['on_split'](0, smaller)
    restored = worker._local_checkpoints(ledger, source, cfg, generation)
    assert restored['splits_in'] == {0: smaller} and restored['chunks_in'] == {}
    monkeypatch.setattr(worker, '_contract', lambda cfg: 'changed-contract')
    assert worker._local_checkpoints(ledger, source, cfg, generation)['splits_in'] == {}


@pytest.mark.parametrize('field', ['piece_hash', 'value_hash', 'value', 'evidence', 'plan', 'chunk_size'])
def test_corrupt_checkpoint_cannot_authorize_replay(page_store, field):
    ledger, cfg, key, *_ = page_store
    assert _run(ledger, cfg)['done'] == 1
    row = ledger.artifacts(worker._CHECKPOINT_KIND)[-1]
    raw = json.loads(row['content'])
    raw[field] = {} if field in ('value', 'evidence') else 'corrupt'
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(raw), row['artifact_id']))
    source = original_source_keys.source(ledger.db, 1, key)
    restored = worker._local_checkpoints(ledger, source, cfg, worker.writer_lock.updater_state())
    assert restored['chunks_in'] == {} and restored['splits_in'] == {}


def _second_patient(ledger, key):
    from extract_testkit import _message
    ledger.ensure_patient(2)
    ledger.save_messages([_message(mid=2, project_id=2, body='合成の新しい投稿')], project_id=2)
    old = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                    (original_source_keys.parse(key)[1],)).fetchone()[0])
    old.update(attachment_id=2)
    old['pages'][0].update(text='最近の患者の合成OCR', text_hash=ocr._hash('最近の患者の合成OCR'))
    old['text_hash'] = ocr._hash(json.dumps(old['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE messages SET posted_at_ts=100 WHERE project_id=1')
        ledger.db.execute('UPDATE messages SET posted_at_ts=300 WHERE project_id=2')
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(2,2,'downloaded',?,32,1)", ('a' * 64,))
        ledger.artifact_add_tx(ocr.KIND, json.dumps(old), project_id=2, message_id=2, meta={'attachment_id': 2})


def test_recent_patient_priority_alternates_with_history_and_deferred_does_not_starve(page_store, monkeypatch):
    ledger, cfg, key, *_ = page_store
    _second_patient(ledger, key)
    attempted = []
    def defer(body, **kwargs):
        attempted.append(body)
        return worker.extract_llm._DEFERRED
    monkeypatch.setattr(worker.extract_llm, 'llm_extract', defer)
    assert worker.run_pending(ledger, cfg, stage='local', limit=1)['deferred'] == 1
    assert attempted == ['最近の患者の合成OCR']
    assert worker.run_pending(ledger, cfg, stage='local', limit=1)['deferred'] == 1
    assert attempted[-1] == '完全合成の経過記録です。'
    for _ in range(4):
        before = len(attempted)
        worker.run_pending(ledger, cfg, stage='local', limit=1)
        assert len(attempted) - before <= 1
    assert attempted.count('最近の患者の合成OCR') >= 2
    assert attempted.count('完全合成の経過記録です。') >= 2
    before = len(attempted)
    worker.run_pending(ledger, cfg, stage='local', deadline=worker.time.monotonic() - 1)
    assert len(attempted) == before
