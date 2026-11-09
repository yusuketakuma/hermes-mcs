"""Removing credentials never releases an unresolved paid context to local inference."""
import cloud_inference
import cloud_policy
import extract_llm
import extract_llm_provider
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
