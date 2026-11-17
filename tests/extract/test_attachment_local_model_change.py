"""A local model change keeps cloud OCR-page results and redoes local ones (fully synthetic)."""
import pytest

import attachment_extraction
import attachment_inference as worker
import attachment_inference_contract as contract
from test_attachment_inference import page_store, run
from test_anthropic_runtime import routed
from test_cloud_after_local import local_then_cloud
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['page_store', 'routed', 'local_then_cloud']

_SOURCES = {'attachment_inference': 'a' * 64}


def _token_without_cfg():
    return contract.contract(code_digest='b' * 64, source_digests=_SOURCES,
                             inference_version='1.0', extract_version=11, context_version=4)


def test_contract_ignores_local_endpoint_and_model():
    token = _token_without_cfg()
    for cfg in ({}, {'local_llm': {'model': 'synthetic-other'}},
                {'local_llm': {'url': 'http://127.0.0.1:9999/v1/chat/completions'}}):
        assert worker._contract(cfg) == worker._contract({})
    assert token == _token_without_cfg()


def _local_model(cfg, name):
    cfg['local_llm'] = {**cfg.get('local_llm', {}), 'model': name}


def test_cloud_page_result_survives_local_model_change_without_calls(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['done'] == 1
        cloud = worker.current(ledger.db, 1, key, cfg=cfg)
    assert cloud['stage'] == 'cloud' and cloud['ready']
    calls = (len(local_calls), len(cloud_calls))
    _local_model(cfg, 'synthetic-other-local')
    with legacy_cloud_routes():
        after = worker.current(ledger.db, 1, key, cfg=cfg)
        assert after['artifact_id'] == cloud['artifact_id'] and after['ready']
        assert attachment_extraction.source(ledger.db, 1, key, cfg=cfg)['extraction_artifact_id'] == cloud['artifact_id']
        assert run(ledger, cfg, 'local')['done'] == 0
        assert run(ledger, cfg, 'cloud')['done'] == 0
    assert (len(local_calls), len(cloud_calls)) == calls
    assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_local_page_result_is_redone_after_local_model_change(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    first = worker.current(ledger.db, 1, key, cfg=cfg)
    before = len(local_calls)
    _local_model(cfg, 'synthetic-other-local')
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert attachment_extraction.source(ledger.db, 1, key, cfg=cfg) is None
    assert run(ledger, cfg, 'local')['done'] == 1
    redone = worker.current(ledger.db, 1, key, cfg=cfg)
    assert len(local_calls) > before and not cloud_calls
    assert redone['artifact_id'] != first['artifact_id']
    row = ledger.db.execute('SELECT model FROM artifacts WHERE artifact_id=?', (redone['artifact_id'],)).fetchone()
    assert row['model'] == 'synthetic-other-local'


def test_local_endpoint_change_alone_keeps_the_local_result(page_store):
    ledger, cfg, key, local_calls, _cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    first = worker.current(ledger.db, 1, key, cfg=cfg)
    before = len(local_calls)
    cfg['local_llm'] = {**cfg.get('local_llm', {}), 'url': 'http://127.0.0.1:8099/v1/chat/completions'}
    assert worker.current(ledger.db, 1, key, cfg=cfg)['artifact_id'] == first['artifact_id']
    assert run(ledger, cfg, 'local')['done'] == 0 and len(local_calls) == before


def test_local_checkpoints_never_resume_across_models(page_store):
    import attachment_inference_checkpoints as checkpoints
    import original_source_keys
    ledger, cfg, key, *_ = page_store
    source = original_source_keys.source(ledger.db, 1, key)
    rows = []
    for model in ('synthetic-a', 'synthetic-b'):
        _local_model(cfg, model)
        rows.append(checkpoints._identity(source, cfg, contract=worker._contract))
    assert rows[0] != rows[1]
    cfg['local_llm']['url'] = 'http://127.0.0.1:8099/v1/chat/completions'
    assert checkpoints._identity(source, cfg, contract=worker._contract) == rows[1]


@pytest.mark.parametrize('field', ['contract', 'source_key'])
def test_cloud_result_on_a_mismatched_local_row_is_not_current(page_store, field):
    import json
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['done'] == 1
        cloud = worker.current(ledger.db, 1, key, cfg=cfg)
    local_id = cloud['local_artifact_id']
    row = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (local_id,)).fetchone()
    value = json.loads(row[0])
    value[field] = '0' * 64 if field == 'contract' else 'attachment_ocr:999:1'
    ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(value), local_id))
    with legacy_cloud_routes():
        current = worker.current(ledger.db, 1, key, cfg=cfg)
    assert current is None or current['stage'] != 'cloud'


def test_paid_unadopted_answer_is_adopted_without_resend_after_local_model_change(page_store, monkeypatch):
    import cloud_inference
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'

    def config():
        return {} if cloud_calls else cfg

    monkeypatch.setattr(cloud_inference, 'load_config', config)
    monkeypatch.setattr(worker, 'load_config', config)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(cloud_calls) == 1 and len(ledger.artifacts('cloud_usage_result')) == 1
    reserves = len(ledger.artifacts('cloud_usage_reserve'))
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    _local_model(cfg, 'synthetic-other-local')
    # the owned unit waits for a local row of the new model, then reads its paid answer
    assert run(ledger, cfg, 'local')['done'] == 1 and len(local_calls) == 2
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert len(cloud_calls) == 1 and len(ledger.artifacts('cloud_usage_reserve')) == reserves
    current = worker.current(ledger.db, 1, key, cfg=cfg)
    assert current['stage'] == 'cloud' and current['ready'] is True
