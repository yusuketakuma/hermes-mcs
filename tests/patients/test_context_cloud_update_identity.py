"""Real cloud accounting follows content changes rather than refetch clocks."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

import anthropic_client
import attachment_extraction
import attachment_ocr
import cloud_client
import cloud_inference
import cloud_policy
import extract_llm as worker
import ledger as ledger_module
import mcs_queries
import patient_context_plan as plan
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
import semantic_jev
import semantic_runtime
import semantic_store
import test_context_completed_carry as completed
from anthropic_context_testkit import CFG, cloud, runtime
from test_context_summary_sources import _ocr_local
from test_patient_context_refresh import _raw_proof, _semantic_proof, document, seed, stamp, step

__all__ = ['cloud', 'runtime']
_REAL_PROVIDER = cloud_inference.CloudInferenceUnit
_REAL_WRITER_CURRENT = worker._writer_current


@pytest.fixture
def real_cloud(runtime, cloud, monkeypatch):
    ledger, clock, _ = runtime
    settings, errors = cloud_policy.cloud_config({'cloud_llm': {
        'mode': 'on', 'provider': 'anthropic', 'project_ids': [1],
        'routes': ['patient_context'], 'daily_request_budget': 100}})
    assert not errors
    cfg = {**CFG, 'cloud_llm': settings,
           'attachment_ocr': {'enabled': True, 'executable': '/synthetic/yomitoku'}}
    modules = (worker, completed.extract_llm_context, completed.extract_llm_prepare,
               completed.extract_llm_provider, completed.extract_llm_qc,
               completed.extract_llm_resident, completed.extract_llm_runner,
               completed.extract_llm_selection, completed.extract_llm_store, cloud_inference)
    for module in modules:
        monkeypatch.setattr(module, 'load_config', lambda: cfg)
    for module in (worker, completed.extract_llm_carry, completed.extract_llm_errors,
                   completed.extract_llm_resident, completed.extract_llm_runner,
                   completed.extract_llm_selection, completed.extract_llm_source_contract):
        monkeypatch.setattr(module, '_writer_current', _REAL_WRITER_CURRENT)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: worker._WRITER_STATE)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _REAL_PROVIDER)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda _cfg: settings)
    monkeypatch.setattr(cloud_policy, 'api_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    clock.wall = stamp('2026-10-26T12:00:00')
    fake_time = SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono,
                               perf_counter=lambda: clock.mono,
                               sleep=lambda seconds: setattr(clock, 'mono', clock.mono + seconds))
    for module in (worker, cloud_inference, cloud_client, cloud_policy, anthropic_client,
                   ledger_module, semantic_jev, semantic_runtime, semantic_store, state):
        monkeypatch.setattr(module, 'time', fake_time)
    posts = []

    def request(_client, method, body, timeout, *, deadline):
        assert method == 'POST' and timeout > 0 and deadline > clock.mono
        assert not ledger.db.in_transaction
        assert len(ledger.artifacts('cloud_usage_reserve')) == len(posts) + 1
        payload = json.loads(body['messages'][0]['content'])
        if 'source' in payload:
            sources = list(mcs_queries.patient_context_sources(ledger.db, 1, cfg=cfg))
            source = next(item for item in sources
                          if plan.source_key(item) == payload['source_identity']['key'])
            assert payload['source_identity']['signature'] == plan.source_signature(source)
            answer = {'patient_context': [{'category': 'living', 'text': payload['source'],
                'evidence': payload['source'], 'subject': 'unspecified', 'details': []}]}
        else:
            answer = {'fact_ids': [item['id'] for item in payload['reports']]}
        posts.append(payload)
        response = {'model': anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(answer)}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}
        return 200, {'Request-Id': 'synthetic-update'}, json.dumps(response).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', request)
    seed(ledger, body='本人は合成の自宅で生活しています。')
    return cfg, posts


def finish(runtime, cfg):
    for _ in range(30):
        result = step(runtime, cfg=cfg, local_admit=lambda: False)
        if result['status'] == 'complete':
            return
        acceptable = result['status'] == 'progressed' or (
            result['status'] == 'deferred' and result['reason'] in (
                'source_quality_pending', 'summary_quality_pending'))
        if not acceptable:
            snapshot, progress, pending = readiness._pending(runtime[0].db, 1, cfg)
            pytest.fail(repr((result, {key: snapshot[key] for key in (
                'acquisition', 'attachment_coverage', 'dependencies')}, progress, pending)))
    pytest.fail('synthetic cloud context did not finish')


def receipts(ledger):
    return [(row['artifact_id'], row['content'], row['meta'])
            for kind in ('cloud_usage_reserve', 'cloud_usage_result')
            for row in ledger.artifacts(kind)]


def test_refetch_timestamp_day_and_fresh_ledger_do_not_repeat_paid_work(runtime, real_cloud):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    before, saved, count = receipts(ledger), document(ledger), len(posts)
    assert count and len(before) == count * 2 and not local_calls
    aid = ledger.artifacts('extract_llm', message_id=1)[-1]['artifact_id']
    meta = json.loads(ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
    meta.update(fetched_at=clock.wall + 200, generated_at=clock.wall + 200)
    with ledger.db:
        ledger.db.execute('UPDATE messages SET updated_seen=updated_seen+200 WHERE message_id=1')
        ledger.db.execute('UPDATE artifacts SET created_at=created_at+200,meta=? WHERE artifact_id=?',
                          (json.dumps(meta), aid))
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
    clock.wall += 86400
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
    path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    ledger.close()
    reopened = ledger_module.Ledger(path)
    try:
        assert step((reopened, clock, local_calls), cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
        assert receipts(reopened) == before and document(reopened) == saved
        assert len(posts) == count
    finally:
        reopened.close()


@pytest.mark.parametrize('changed', ['original', 'summary', 'delayed_ocr'])
def test_changed_input_reaches_full_paid_payload_and_updates_context(runtime, real_cloud, changed):
    ledger, clock, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    before, count = receipts(ledger), len(posts)
    old_sources = {plan.source_key(source): plan.source_signature(source)
                   for source in mcs_queries.patient_context_sources(ledger.db, 1, cfg=cfg)}
    same_day = int(clock.wall // 86400)
    same_jst_day = state.workday_window(clock.wall)[0]
    expected_body = '本人は合成の自宅で生活しています。'
    expected_summary = None
    if changed == 'original':
        expected_body = '本人は合成の集合住宅へ転居しました。'
        seed(ledger, body=expected_body)
    elif changed == 'summary':
        aid = ledger.artifacts('extract_llm', message_id=1)[-1]['artifact_id']
        expected_summary = '変更後の合成の補助要約です。'
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                (json.dumps({'summary': expected_summary}), aid))
    else:
        expected_body = '本人は合成の介護ベッドを使用しています。'
        ledger.db.execute("INSERT INTO attachments(attachment_id,message_id,state,sha256,bytes,downloaded_at) "
                          "VALUES(1,1,'downloaded',?,32,1)", ('a' * 64,))
        pages = [{'page': 1, 'engine': 'pdf_text', 'text': expected_body,
                  'text_hash': attachment_ocr._hash(expected_body)}]
        value = {'schema': attachment_ocr.SCHEMA, 'runtime_contract': attachment_ocr.RUNTIME_CONTRACT,
            'engine': 'yomitoku', 'engine_version': attachment_ocr.ENGINE_VERSION,
            'state': 'complete', 'attachment_id': 1, 'input_sha256': 'a' * 64, 'input_bytes': 32,
            'pages': pages, 'text_hash': attachment_ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
        aid = ledger.artifact_add(attachment_ocr.KIND, json.dumps(value), project_id=1,
                                  message_id=1, meta={'attachment_id': 1})
        ledger.db.commit()
        key = f'attachment_ocr:{aid}:1'
        _ocr_local(ledger, key, cfg, '合成の添付要約です。')
        assert attachment_extraction.current(ledger.db, 1, key, cfg=cfg)['ready'] is True
        _raw_proof(ledger, 1, 1)
        _semantic_proof(ledger, 1, 1, cfg=cfg)
    current = {plan.source_key(source): plan.source_signature(source)
               for source in mcs_queries.patient_context_sources(ledger.db, 1, cfg=cfg)}
    assert current != old_sources
    # A distinct input is eligible after the three-hour generation interval.
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    finish(runtime, cfg)
    assert int(clock.wall // 86400) == same_day
    assert state.workday_window(clock.wall)[0] == same_jst_day
    fresh = [payload for payload in posts[count:] if 'source' in payload]
    matching = [payload for payload in fresh if payload['source'] == expected_body]
    assert matching, fresh
    if expected_summary is not None:
        assert any(item['text'] == expected_summary for payload in matching
                   for item in payload['derived_summaries'])
    assert any(payload['source_identity']['signature'] not in old_sources.values() for payload in matching)
    after = receipts(ledger)
    assert len(after) == len(posts) * 2 > len(before)
    assert all(row in after for row in before)
    view = readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert view and any(fact['quote'] == expected_body for fact in view['facts'])
