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
