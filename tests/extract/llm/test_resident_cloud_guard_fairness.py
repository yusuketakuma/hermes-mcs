"""An unverifiable resident cloud source must yield to later local work."""
import extract_llm_runner
from contextlib import nullcontext
import time

import pytest

import cloud_backlog
import cloud_inference
import cloud_policy
import extract_llm
from test_anthropic_runtime import _current_artifacts, old, real_usage_reserver, routed as routed
from test_cloud_response_recovery import legacy_cloud_routes


@pytest.mark.parametrize("reserved", [False, True])
def test_cloud_guard_resident_refusal_does_not_starve_following_local_source(routed, monkeypatch, reserved):
    ledger, _, calls, reservations, results = routed
    old(ledger, 1, 1)
    old(ledger, 2, 2)
    now = time.time()
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?,posted_at_ts=?', (now-1200, now-1200))
        ledger.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=2', (now-1300,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (now-1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-cloud-guard-fairness', now)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *args: nullcontext(True))
    if reserved:
        monkeypatch.setattr(cloud_policy, 'usage_reserver', real_usage_reserver)
        monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    attempted, local = [], []

    def extraction(body, **kwargs):
        provider = kwargs.get('provider')
        if provider is not None:
            attempted.append(provider.message_id)
            if reserved:
                provider.reserve(provider.client.body('synthetic unresolved input', 100, None), 30)
            raise cloud_inference.CloudObservationIncomplete('synthetic source observation unavailable')
        local.append(body)
        return {'symptoms': []}

    monkeypatch.setattr(extract_llm, 'llm_extract', extraction)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    with legacy_cloud_routes():
        first = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180,
                                        allow_cloud=True, background=True)
    assert first['defer_reasons'] == {'cloud_guard': 1} and first['done'] == 0
    evidence = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')")]
    assert len(evidence) == int(reserved)
    claim = ledger.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' AND message_id=1").fetchone()
    assert claim is not None and time.time() < claim[0] <= time.time() + extract_llm._PREPARE_HOLD_S
    with legacy_cloud_routes():
        following = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180,
                                            allow_cloud=True, background=True)
    assert calls == reservations == results == []
    assert following['done'] == 1, {'attempted': attempted, 'local': len(local),
                                  'following_defer_reasons': following.get('defer_reasons')}
    assert attempted == [1] and len(local) == 1
    assert [row['message_id'] for row in _current_artifacts(ledger)] == [2]
    assert [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')")] == evidence
