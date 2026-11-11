"""Real local OCR production feeds the context-only cloud consumer without rerouting."""
import json
import time

import pytest

import attachment_extraction
import attachment_inference
import attachment_ocr
import cloud_policy
import extract_llm
import extract_llm_prepare
import extract_llm_provider
import local_llm
import mcs_queries
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from anthropic_context_testkit import cloud, runtime
from test_context_cloud_update_identity import finish, real_cloud, receipts
from test_patient_context_refresh import CFG as BASE_CFG, _raw_proof, _semantic_proof, seed, step

__all__ = ['cloud', 'runtime', 'real_cloud']

OCR_TEXT = '完全合成の添付原文です。\n' + '合成資料' * 550 + '添付の末尾です。'
LOCAL_SUMMARY = '添付だけの合成補助要約です。'


def _retain_ocr(ledger):
    pages = [{'page': 1, 'engine': 'pdf_text', 'text': OCR_TEXT,
              'text_hash': attachment_ocr._hash(OCR_TEXT)}]
    value = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
        'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION,
        'state': 'complete', 'attachment_id': 1, 'input_sha256': 'a' * 64, 'input_bytes': 32,
        'pages': pages, 'text_hash': attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
    with ledger.db:
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(1,1,'downloaded',?,32,1)", ('a' * 64,))
        aid = ledger.artifact_add(attachment_ocr.KIND, json.dumps(value), project_id=1,
                                  message_id=1, meta={'attachment_id': 1})
    assert attachment_ocr.current(ledger.db, 1, project_id=1) is not None
    return f'attachment_ocr:{aid}:1'


@pytest.mark.parametrize('pending_journal', [False, True], ids=['clean', 'unreplayable-paid-journal'])
def test_actual_local_ocr_result_becomes_full_context_input_without_individual_cloud(
        runtime, real_cloud, monkeypatch, pending_journal):
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    # The reused cloud fixture fakes this selector; use the real closed policy here.
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    monkeypatch.setattr(extract_llm_prepare, '_cloud_mode', extract_llm_provider._cloud_mode)
    monkeypatch.setattr(attachment_inference, 'load_config', lambda: cfg)
    settings, errors = cloud_policy.resolve_settings(cfg)
    assert not errors and settings['mode'] == 'on'
    assert cloud_policy.eligible(settings, 'patient_context', 1)
    for route in ('reextract', 'semantic_backlog'):
        assert not cloud_policy.eligible(settings, route, 1)
        assert extract_llm._cloud_mode(ledger, cfg, route, 1) == 'off'
    before_chat = tuple(ledger.db.execute('SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone())
    assert before_chat[0] != OCR_TEXT
    key = _retain_ocr(ledger)
    assert attachment_extraction.current(ledger.db, 1, key, cfg=cfg) is None
    assert not mcs_queries.attachment_ocr_coverage(ledger.db, 1, cfg)['complete']
    journal = attachment_ocr._data_root(ledger) / 'attachment_paid_results' / 'unreplayable'
    if pending_journal:
        journal.parent.mkdir(mode=0o700)
        journal.write_text('synthetic unreplayable journal')
    local_calls = []

    def local_chat(_owner, prompt, **kwargs):
        assert not ledger.db.in_transaction
        local_calls.append(prompt)
        assert OCR_TEXT in prompt
        return {'status': 200, 'finish_reason': 'stop',
                'text': json.dumps({'symptoms': [], 'summary': LOCAL_SUMMARY})}

    monkeypatch.setattr(local_llm, 'admitted_probe_format', lambda *args, **kwargs: 'plain')
    monkeypatch.setattr(local_llm, 'admitted_chat', local_chat)
    result = attachment_inference.run_pending(ledger, cfg, stage='local', deadline=clock.mono + 300)
    assert result['done'] == 1 and result['deferred'] == 0, result
    assert bool(result.get('paid_recovery_pending')) is pending_journal
    local = attachment_extraction.current(ledger.db, 1, key, cfg=cfg)
    assert local and local['ready'] and local['stage'] == 'local' and local['require_cloud'] is False
    assert local['document']['summary'] == LOCAL_SUMMARY
    assert local['provider_evidence'] == {'complete': True, 'providers': ['local']}
    assert local['reservation_artifact_ids'] == [] and local_calls
    local_count = len(local_calls)
    assert not posts and not receipts(ledger)
    assert mcs_queries.attachment_ocr_coverage(ledger.db, 1, cfg)['complete']
    source = mcs_queries.patient_context_source(ledger.db, 1, key, cfg=cfg)
    assert source['body_text'] == OCR_TEXT and source['extraction_ready']
    assert source['derived_summaries'] == [{'kind': 'attachment_extract_local',
        'artifact_id': local['artifact_id'], 'text': LOCAL_SUMMARY}]
    assert attachment_inference.run_pending(ledger, cfg, stage='cloud', deadline=clock.mono + 300)['done'] == 0
    assert not posts and not receipts(ledger)
    _raw_proof(ledger, 1, 1)
    _semantic_proof(ledger, 1, 1, cfg=cfg)
    finish(runtime, cfg)
    ocr_posts = [payload for payload in posts if payload.get('source_identity', {}).get('key') == key]
    assert ocr_posts and ''.join(payload['source'] for payload in ocr_posts) == OCR_TEXT
    assert ''.join(page['text'] for payload in ocr_posts for page in payload.get('derived_summaries', [])
                   if page['artifact_id'] == local['artifact_id']) == LOCAL_SUMMARY
    reserves = ledger.artifacts('cloud_usage_reserve')
    assert reserves and len(reserves) == len(posts)
    assert {json.loads(row['meta'])['route'] for row in reserves} == {'patient_context'}
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert tuple(ledger.db.execute('SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone()) == before_chat
    saved, count = receipts(ledger), len(posts)
    assert attachment_inference.run_pending(ledger, cfg, stage='local', deadline=clock.mono + 300)['done'] == 0
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
    assert receipts(ledger) == saved and len(posts) == count and len(local_calls) == local_count
    if pending_journal:
        assert journal.read_text() == 'synthetic unreplayable journal'


def test_ready_ocr_page_publishes_scoped_context_while_parent_chat_pending(
        runtime, monkeypatch):
    """A ready OCR original publishes its own scoped context while the parent
    chat extraction is still pending — the page is an independent original,
    never gated by the chat message's extraction state."""
    ledger, clock, _ = runtime
    cfg = {**BASE_CFG, 'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    monkeypatch.setattr(attachment_inference, 'load_config', lambda: cfg)
    seed(ledger, body='本人は合成の自宅で生活しています。', proof=False)
    key = _retain_ocr(ledger)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' "
                             "AND message_id=1").fetchone()[0] == 0
    parent_before = tuple(ledger.db.execute(
        'SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone())
    ocr_artifact = int(key.split(':')[1])
    ocr_before = ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                   (ocr_artifact,)).fetchone()[0]
    attachment_before = tuple(ledger.db.execute(
        'SELECT * FROM attachments WHERE attachment_id=1').fetchone())
    reserves_before = ledger.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]

    calls = []

    def admitted(_owner, text, **kwargs):
        assert not ledger.db.in_transaction
        calls.append(text)
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and ('reports' in payload or 'source' in payload):
            if 'reports' in payload:
                answer = {'fact_ids': [r['id'] for r in payload['reports'][:12]]}
            else:
                body = payload['source']
                answer = {'patient_context': [{'category': 'living', 'text': body,
                    'evidence': body, 'subject': 'unspecified', 'details': []}]}
        else:
            answer = {'symptoms': [], 'summary': LOCAL_SUMMARY}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}

    monkeypatch.setattr(local_llm, 'admitted_probe_format', lambda *a, **k: 'plain')
    monkeypatch.setattr(local_llm, 'admitted_chat', admitted)
    result = attachment_inference.run_pending(ledger, cfg, stage='local',
                                              deadline=time.monotonic() + 300)
    assert result['done'] == 1 and result['deferred'] == 0, result
    local = attachment_extraction.current(ledger.db, 1, key, cfg=cfg)
    assert local and local['ready'] and local['require_cloud'] is False
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' "
                             "AND message_id=1").fetchone()[0] == 0

    view = None
    for _ in range(30):
        result = step(runtime, cfg=cfg)
        view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
        if view is not None or result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    assert view is not None, 'ready OCR scope never published while parent chat pending'
    assert view['facts'] and view['coverage']['partial'] is True
    assert view['coverage']['unreflected'] == 1
    assert readiness.inference_coverage(ledger.db, 1, cfg)['complete'] is False
    _, meta = state._latest(ledger.db, state.KIND, 1)
    assert meta['source_scope'] == [key]
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' "
                             "AND message_id=1").fetchone()[0] == 0
    # The parent chat's own extraction stays genuinely pending — pinned on the
    # raw scheduler predicate, not inferred from the missing artifact row.
    pending = extract_llm.pending_pred(ledger.db)
    assert ledger.db.execute(
        'SELECT 1 FROM messages m WHERE m.project_id=1 AND m.message_id=1 '
        f'AND ({pending}) LIMIT 1').fetchone()
    # Repeating the same scope must not grow material inference calls, paid
    # reservations, or mutate the parent message or the retained original.
    published_calls = len(calls)
    reserves_after = ledger.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]
    assert reserves_after == reserves_before == 0
    result = step(runtime, cfg=cfg)
    assert result['status'] in ('progressed', 'deferred', 'complete'), result
    assert len(calls) == published_calls
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg) is not None
    assert tuple(ledger.db.execute(
        'SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone()) == parent_before
    assert ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                             (ocr_artifact,)).fetchone()[0] == ocr_before
    assert tuple(ledger.db.execute(
        'SELECT * FROM attachments WHERE attachment_id=1').fetchone()) == attachment_before
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' "
                             "AND message_id=1").fetchone()[0] == 0
