"""Progress counts accepted source intervals, not cursors, duration or vacuous targets."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlite3

import patient_context_progress as progress
from test_patient_context_refresh import runtime, seed, CFG

__all__ = ['runtime']
SAFE_CFG = {**CFG, 'cloud_llm': {'mode': 'off', 'provider': 'anthropic'}}


@pytest.fixture
def observed(monkeypatch):
    db = SimpleNamespace(scan_until=progress.state.time.monotonic() + 60)
    source = {'message_id': 1, 'source_key': 1, 'body_length': 100, 'derived_summary_chars': 0}
    snapshot = {'source_sig': '1' * 64, 'sources': [source], 'contract': {},
                'acquisition': {'complete': True}, 'attachment_coverage': {'complete': True},
                'dependencies': {'complete': True}}
    scans = {1: {'complete': False, 'audited': False, 'cursor': 100}}
    chunks = [{'chunk_span': [0, 50], 'part': 0, 'parts': 1}]
    coverage = {'complete': False, 'summary': {'current': False}}
    @contextmanager
    def owner(*args):
        yield lambda *args: None
    monkeypatch.setattr(progress.sources, '_snapshot', lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(progress.state, '_out_of_scope', lambda *args: False)
    monkeypatch.setattr(progress, 'paused', lambda *args: False)
    monkeypatch.setattr(progress.plan, 'source_usable', lambda source: source['body_length'] > 0)
    monkeypatch.setattr(progress.state, '_latest', lambda *args: ({}, {}))
    monkeypatch.setattr(progress.readiness, '_inference_ready', lambda *args, **kwargs: True)
    monkeypatch.setattr(progress.sources, '_scan_read_owner', owner)
    monkeypatch.setattr(progress.sources, '_scans_from_read', lambda *args: scans)
    monkeypatch.setattr(progress.sources, '_source_chunks', lambda db, pid, source, *args: iter(chunks if source['source_key'] == 1 else []))
    monkeypatch.setattr(progress.readiness, '_inference_coverage', lambda *args, **kwargs: coverage)
    monkeypatch.setattr(progress.readiness, '_context_view_input', lambda *args, **kwargs: None)
    return db, snapshot, scans, chunks, coverage


def test_partial_cursor_is_never_a_coverage_proof_and_new_source_can_decrease(observed):
    db, snapshot, scans, chunks, _ = observed
    assert progress._read(db, 1, CFG)['percent'] == 45
    chunks.append({'chunk_span': [50, 100], 'part': 0, 'parts': 1})
    assert progress._read(db, 1, CFG)['percent'] == 80
    snapshot['sources'].append({'message_id': 2, 'source_key': 2, 'body_length': 100})
    snapshot['source_sig'] = '2' * 64
    assert progress._read(db, 1, CFG)['percent'] == 45
    scans[1]['held'] = True
    result = progress._read(db, 1, CFG)
    assert result['held'] is True and result['percent'] == 45


def test_split_parts_and_supplemental_pages_count_once_after_first_accepted_input(observed):
    db, snapshot, _, chunks, _ = observed
    snapshot['sources'][0]['derived_summary_chars'] = 20
    assert progress._read(db, 1, CFG)['percent'] == 50
    chunks[0].update(parts=2)
    assert progress._read(db, 1, CFG)['percent'] == 10
    chunks.append({'chunk_span': [0, 50], 'part': 1, 'parts': 2})
    assert progress._read(db, 1, CFG)['percent'] == 50
    chunks[0]['chunk_span'] = chunks[1]['chunk_span'] = [25, 50]
    assert progress._read(db, 1, CFG)['percent'] == 24


def test_verified_audits_and_existing_saved_view_proof_gate_completion(observed, monkeypatch):
    db, snapshot, scans, chunks, coverage = observed
    chunks[0]['chunk_span'] = [0, 100]
    scans[1].update(complete=True, audited=True)
    assert progress._read(db, 1, CFG)['percent'] == 90
    coverage['summary']['current'] = True
    assert progress._read(db, 1, CFG)['percent'] == 99
    coverage['complete'] = True
    assert progress._read(db, 1, CFG)['percent'] == 99
    monkeypatch.setattr(progress.readiness, '_context_view_input', lambda *args, **kwargs: {'facts': []})
    assert progress._read(db, 1, CFG)['percent'] == 100
    snapshot['sources'].clear()
    assert progress._read(db, 1, CFG)['stage'] == 'inactive'
    assert progress._read(db, 1, CFG)['percent'] is None


def test_input_and_local_boundaries_require_existing_readiness(observed, monkeypatch):
    db, snapshot, _, _, _ = observed
    snapshot['acquisition']['complete'] = False
    assert progress._read(db, 1, CFG)['percent'] == 0
    snapshot['acquisition']['complete'] = True
    monkeypatch.setattr(progress.readiness, '_inference_ready', lambda *args, **kwargs: False)
    assert progress._read(db, 1, CFG)['percent'] == 5


def test_independent_reader_preserves_caller_and_rejects_uncommitted_source(runtime):
    led, _, _ = runtime
    seed(led)
    state = led.db.in_transaction, led.db.total_changes
    assert progress.read(led.db, 1, SAFE_CFG)['percent'] in (5, 10)
    assert (led.db.in_transaction, led.db.total_changes) == state
    led.db.execute("UPDATE messages SET body_text='synthetic pending edit' WHERE project_id=1")
    pending = led.db.in_transaction, led.db.total_changes
    assert progress.read(led.db, 1, SAFE_CFG)['stage'] == 'unknown'
    assert (led.db.in_transaction, led.db.total_changes) == pending
    led.db.rollback()
    # Notification-only writes do not change the clinical input equivalence proof.
    led.db.execute('UPDATE notification_meta SET notify_dirty=1 WHERE singleton=1')
    assert progress.read(led.db, 1, SAFE_CFG)['percent'] in (5, 10)
    led.db.rollback()


def test_unavailable_or_exhausted_observation_is_unknown(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    monkeypatch.setattr(progress, 'READ_SECONDS', 0)
    assert progress.read(led.db, 1, SAFE_CFG)['percent'] is None
    assert progress.read(led.db, 1, {'extract': {'patient_context_nightly': {'enabled': False}}})['stage'] == 'inactive'


def test_control_pause_retains_independently_verified_partial_coverage(observed, monkeypatch):
    db, _, _, _, _ = observed
    assert progress._read(db, 1, CFG)['percent'] == 45
    monkeypatch.setattr(progress, 'paused', lambda *args: True)
    monkeypatch.setattr(progress.readiness, '_inference_ready', lambda *args, **kwargs: False)
    monkeypatch.setattr(progress.state, '_sources_intact', lambda *args: True)
    monkeypatch.setattr(progress, '_local_sources_ready', lambda *args: True)
    held = progress._read(db, 1, CFG)
    assert held['percent'] == 45 and held['held'] is True
    monkeypatch.setattr(progress, '_local_sources_ready', lambda *args: False)
    assert progress._read(db, 1, CFG)['percent'] == 5


def test_real_paused_control_does_not_erase_current_local_and_source_evidence(runtime):
    import mcs_queries
    led, _, _ = runtime
    seed(led)
    before = progress.read(led.db, 1, SAFE_CFG)
    assert before['percent'] == 10
    led.artifact_add(mcs_queries.SEMANTIC_CONTROL_KIND, 'paused', project_id=1)
    import semantic_policy
    import semantic_store
    row = led.db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m JOIN patients p USING(project_id) WHERE m.message_id=1').fetchone()
    policy, errors = semantic_policy.runtime_config(CFG)
    assert not errors
    assert semantic_store.reprocess_target_state(led, row, policy, cfg=CFG) == 'paused'
    assert semantic_store.reprocess_target_state(led, row, policy, cfg=CFG, observe_paused=True) == 'current'
    after = progress.read(led.db, 1, SAFE_CFG)
    assert after['percent'] == before['percent'] and after['held'] is True


def test_caller_interrupt_handler_and_outer_transaction_are_preserved(runtime):
    led, _, _ = runtime
    seed(led)
    led.db.execute('BEGIN')
    state = led.db.in_transaction, led.db.total_changes
    calls = []
    def interrupt():
        calls.append(True)
        return 1
    led.db.set_progress_handler(interrupt, 1)
    try:
        assert progress.read(led.db, 1, SAFE_CFG)['percent'] is None
        count = len(calls)
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            led.db.execute('SELECT COUNT(*) FROM messages').fetchone()
        assert len(calls) > count
    finally:
        led.db.set_progress_handler(None, 0)
    assert (led.db.in_transaction, led.db.total_changes) == state
    led.db.rollback()


def test_observed_unavailable_input_is_zero_not_inactive(observed, monkeypatch):
    db, snapshot, _, _, _ = observed
    snapshot['sources'][0]['body_state'] = 'unknown'
    monkeypatch.setattr(progress.plan, 'source_usable', lambda *args: False)
    result = progress._read(db, 1, CFG)
    assert result['stage'] == 'inputs' and result['percent'] == 0
    snapshot['sources'][0]['body_length'] = None
    with pytest.raises(ValueError, match='size_unknown'):
        progress._read(db, 1, CFG)


def test_usable_current_context_can_remain_complete_during_control_pause(observed, monkeypatch):
    db, _, scans, chunks, coverage = observed
    chunks[0]['chunk_span'] = [0, 100]
    scans[1].update(complete=True, audited=True)
    coverage.update(complete=True, summary={'current': True})
    monkeypatch.setattr(progress.readiness, '_context_view_input', lambda *args, **kwargs: {'facts': []})
    monkeypatch.setattr(progress, 'paused', lambda *args: True)
    result = progress._read(db, 1, CFG)
    assert result['percent'] == 100 and result['held'] is True
    from processing_route import valid_context_progress
    assert valid_context_progress({**result, 'observed_at': 0})


def test_progress_never_reads_private_key_values(runtime, monkeypatch):
    import cloud_policy
    led, _, _ = runtime
    seed(led)
    def forbidden(*args, **kwargs):
        pytest.fail('display must not inspect credentials')
    monkeypatch.setattr(cloud_policy, 'private_env_value', forbidden)
    assert progress.read(led.db, 1, SAFE_CFG)['percent'] == 10
    for cfg in (CFG, {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'auto'}},
                {**CFG, 'cloud_llm': {'provider': 'anthropic'}}):
        assert progress.read(led.db, 1, cfg)['percent'] is None


@pytest.mark.parametrize("available", [(), ("openai",)])
def test_real_progress_reader_accepts_publicly_resolved_implicit_mode(runtime, monkeypatch, available):
    import cloud_policy
    led, _, _ = runtime
    seed(led)
    cfg = {**CFG, "cloud_llm": {"provider": "openai"}}
    def forbidden(*args, **kwargs):
        pytest.fail("display must not inspect credentials")
    monkeypatch.setattr(cloud_policy, "provider_key", forbidden)
    settings, errors = cloud_policy.resolve_settings(cfg, available_providers=available)
    assert not errors
    effective = {**cfg, "cloud_llm": {**cfg["cloud_llm"], **settings}}
    assert progress.read(led.db, 1, cfg)["percent"] is None
    result = progress.read(led.db, 1, effective)
    assert result["percent"] == 10
    assert result["stage"] == "sources"
    assert led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0] == 0
