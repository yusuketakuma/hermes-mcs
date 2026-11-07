"""Synthetic overview retries remain bound to their exact selected source cohort."""
import pytest

import patient_context_plan as context_plan
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from test_patient_context_refresh import CFG, _prepare_summary, document, runtime as runtime, seed, step


def _failed_progress(led, clock, *, source_keys=None):
    snapshot = sources._snapshot(led.db, 1, CFG, hydrate=False, source_keys=source_keys)
    progress = {'source_sig': snapshot['source_sig'], 'attempts': 3,
                'next_try': clock.wall + 3600, 'failure': 'nightly_length_stop',
                'held': True, 'checks': state._CHECKS, 'quality_state': 'held'}
    if source_keys is not None:
        progress['source_scope'] = source_keys
    with led.db:
        state._replace(led, state.PROGRESS, 1, progress, {})
    return progress


@pytest.mark.parametrize('held', [False, True])
def test_whole_observer_keeps_scoped_failure_without_changing_snapshot(runtime, monkeypatch, held):
    led, clock, _ = runtime
    seed(led)
    _prepare_summary(runtime)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    assert step(runtime)['status'] == 'failed'
    stored = document(led, state.PROGRESS)
    assert stored['source_scope'] == [1]
    stored['held'] = held
    with led.db:
        state._replace(led, state.PROGRESS, 1, stored, {})
    whole = sources._snapshot(led.db, 1, CFG, hydrate=False)
    assert whole['source_sig'] != stored['source_sig']
    observed, progress, pending = readiness._pending(led.db, 1, CFG, now=clock.wall)
    assert observed['source_sig'] == whole['source_sig'] and 'source_scope' not in observed
    assert progress['source_sig'] == stored['source_sig']
    assert progress['attempts'] == stored['attempts'] and progress['next_try'] == stored['next_try']
    assert progress['held'] is held and progress['failure'] == 'nightly_length_stop'
    assert not pending['A'] and readiness.status(led.db, CFG)['failed'] == 1


def test_legacy_whole_retry_matches_only_the_identical_complete_cohort(runtime):
    led, clock, _ = runtime
    seed(led)
    seed(led, mid=2)
    stored = _failed_progress(led, clock)
    all_sources, same, pending = readiness._pending(led.db, 1, CFG, now=clock.wall, source_keys=[1, 2])
    assert all_sources['source_sig'] != stored['source_sig']
    assert same['held'] and same['attempts'] == 3 and not pending['A']
    _, subset, pending = readiness._pending(led.db, 1, CFG, now=clock.wall, source_keys=[1])
    assert not subset['held'] and subset['attempts'] == 0 and pending['A']
    assert document(led, state.PROGRESS) == stored


@pytest.mark.parametrize('changed', ['acquisition', 'contract', 'source'])
def test_legacy_retry_compatibility_rechecks_current_bindings(runtime, changed):
    led, clock, _ = runtime
    seed(led)
    stored = _failed_progress(led, clock)
    contract = sources._snapshot(led.db, 1, CFG, hydrate=False)['contract']
    if changed == 'acquisition':
        with led.db:
            led.db.execute("UPDATE patients SET fetch_state='partial',history_floor=NULL WHERE project_id=1")
    elif changed == 'contract':
        contract = {**contract, 'model': 'synthetic-other-model'}
    else:
        seed(led, body='生活環境: 更新された合成の居住記録。')
    current = sources._snapshot(led.db, 1, CFG, hydrate=False, contract=contract)
    assert current['source_sig'] != stored['source_sig']
    _, progress, _ = readiness._pending(led.db, 1, CFG, now=clock.wall,
                                       contract=contract, source_keys=[1])
    assert not progress['held'] and progress['attempts'] == 0 and progress['failure'] is None
    assert document(led, state.PROGRESS) == stored


def test_new_source_does_not_rearm_retry_of_the_frozen_old_subset(runtime):
    led, clock, _ = runtime
    seed(led)
    stored = _failed_progress(led, clock, source_keys=[1])
    seed(led, mid=2)
    _, same, pending = readiness._pending(led.db, 1, CFG, now=clock.wall, source_keys=[1])
    assert same == stored and not pending['A']
    _, new, pending = readiness._pending(led.db, 1, CFG, now=clock.wall, source_keys=[1, 2])
    assert new['attempts'] == 0 and not new['held'] and pending['A']
    # Selection is read-only: a new input neither deletes the older failure nor
    # releases any generation quota or financial receipt.
    assert document(led, state.PROGRESS) == stored


def test_source_scan_keeps_overview_retry_binding_from_a_narrower_cohort(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    stored = _failed_progress(led, clock, source_keys=[1])
    seed(led, mid=2)
    rotate = context_plan.lane_rotation
    monkeypatch.setattr(context_plan, 'lane_rotation',
        lambda best, rotation: rotate({'B': best['B']} if 'B' in best else {}, rotation))
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    result = step(runtime)
    assert result['lane'] == 'B' and result['status'] == 'failed'
    saved = document(led, state.PROGRESS)
    assert {key: saved[key] for key in stored} == stored
    assert saved['last_attempt'] > 0


def test_invalid_recorded_scope_does_not_release_a_held_retry(runtime):
    led, clock, _ = runtime
    seed(led)
    stored = _failed_progress(led, clock, source_keys=[1])
    invalid = {**stored, 'source_scope': [1, 1]}
    with led.db:
        state._replace(led, state.PROGRESS, 1, invalid, {})
    with pytest.raises(ValueError, match='patient_context_source_scope_invalid'):
        readiness._pending(led.db, 1, CFG, now=clock.wall)
    assert document(led, state.PROGRESS) == invalid
