"""Removing credentials never releases an unresolved paid context to local inference."""
import json
from decimal import Decimal

import pytest

import anthropic_client
import cloud_client
import cloud_inference
import cloud_policy
import extract_llm
import extract_llm_provider
import patient_context_refresh_state as state
import patient_context_refresh as refresh
from test_context_cloud_update_identity import real_cloud, runtime, cloud
from test_patient_context_refresh import seed, step

__all__ = ['real_cloud', 'runtime', 'cloud']


def test_missing_key_never_replaces_a_paid_context_with_missing_result(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    result = step(runtime, cfg=cfg)
    assert result['status'] == 'progressed' and posts
    assert ledger.artifacts('cloud_usage_reserve') and ledger.artifacts('cloud_usage_result')
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
    before = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")]
    sent, local = len(posts), len(local_calls)
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda value: cloud_policy.resolve_settings(value, extract_llm.HOME)[0])
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    for _ in range(4):
        result = step(runtime, cfg=cfg)
        assert result['status'] != 'complete'
    assert len(local_calls) == local and len(posts) == sent
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")] == before


@pytest.mark.parametrize('status', [401, 403, 402, 404, 429, 500, 503, 529])
def test_definite_api_error_uses_local_without_erasing_reserved_cost(runtime, real_cloud, monkeypatch, status):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = []
    def rejected(_client, *args, **kwargs):
        sent.append(status)
        return status, {}, b'{"error":{"type":"synthetic_rejection"}}'
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', rejected)
    monkeypatch.setattr(cloud_client, 'time', cloud_policy.time)
    seed(ledger, body='本人は合成の自宅で生活しています。')
    for _ in range(40):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
        pytest.fail(f'API fallback did not publish: {result!r}; coverage='
                    f'{refresh._inference_coverage(ledger.db, 1, cfg, snapshot)!r}')
    assert sent and local_calls
    assert json.loads(ledger.artifacts(state.KIND)[-1]["meta"])["local_fallback"] is True
    results = ledger.artifacts('cloud_usage_result')
    assert results
    for row in results:
        metadata = json.loads(row['meta'])
        assert metadata['http_class'] == 'http_' + str(status)
        assert Decimal(metadata['cost_usd']) > 0
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg)


def test_budget_refusal_uses_local_without_new_cloud_reservation(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: 'daily_budget')
    seed(ledger, body='本人は合成の自宅で生活しています。')
    for _ in range(40):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
        pytest.fail(f'budget fallback did not publish: {result!r}; coverage='
                    f'{refresh._inference_coverage(ledger.db, 1, cfg, snapshot)!r}')
    assert local_calls and not posts
    assert not ledger.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize('status', [None, 200])
def test_unverified_or_answered_request_cannot_be_substituted_locally(runtime, real_cloud, status):
    ledger, _, _ = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    if status is None:
        with ledger.db:
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with pytest.raises(cloud_client.CloudError):
        cloud_inference.guard_context_local(ledger, cfg, json.dumps(posts[0], ensure_ascii=False),
            project_id=1, message_id=1, unit_key='scan:1', owner={})
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_unknown_transport_never_starts_local_replacement(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    sent = []
    def unknown(*args, **kwargs):
        sent.append(True)
        raise OSError('synthetic uncertain transport')
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', unknown)
    seed(ledger, body='本人は合成の自宅で生活しています。')
    for _ in range(4):
        assert step(runtime, cfg=cfg)['status'] != 'complete'
    assert sent and not local_calls
    assert ledger.artifacts('cloud_usage_reserve')
    assert ledger.artifacts('cloud_usage_result')
    assert not ledger.artifacts(state.KIND)


def test_paid_reservation_arriving_after_local_inference_blocks_adoption(runtime, real_cloud, monkeypatch):
    import local_llm
    ledger, _, _ = runtime
    cfg, posts = real_cloud
    monkeypatch.setattr(extract_llm, '_cloud_mode', lambda *args: 'off')
    original = local_llm.admitted_chat
    reservations = []
    def race(owner, prompt, **kwargs):
        result = original(owner, prompt, **kwargs)
        selection, errors = cloud_policy.resolve_profile(cfg, route='patient_context',
            purpose='patient_context', root=extract_llm.HOME)
        assert not errors and selection is not None
        reservation = cloud_policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1,
            message_id=1, unit_key='scan:1', owner={}, selection=selection)(
                anthropic_client.AnthropicClient.body(None, prompt, 1600, None), 120)
        reservations.append(reservation)
        return result
    monkeypatch.setattr(local_llm, 'admitted_chat', race)
    seed(ledger, body='本人は合成の自宅で生活しています。')
    result = step(runtime, cfg=cfg)
    assert reservations and not posts
    assert result['status'] == 'deferred'
    assert not ledger.artifacts(state.SCAN) and not ledger.artifacts(state.KIND)
    assert not ledger.artifacts('cloud_usage_result')



def test_verified_fallback_supersedes_an_older_failed_cloud_scan(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request',
                        lambda *args, **kwargs: (503, {}, b'{"error":{}}'))
    seed(ledger, body='本人は合成の自宅で生活しています。')
    first = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert first['status'] != 'complete' and not local_calls
    assert ledger.artifacts(state.SCAN)
    assert any(json.loads(row['meta']).get('provider') for row in ledger.artifacts(state.SCAN))
    clock.wall += 1000
    for _ in range(40):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail('older cloud failure shadowed the verified local fallback')
    assert local_calls
    snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
    assert refresh._inference_coverage(ledger.db, 1, cfg, snapshot)['complete']



@pytest.mark.parametrize('discarded', [False, True])
def test_legacy_summary_without_owner_never_uses_signature_difference_as_fresh_input(runtime, real_cloud, discarded):
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    selection, errors = cloud_policy.resolve_profile(cfg, route='patient_context',
        purpose='patient_context', root=extract_llm.HOME)
    assert not errors
    previous = {'instruction': '完全合成の同じ選択', 'reports': [{'id': '1'}],
                'source_identity': {'signature': 'a' * 64}}
    reservation = cloud_policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1,
        message_id=None, unit_key='summary', owner={}, selection=selection)(
            anthropic_client.AnthropicClient.body(None, json.dumps(previous, ensure_ascii=False), 1600, None), 120)
    if discarded:
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
    current = {**previous, 'source_identity': {'signature': 'b' * 64}}
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with pytest.raises(cloud_client.CloudError):
        cloud_inference.guard_context_local(ledger, cfg, json.dumps(current, ensure_ascii=False),
            project_id=1, message_id=None, unit_key='summary', owner={'source_sig': 'b' * 64})
    assert not posts and not local_calls
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def _finish_sources(runtime, cfg):
    ledger, _, _ = runtime
    for _ in range(20):
        step(runtime, cfg=cfg)
        snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
        scans = refresh._current_scans(ledger.db, 1, snapshot['sources'], snapshot['contract'], cfg=cfg)
        if scans and all(refresh._scan_ready(scan) for scan in scans.values()):
            assert all(scan.get('_provider_contract') for scan in scans.values())
            assert not ledger.artifacts(state.KIND)
            return
    pytest.fail('synthetic cloud source scans did not become ready before summary')


def test_only_summary_falls_back_then_advances_quality_without_repeating_local_generation(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: 'daily_budget')
    for _ in range(20):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail(f'local summary over cloud sources did not finish quality: {result!r}')
    assert len([call for call in local_calls if 'reports' in call]) == 1
    assert json.loads(ledger.artifacts(state.KIND)[-1]['meta'])['local_fallback'] is True


@pytest.mark.parametrize('discarded', [False, True])
def test_missing_or_discarded_paid_source_after_quality_cannot_enter_local_summary(runtime, real_cloud, monkeypatch, discarded):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    if discarded:
        reservation = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
    else:
        with ledger.db:
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    before = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")]
    sent, local = len(posts), len(local_calls)
    for _ in range(10):
        assert step(runtime, cfg=cfg)['status'] != 'complete'
        clock.wall += 61
    assert len(local_calls) == local and len(posts) == sent
    assert not ledger.artifacts(state.KIND)
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%' ORDER BY artifact_id")] == before


@pytest.mark.parametrize('jev_configured', [False, True])
def test_completed_paid_scan_can_finish_locally_when_key_disappears_before_quality(runtime, real_cloud, monkeypatch, jev_configured):
    import semantic_jev
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert posts and not ledger.artifacts(state.KIND)
    chunk = json.loads(ledger.artifacts(state.CHUNK)[-1]['meta'])
    assert chunk['cloud_receipts']
    sent = len(posts)
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    if not jev_configured:
        monkeypatch.setattr(semantic_jev, 'api_key', lambda: None)
        monkeypatch.setattr(semantic_jev, 'JevClient', lambda **kwargs: pytest.fail('unconfigured Jev called'))
    for _ in range(40):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
        assert result['status'] in ('progressed', 'deferred'), result
    else:
        pytest.fail(f'verified paid scan could not continue without cloud credentials: {result!r}')
    assert len(posts) == sent and local_calls
    assert all('reports' in call for call in local_calls), 'a paid source must not be re-inferred locally'
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg)


def test_source_result_lost_after_local_summary_also_blocks_its_quality_adoption(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: 'daily_budget')
    step(runtime, cfg=cfg)
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (state.KIND,))]
    assert before and json.loads(ledger.artifacts(state.KIND)[-1]['meta'])['local_fallback'] is True
    local = len(local_calls)
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
    for _ in range(5):
        assert step(runtime, cfg=cfg)['status'] != 'complete'
        clock.wall += 61
    assert len(local_calls) == local
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (state.KIND,))] == before


@pytest.mark.parametrize('damage', ['missing', 'null', 'duplicate', 'wrong_witness', 'wrong_id'])
def test_cloud_source_receipt_pointer_is_strict_and_legacy_lookup_is_verified(runtime, real_cloud, monkeypatch, damage):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    chunk = ledger.artifacts(state.CHUNK)[-1]
    metadata = json.loads(chunk['meta'])
    if damage == 'missing':
        metadata.pop('cloud_receipts')
    elif damage == 'null':
        metadata['cloud_receipts'] = None
    elif damage == 'duplicate':
        metadata['cloud_receipts'] *= 2
    elif damage == 'wrong_witness':
        metadata['cloud_receipts'][0]['witness'] = '0' * 64
    else:
        metadata['cloud_receipts'][0]['artifact_id'] += 100000
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                          (json.dumps(metadata), chunk['artifact_id']))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    sent = len(posts)
    for _ in range(20):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            assert damage == 'missing'
            break
        clock.wall += 61
    else:
        assert damage != 'missing', result
    assert len(posts) == sent
    if damage != 'missing':
        assert not local_calls and not ledger.artifacts(state.KIND)



def test_verified_partial_local_fallback_keeps_its_cursor_when_cloud_returns(runtime, real_cloud, monkeypatch):
    import patient_context_plan as plan
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    monkeypatch.setattr(plan, 'SCAN_CHUNK', 80)
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: 'daily_budget')
    seed(ledger, body=''.join(f'本人は合成施設{index}で生活しています。' for index in range(8)))
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    first = ledger.artifacts(state.SCAN)[-1]
    assert json.loads(first['meta'])['local_fallback'] is True
    assert not json.loads(first['content'])['complete']
    original = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE kind=?', (state.CHUNK,))]
    monkeypatch.setattr(cloud_policy, 'budget_block_reason', lambda *args, **kwargs: None)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit',
                        lambda *args, **kwargs: pytest.fail('partial local fallback restarted cloud inference'))
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert len(local_calls) == 2
    assert local_calls[1]['source_identity']['span'][0] == local_calls[0]['source_identity']['span'][1]
    assert not ledger.artifacts('cloud_usage_reserve')
    for row in original:
        assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (row[0],)).fetchone()) == row



@pytest.mark.parametrize('raw', ['not-json', '[' * 1500 + '0' + ']' * 1500])
def test_unreadable_paid_dependency_stays_held_without_entering_local_llm(runtime, real_cloud, monkeypatch, raw):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    row = ledger.artifacts('cloud_usage_result')[-1]
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, row['artifact_id']))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args: None)
    monkeypatch.setattr(extract_llm, '_cloud_mode', extract_llm_provider._cloud_mode)
    sent = len(posts)
    for _ in range(4):
        assert step(runtime, cfg=cfg)['status'] != 'complete'
        clock.wall += 61
    assert len(posts) == sent and not local_calls and not ledger.artifacts(state.KIND)
    assert ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (row['artifact_id'],)).fetchone()[0] == raw
