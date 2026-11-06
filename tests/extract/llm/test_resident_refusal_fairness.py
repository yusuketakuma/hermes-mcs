"""A resident refusal yields its selection slot without changing send evidence."""
import extract_llm_engine
import extract_llm_runner
from contextlib import nullcontext
import time

import pytest

import cloud_backlog
import cloud_client
import cloud_policy
import extract_llm
from test_cloud_selection_progress import cloud_candidates, routed
from test_anthropic_runtime import _current_artifacts, old, real_usage_reserver
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['cloud_candidates', 'routed']


@pytest.mark.parametrize('reason,yields', [
    ('cloud_unit_unresolved', True),
    ('checkpoint_unverified', True),
    ('synthetic-private', False),
])
def test_resident_refusal_allows_the_next_source_to_progress(cloud_candidates, monkeypatch, reason, yields):
    ledger, _, calls, reservations, results = cloud_candidates
    first = extract_llm._select_pending(ledger, 1, None, None, False)[0][0]['message_id']
    original = extract_llm._llm_call

    def refuse(*args, **kwargs):
        raise cloud_client.CloudNotSent('budget_exceeded', reason)

    monkeypatch.setattr(extract_llm, '_llm_call', refuse)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    with legacy_cloud_routes():
        report = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert report['deferred'] == 1 and report['done'] == 0
    assert calls == reservations == results == []

    monkeypatch.setattr(extract_llm, '_llm_call', original)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    with legacy_cloud_routes():
        following = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert following['done'] == 1
    completed = [row['message_id'] for row in _current_artifacts(ledger)]
    assert len(completed) == 1
    assert (first not in completed) is yields
    assert len(calls) == len(reservations) == len(results) == 1


def test_unresolved_paid_source_yields_without_rewriting_its_evidence(routed, monkeypatch):
    ledger, _, calls, _, _ = routed
    old(ledger, 1, 1)
    old(ledger, 2, 2)
    now = time.time()
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?,posted_at_ts=?', (now - 1200, now - 1200))
        ledger.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=2', (now - 1300,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (now - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-resident-fairness', now)
    monkeypatch.setattr(cloud_policy, 'usage_reserver', real_usage_reserver)
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *args: nullcontext(True))
    local = []
    seeded = []

    def extraction(body, **kwargs):
        provider = kwargs.get('provider')
        if provider is None:
            local.append(body)
            return {'symptoms': []}
        assert provider.message_id == 1
        if not seeded:
            seeded.append(provider.reserve(provider.client.body('synthetic unresolved input', 100, None), 30))
            return extract_llm._DEFERRED
        return provider('synthetic unresolved input', deadline=kwargs['deadline'], max_tokens=100)

    monkeypatch.setattr(extract_llm, 'llm_extract', extraction)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    with legacy_cloud_routes():
        assert extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)['deferred'] == 1
    before = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')")]
    with legacy_cloud_routes():
        held = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert held['defer_reasons'] == {'cloud_unit_unresolved': 1}
    with legacy_cloud_routes():
        following = extract_llm.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert following['done'] == 1 and len(local) == 1
    assert [row['message_id'] for row in _current_artifacts(ledger)] == [2]
    after = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')")]
    assert before == after and len(before) == 1 and not calls
