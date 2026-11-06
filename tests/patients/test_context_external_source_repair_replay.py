"""Paid external-original repairs replay without derived inputs or another dispatch."""
import json

import pytest

import anthropic_client
import attachment_extraction
import mcs_queries
import cloud_policy
import openai_client
import original_source_keys
import patient_context_artifacts as artifacts
import patient_context_refresh_state as state
from anthropic_context_testkit import (
    _REPAIR_BED, _REPAIR_HOME, _SOURCE_REPAIR_CLIENT, cloud, runtime, source_repair,
)
from test_context_cloud_update_identity import finish, real_cloud, receipts
from test_context_summary_sources import _ocr_local
import test_ocr_local_context_pipeline as ocr_testkit
from test_patient_context_refresh import _raw_proof, _semantic_proof, seed

__all__ = ['cloud', 'runtime', 'source_repair', 'real_cloud']


@pytest.fixture(params=['karte', 'attachment_ocr'])
def paid_external(runtime, source_repair, request, monkeypatch):
    # Restore the real provider and writer checks after the independent Jev oracle.
    monkeypatch.setattr(anthropic_client, 'AnthropicClient', _SOURCE_REPAIR_CLIENT)
    cfg, posts = request.getfixturevalue('real_cloud')
    settings, errors = cloud_policy.cloud_config({'cloud_llm': {
        'mode': 'on', 'provider': 'openai', 'project_ids': [1],
        'routes': ['patient_context'], 'daily_request_budget': 100,
        'profiles': {'patient_context': 'luna-medium', 'quality_repair': 'sol-high'}}})
    assert not errors
    cfg['cloud_llm'].clear()
    cfg['cloud_llm'].update(settings)
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    assert cloud_policy.resolve_profile(cfg, route='patient_context', purpose='quality_repair')[0]['provider'] == 'openai'
    ledger, _, _ = runtime
    body = _REPAIR_HOME + _REPAIR_BED
    seed(ledger, body=body)
    if request.param == 'karte':
        with ledger.db:
            ledger.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
            aid = ledger.artifact_add('karte_summary', json.dumps({'comment': body}),
                project_id=1, meta={'karte_id': 99})
        key = f'karte:{aid}'
    else:
        monkeypatch.setattr(ocr_testkit, 'OCR_TEXT', body)
        key = ocr_testkit._retain_ocr(ledger)
        with ledger.db:
            _ocr_local(ledger, key, cfg, '合成OCRの補助要約です。')
        assert attachment_extraction.current(ledger.db, 1, key, cfg=cfg)['ready']
        _raw_proof(ledger, 1, 1)
        _semantic_proof(ledger, 1, 1, cfg=cfg)
    monkeypatch.setattr(anthropic_client, 'AnthropicClient', _SOURCE_REPAIR_CLIENT)

    def transport(_client, method, request_body, timeout, *, deadline):
        assert method == 'POST' and timeout > 0 and not ledger.db.in_transaction
        payload = json.loads(request_body['input'])
        posts.append(payload)
        if 'source' in payload:
            texts = (_REPAIR_HOME, _REPAIR_BED) if 'source_quality_findings' in payload else (_REPAIR_HOME,)
            answer = {'patient_context': [{'category': 'living', 'subject': 'patient',
                'text': text, 'evidence': text, 'details': []} for text in texts if text in payload['source']]}
        else:
            answer = {'fact_ids': [report['id'] for report in payload['reports'][:12]]}
        value = {'status': 'completed', 'model': request_body['model'], 'service_tier': 'default',
            'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': json.dumps(answer)}]}],
            'usage': {'input_tokens': 2, 'output_tokens': 2, 'total_tokens': 4,
                      'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}
        return 200, {}, json.dumps(value).encode()

    monkeypatch.setattr(openai_client.OpenAIClient, 'request', transport)
    finish(runtime, cfg)
    header = next(row for row in ledger.artifacts(state.SOURCE_REPAIR, project_id=1)
                  if json.loads(row['meta'])['source_key'] == key)
    rows = [dict(row) for row in ledger.artifacts(state.SOURCE_REPAIR_CANDIDATE, project_id=1)
            if json.loads(row['meta'])['source_key'] == key]
    assert rows and 'original_source_sig' in json.loads(header['meta'])
    assert 'repair_provider' in json.loads(header['meta']), json.loads(header['meta'])
    assert any('source_quality_findings' in payload for payload in posts)
    return ledger, cfg, posts, key, json.loads(header['meta']), rows


@pytest.mark.parametrize('tamper', [None, 'binding', 'scope', 'receipt'])
def test_external_paid_repair_replays_original_only_and_rejects_tampering(paid_external, tamper):
    ledger, cfg, posts, key, origin, rows = paid_external
    before, sent = receipts(ledger), len(posts)
    original = original_source_keys.source(ledger.db, 1, key)
    assert 'items' not in original
    # A later interpretation and today's disabled OCR config are not original inputs.
    cfg['attachment_ocr'] = {'enabled': False}
    if original['source'] == 'attachment_ocr':
        assert mcs_queries.patient_context_source(ledger.db, 1, key, cfg=cfg) is None
    with ledger.db:
        ledger.artifact_add('extract_llm', json.dumps({'summary': '後から作られた合成補助要約'}),
            project_id=1, message_id=1, model='synthetic-local', meta={'hash': 'stale'})
        if original['source'] == 'attachment_ocr':
            new_summary = _ocr_local(ledger, key, cfg, '後から作られた合成OCR補助要約です。')
            assert attachment_extraction.current(ledger.db, 1, key, cfg=cfg)['artifact_id'] == new_summary
        if tamper == 'binding':
            if original['source'] == 'attachment_ocr':
                ledger.db.execute('UPDATE attachments SET sha256=? WHERE attachment_id=?',
                                  ('b' * 64, original['attachment_id']))
            else:
                ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                    (json.dumps({'karte_id': 100}), original['source_artifact_id']))
        elif tamper == 'scope':
            ledger.db.execute('UPDATE patients SET karte_id=100 WHERE project_id=1')
        elif tamper == 'receipt':
            row = rows[0]
            content = json.loads(row['content'])
            content['__mcs_internal_proof_v1']['receipt_sig'] = '0' * 64
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                (json.dumps(content), row['artifact_id']))
            rows[0] = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?',
                                           (row['artifact_id'],)).fetchone())
    ledger.db.execute('BEGIN')
    try:
        if tamper:
            with pytest.raises(ValueError):
                artifacts.verify_source_repair_rows(ledger.db, 1, key, origin, rows)
            with pytest.raises(ValueError):
                artifacts.selected_rows(ledger.db, 1, key, source_binding=origin['source_binding'],
                                        body_size=original['body_length'])
        else:
            artifacts.verify_source_repair_rows(ledger.db, 1, key, origin, rows)
            selected = artifacts.selected_rows(ledger.db, 1, key, source_binding=origin['source_binding'],
                                        body_size=original['body_length'])
            assert {row['artifact_id'] for row in selected} == {row['artifact_id'] for row in rows}
            assert {fact['text'] for row in selected for fact in json.loads(row['content'])['facts']} == {
                _REPAIR_HOME, _REPAIR_BED}
        assert ledger.db.in_transaction
    finally:
        ledger.db.rollback()
    assert receipts(ledger) == before and len(posts) == sent
