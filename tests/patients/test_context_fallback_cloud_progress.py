"""Verified local fallback covers original input without redundant cloud replay."""
import json

import cloud_policy
import mcs_queries
import patient_context_plan as plan
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from test_context_cloud_update_identity import cloud, real_cloud, receipts, runtime
from test_patient_context_refresh import seed, step

__all__ = ['runtime', 'cloud', 'real_cloud']


def test_verified_local_context_covers_every_input_without_cloud_replay_after_recovery(runtime, real_cloud, monkeypatch):
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    body = '本人は合成の自宅で生活しています。' * 170 + '\n\n' + '合成の通院記録です。' * 170
    seed(ledger, body=body)
    summary = '完全合成の補助要約。' * 1500
    artifact = ledger.artifacts('extract_llm', message_id=1)[-1]['artifact_id']
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                          (json.dumps({'summary': summary}), artifact))
    expected = next(mcs_queries.patient_context_sources(ledger.db, 1, cfg=cfg))
    assert expected['body_text'] == body
    assert any(item['text'] == summary for item in expected['derived_summaries'])
    with monkeypatch.context() as blocked:
        blocked.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'circuit_open')
        for _ in range(60):
            step(runtime, cfg=cfg)
            if readiness.context_view_input(ledger.db, 1, cfg=cfg):
                break
        else:
            raise AssertionError('verified local context never became usable')
        assert local_calls and not posts and not receipts(ledger)
        assert readiness.inference_coverage(ledger.db, 1, cfg)['complete']
        assert readiness.pending_estimate(ledger.db, 1, cfg)['scan_sources'] == 0
        before = len(local_calls)
        for _ in range(3):
            assert step(runtime, cfg=cfg)['status'] == 'complete'
        assert len(local_calls) == before and not posts and not receipts(ledger)
    old_local = [tuple(row) for row in ledger.db.execute(
        'SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id', (state.CHUNK,))]
    outcomes = []
    for _ in range(60):
        outcome = step(runtime, cfg=cfg, local_admit=lambda: False)
        outcomes.append(outcome)
        if outcome['status'] == 'complete':
            break
    else:
        raise AssertionError(repr((outcomes[-10:], len(posts), readiness.inference_coverage(ledger.db, 1, cfg)['status'])))
    observed = [payload for payload in local_calls if 'source' in payload]
    # The local path must cover the same original spans and supplemental pages.
    spans = {tuple(payload['source_identity']['span']): payload['source'] for payload in observed}
    assert ''.join(spans[span] for span in sorted(spans)) == body
    pages = {tuple(page['span']): page['text'] for payload in observed
             for page in payload.get('derived_summaries', []) if page['artifact_id'] == artifact}
    assert ''.join(pages[span] for span in sorted(pages)) == summary
    assert all(payload['source_identity']['signature'] == plan.source_signature(expected) for payload in observed)
    assert readiness.inference_coverage(ledger.db, 1, cfg)['complete']
    assert readiness.context_view_input(ledger.db, 1, cfg=cfg)
    assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == len(posts)
    saved, count = receipts(ledger), len(posts)
    assert not saved and count == 0
    for _ in range(3):
        assert step(runtime, cfg=cfg)['status'] == 'complete'
    assert len(posts) == count and receipts(ledger) == saved
    for row in old_local:
        assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (row[0],)).fetchone()) == row
