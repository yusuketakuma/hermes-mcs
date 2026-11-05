"""Synthetic OCR pages use the real clinical validator and guarded provider lifecycle."""
import json
import time

import pytest

import attachment_inference as worker
import attachment_ocr
import original_source_keys
from test_anthropic_runtime import routed
from test_cloud_after_local import local_then_cloud

__all__ = ['routed', 'local_then_cloud']


@pytest.fixture
def page_store(local_then_cloud, monkeypatch):
    ledger, cfg, local_calls, cloud_calls, answers = local_then_cloud
    ledger.ensure_patient(1)
    cfg['attachment_ocr'] = {'enabled': True, 'executable': '/synthetic/yomitoku'}
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.extract_llm, '_context_memory_available', lambda: True)
    monkeypatch.setattr(worker.extract_llm, '_writer_current', lambda **kw: True)
    monkeypatch.setattr(worker.writer_lock, 'wait_run_lock',
                        lambda path, **kw: worker.writer_lock.acquire_run_lock(path))
    ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                      "VALUES(1,1,'downloaded',?,32,1)", ('a' * 64,))
    pages = [{'page': 1, 'engine': 'pdf_text', 'text': '完全合成の経過記録です。'}]
    for page in pages:
        page['text_hash'] = attachment_ocr._hash(page['text'])
    doc = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
           'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION,
           'state': 'complete', 'attachment_id': 1, 'input_sha256': 'a' * 64,
           'input_bytes': 32, 'pages': pages,
           'text_hash': attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
    artifact = ledger.artifact_add(attachment_ocr.KIND, json.dumps(doc), project_id=1,
                                   message_id=1, meta={'attachment_id': 1})
    assert attachment_ocr.current(ledger.db, 1, project_id=1) is not None
    return ledger, cfg, f'attachment_ocr:{artifact}:1', local_calls, cloud_calls, answers


def run(ledger, cfg, stage):
    # Advancing past EOF takes one bounded idle scan; never redo a completed call.
    result = worker.run_pending(ledger, cfg, stage=stage, deadline=time.monotonic() + 300)
    if not result['done'] and not result['deferred']:
        result = worker.run_pending(ledger, cfg, stage=stage, deadline=time.monotonic() + 300)
    return result


def test_ocr_local_then_cloud_real_accounting_is_separate_from_chat(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    before = tuple(ledger.db.execute('SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone())
    assert run(ledger, cfg, 'local')['done'] == 1
    local = worker.current(ledger.db, 1, key, cfg=cfg)
    assert local['ready'] and local['stage'] == 'local' and local_calls and not cloud_calls
    cfg['cloud_llm']['mode'] = 'on'
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False
    assert run(ledger, cfg, 'cloud')['done'] == 1
    cloud = worker.current(ledger.db, 1, key, cfg=cfg)
    assert cloud['ready'] and cloud['stage'] == 'cloud'
    assert cloud['local_artifact_id'] == local['artifact_id']
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert not ledger.artifacts(worker.extract_llm.KIND)
    assert tuple(ledger.db.execute('SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone()) == before
    assert run(ledger, cfg, 'cloud')['done'] == 0 and len(cloud_calls) == 1


def test_stale_and_forged_results_never_complete(page_store):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    row = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (result['artifact_id'],)).fetchone()
    doc = json.loads(row[0])
    doc['document'] = {}
    doc['result_hash'] = worker.payload_hash({})
    ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), result['artifact_id']))
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    ledger.db.rollback()
    ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None


def test_recursive_dispatch_and_post_response_source_loss(page_store):
    ledger, cfg, key, *_ = page_store
    source = original_source_keys.source(ledger.db, 1, key)
    dispatch = worker._dispatcher(ledger, source, cfg, worker.writer_lock.updater_state())
    assert dispatch(lambda: dispatch(lambda: 'nested')) == 'nested'
    ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
    ledger.db.commit()
    calls = []
    assert dispatch(lambda: calls.append('accounted'), result=True) is None
    assert calls == ['accounted']
    with pytest.raises(ValueError, match='source_changed'):
        dispatch(lambda: None)


@pytest.mark.parametrize('free', [None, 0])
def test_unverified_or_low_disk_never_starts_inference(page_store, monkeypatch, free):
    ledger, cfg, _key, local_calls, cloud_calls, _ = page_store
    monkeypatch.setattr(worker.extract_llm, 'disk_free_mb', lambda db: free)
    assert run(ledger, cfg, 'local')['deferred'] == 1
    assert not local_calls and not cloud_calls and not ledger.artifacts(worker.KIND)
