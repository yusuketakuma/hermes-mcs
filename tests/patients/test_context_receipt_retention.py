"""Usage retention must not strand a patient context that still cites its paid receipts.

Each ``pinned=False`` case replays retention as it behaved before the context
pin existed, and records which guard then refuses the stored context.
"""
import json

import pytest

import cloud_policy
import maintenance
import patient_context_quality_receipts as quality_receipts
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as context_sources
import patient_context_refresh_state as state
from anthropic_context_testkit import CFG
from test_anthropic_context_repairs import publish_summary_repair
from test_context_cloud_update_identity import real_cloud, runtime, cloud, finish
from test_keyless_context_paid_history import _finish_sources
from test_patient_context_refresh import seed, step

__all__ = ['real_cloud', 'runtime', 'cloud']


def _retire(ledger, monkeypatch, *, pinned, batch=5000):
    """Run usage retention as if every stored receipt had passed its 62 days."""
    if not pinned:
        monkeypatch.setattr(maintenance, '_context_scan_cloud_pins',
                            lambda con, rows: (set(), set()), raising=False)
    monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
    newest = ledger.db.execute("SELECT MAX(created_at) FROM artifacts WHERE kind LIKE 'cloud_usage_%'").fetchone()[0]
    maintenance._prune_cloud_usage(ledger.db, newest + 1)


def _cited(ledger, kind):
    return [ref['artifact_id'] for row in ledger.artifacts(kind, project_id=1)
            for ref in json.loads(row['meta']).get('cloud_receipts', [])]


def _guard_sources(runtime, cfg):
    ledger, clock, _ = runtime
    snapshot = refresh._snapshot(ledger.db, 1, cfg, hydrate=False)
    context_sources.guard_cloud_sources(ledger, 1, snapshot['sources'], snapshot['contract'],
                                        cfg=cfg, deadline=clock.mono + 600)


def _scan_history(ledger):
    return [row['artifact_id'] for row in cloud_policy._verified_request_history(
        ledger, route='patient_context', project_id=1, message_id=1, unit_key='scan:1')]


@pytest.mark.parametrize('batch', [1, 5000])
def test_scan_receipts_outlive_retention_and_the_summary_still_runs(runtime, real_cloud, monkeypatch, batch):
    ledger, _, local_calls = runtime
    cfg, _ = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    cited = _cited(ledger, state.CHUNK)
    assert cited
    proof, history = cloud_policy.reservation_evidence_batch(ledger.db, cited), _scan_history(ledger)
    assert set(cited) <= set(history)
    _retire(ledger, monkeypatch, pinned=True, batch=batch)
    # Reserve, result and any disposition stay together: the witness covers all of them.
    assert cloud_policy.reservation_evidence_batch(ledger.db, cited) == proof
    # The resend guard reads this history without a date window, so it must stay whole.
    assert _scan_history(ledger) == history
    _guard_sources(runtime, cfg)
    local = len(local_calls)
    finish(runtime, cfg)
    assert len(local_calls) == local
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg)


def test_without_the_pin_retention_defers_the_summary_at_the_source_receipt_guard(runtime, real_cloud, monkeypatch):
    ledger, clock, local_calls = runtime
    cfg, posts = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    cited = _cited(ledger, state.CHUNK)
    _retire(ledger, monkeypatch, pinned=False)
    with pytest.raises(ValueError):
        cloud_policy.reservation_evidence_batch(ledger.db, cited)
    with pytest.raises(ValueError):
        _guard_sources(runtime, cfg)
    sent, local, refused = len(posts), len(local_calls), []
    for _ in range(5):
        result = step(runtime, cfg=cfg)
        assert result['status'] != 'complete'
        refused.append((result['status'], result.get('reason'), result.get('guard_error')))
        clock.wall += 61
    assert ('deferred', 'patient_context_cloud_not_sent', 'ValueError') in refused
    assert len(posts) == sent and len(local_calls) == local and not ledger.artifacts(state.KIND)


def test_discarded_scan_receipt_stays_rejected_after_retention(runtime, real_cloud, monkeypatch):
    ledger, _, _ = runtime
    cfg, _ = real_cloud
    seed(ledger, body='本人は合成の自宅で生活しています。')
    _finish_sources(runtime, cfg)
    reservation = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
    with pytest.raises(ValueError):
        _guard_sources(runtime, cfg)
    dispositions = [row['artifact_id'] for row in ledger.artifacts('cloud_usage_disposition')]
    assert dispositions
    _retire(ledger, monkeypatch, pinned=True, batch=1)
    # Dropping only the disposition would turn the discarded answer back into valid evidence.
    assert [row['artifact_id'] for row in ledger.artifacts('cloud_usage_disposition')] == dispositions
    with pytest.raises(ValueError):
        _guard_sources(runtime, cfg)


@pytest.mark.parametrize('pinned', [True, False])
def test_published_summary_receipt_guard_after_retention(runtime, real_cloud, monkeypatch, pinned):
    ledger, _, _ = runtime
    cfg, posts = real_cloud
    finish(runtime, cfg)
    meta = json.loads(ledger.artifacts(state.KIND)[-1]['meta'])
    assert meta['provider'] and meta['cloud_receipts']
    origin = [(None, 'summary', meta['provider']['model'], meta)]
    cited = _cited(ledger, state.CHUNK) + _cited(ledger, state.SCAN) + _cited(ledger, state.KIND)
    cited = list(dict.fromkeys(cited))
    proof, sent = cloud_policy.reservation_evidence_batch(ledger.db, cited), len(posts)
    cloud_policy.guard_context_origins(ledger, 1, origin)
    _retire(ledger, monkeypatch, pinned=pinned)
    if not pinned:
        with pytest.raises(ValueError):
            cloud_policy.guard_context_origins(ledger, 1, origin)
        with pytest.raises(ValueError):
            _guard_sources(runtime, cfg)
        return
    assert cloud_policy.reservation_evidence_batch(ledger.db, cited) == proof
    cloud_policy.guard_context_origins(ledger, 1, origin)
    _guard_sources(runtime, cfg)
    assert step(runtime, cfg=cfg, local_admit=lambda: False)['status'] == 'complete'
    assert len(posts) == sent and refresh.context_view_input(ledger.db, 1, cfg=cfg)


def test_summary_without_receipt_refs_is_refused_after_retention(runtime, real_cloud, monkeypatch):
    """A cloud-made row that stores no ``cloud_receipts`` has nothing for the pin to follow."""
    ledger, _, _ = runtime
    cfg, _ = real_cloud
    finish(runtime, cfg)
    row = ledger.artifacts(state.KIND)[-1]
    meta = json.loads(row['meta'])
    own = [ref['artifact_id'] for ref in meta.pop('cloud_receipts')]
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                          (json.dumps(meta, separators=(',', ':')), row['artifact_id']))
    origin = [(None, 'summary', meta['provider']['model'], meta)]
    # While the receipts exist, the guard falls back to the unit's whole verified history.
    cloud_policy.guard_context_origins(ledger, 1, origin)
    _retire(ledger, monkeypatch, pinned=True)
    kept = {row['artifact_id'] for row in ledger.artifacts('cloud_usage_reserve')}
    assert kept and not kept & set(own)
    # An empty history cannot establish the adopted answer, so the row is refused, not passed.
    with pytest.raises(ValueError, match='patient_context_cloud_source_unverified'):
        cloud_policy.guard_context_origins(ledger, 1, origin)


@pytest.mark.parametrize('pinned', [True, False])
def test_repaired_summary_publication_after_retention(runtime, cloud, monkeypatch, tmp_path, pinned):
    ledger, _, _ = runtime
    sends = publish_summary_repair(runtime, monkeypatch, tmp_path)
    assert json.loads(ledger.artifacts(state.KIND, project_id=1)[-1]['meta'])['quality_repair_ref']
    repair = json.loads(ledger.artifacts(state.QUALITY_REPAIR, project_id=1)[-1]['meta'])
    reserve = repair['reservation_artifact_id']
    proof = cloud_policy.reservation_evidence(ledger.db, reserve)
    profiled = quality_receipts._profiled_summary_repair_exists(ledger.db, 1, repair['input_key'])
    assert readiness.context_view_input(ledger.db, 1, cfg=CFG)
    _retire(ledger, monkeypatch, pinned=pinned)
    if not pinned:
        with pytest.raises(ValueError):
            cloud_policy.reservation_evidence(ledger.db, reserve)
        # The read path re-verifies the repair's receipt, so the repaired summary disappears.
        assert readiness.context_view_input(ledger.db, 1, cfg=CFG) is None
        # ...and the reservation that marks this input as already repaired is gone too.
        assert not quality_receipts._profiled_summary_repair_exists(ledger.db, 1, repair['input_key'])
        return
    assert cloud_policy.reservation_evidence(ledger.db, reserve) == proof
    assert quality_receipts._profiled_summary_repair_exists(ledger.db, 1, repair['input_key']) == profiled
    assert readiness.context_view_input(ledger.db, 1, cfg=CFG)
    assert len(sends) == 1
