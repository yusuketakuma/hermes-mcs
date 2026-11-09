"""Exercise durable scan progress through the real context scheduler entry."""

import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import sqlite3

import pytest

import patient_context_refresh as refresh
import patient_context_schedule as schedule
from test_patient_context_refresh import CFG, document, runtime as runtime, seed, step


def global_state(led):
    return patient_context_refresh_state._latest(led.db, patient_context_refresh_state.PROGRESS, 0)[0]


def test_slow_first_patient_does_not_restart_the_prefix(runtime, monkeypatch):
    led, clock, _ = runtime
    for pid in (1, 2):
        seed(led, mid=pid, pid=pid)
    original = patient_context_refresh_readiness._pending
    checked = []

    def pending(db, pid, cfg, **kwargs):
        checked.append(pid)
        if pid == 1:
            clock.mono += 50
        return original(db, pid, cfg, **kwargs)

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', pending)
    first = step(runtime, seconds=20)
    assert first['status'] == 'deferred' and first['reason'] == 'patient_context_scan_incomplete'
    assert global_state(led)['patient_scan']['activity_after'][2] == 1
    assert document(led, patient_context_refresh_state.PROGRESS, pid=1) is None
    second = step(runtime, seconds=600)
    assert second['status'] == 'progressed' and checked[:2] == [1, 2]
    assert document(led, patient_context_refresh_state.PROGRESS, pid=2)['last_attempt_unit'] == 1


def test_interrupted_activity_sql_still_reaches_fair_turn(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    original = schedule.plan
    turns = []

    def plan(db, state):
        turn = state['turn'] if state else 0
        turns.append(turn)
        if turn != 3:
            raise sqlite3.OperationalError('interrupted')
        return original(db, state)

    monkeypatch.setattr(schedule, 'plan', plan)
    for expected in (1, 2, 3):
        result = step(runtime)
        assert result['status'] == 'deferred'
        assert global_state(led)['patient_scan']['turn'] == expected
    assert step(runtime)['status'] == 'progressed'
    assert turns == [0, 1, 2, 3]


def test_bookmark_preserves_lane_rotation_metadata_and_does_not_grow_artifacts(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1, proof=False)
    patient_context_refresh_state._replace(led, patient_context_refresh_state.PROGRESS, 0, {'cursor': 6, 'units': 12, 'other': 'synthetic'}, {'extra': 'retained'})
    led.db.commit()
    timeout = led.db.execute('PRAGMA busy_timeout').fetchone()[0]
    for _ in range(3):
        assert step(runtime)['status'] == 'deferred'
        assert led.db.execute('PRAGMA busy_timeout').fetchone()[0] == timeout
    content, meta = patient_context_refresh_state._latest(led.db, patient_context_refresh_state.PROGRESS, 0)
    assert (content['cursor'], content['units'], content['other'], meta['extra']) == (6, 12, 'synthetic', 'retained')
    assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=? AND project_id=0', (patient_context_refresh_state.PROGRESS,)).fetchone()[0] == 1
    assert not led.db.in_transaction


def test_malformed_bookmark_is_held_without_reset(runtime):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    saved = {'cursor': 8, 'units': 14, 'patient_scan': {'version': False}}
    patient_context_refresh_state._replace(led, patient_context_refresh_state.PROGRESS, 0, saved, {})
    led.db.commit()
    assert step(runtime)['status'] == 'deferred'
    assert global_state(led) == saved


def test_partial_pass_never_reports_complete(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in range(1, 14):
        seed(led, mid=pid, pid=pid)
    checked = []
    original = patient_context_refresh_readiness._pending

    def pending(db, pid, cfg, **kwargs):
        snapshot, progress, _ = original(db, pid, cfg, **kwargs)
        snapshot['unfinished'] = False
        checked.append(pid)
        return snapshot, progress, {'A': False, 'B': False, 'C': False}

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', pending)
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'patient_context_scan_incomplete'
    assert 0 < result['scanned'] <= 8 and len(checked) < 13


def test_candidate_reader_keeps_borrowed_transaction_and_cancellation_handler(runtime):
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    calls = []
    led.db.set_progress_handler(lambda: calls.append(True) or 0, 1)
    led.db.execute('BEGIN')
    try:
        with patient_context_refresh_sources._candidate_reader(led, clock.mono + 600) as reader:
            assert reader.db is not led.db and reader.db.in_transaction
            assert reader.db.execute('SELECT project_id FROM patients').fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                reader.db.execute('DELETE FROM patients')
        assert led.db.in_transaction
        before = len(calls)
        led.db.execute('SELECT 1').fetchone()
        assert len(calls) > before
        assert refresh.run_due(led, CFG, deadline=clock.mono+600, admit=lambda: True)['reason'] == 'transaction_active'
    finally:
        led.db.rollback()
        led.db.set_progress_handler(None, 0)


def test_saved_global_cas_detects_competing_owner(runtime):
    led, clock, _ = runtime
    expected = patient_context_refresh_state._latest(led.db, patient_context_refresh_state.PROGRESS, 0)
    other = {'cursor': 7, 'units': 3}
    patient_context_refresh_state._replace(led, patient_context_refresh_state.PROGRESS, 0, other, {})
    led.db.commit()
    assert refresh._advance_patient_scan(led, expected, schedule.advance_turn(None), clock.mono+600, lambda: True) is None
    assert global_state(led) == other


def test_reader_cancels_rows_and_keeps_complete_callback(runtime):
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    with patient_context_refresh_sources._candidate_reader(led, clock.mono+600) as reader:
        cursor = reader.db.execute('SELECT project_id FROM patients')
        reader.db.scan_until = clock.mono
        with pytest.raises(patient_context_refresh_sources._ScanInterrupted):
            cursor.fetchone()
    callbacks = []
    def complete(pid):
        callbacks.append(pid)
        return False
    # Whole-patient work still honors the callback; a proven source scope does not need it.
    assert not patient_context_refresh_readiness._inference_ready(
        led, 1, CFG, deadline=clock.mono + 600, complete=complete)
    assert callbacks == [1]
    assert patient_context_refresh_readiness._inference_ready(
        led, 1, CFG, deadline=clock.mono + 600, complete=lambda pid: True)
    assert step(runtime, complete=complete)['status'] == 'progressed'
    assert callbacks == [1]


def test_source_edit_after_candidate_read_is_rejected_before_inference(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led, mid=1, pid=1)
    original = patient_context_refresh_readiness._pending
    edited = []

    def pending(db, pid, cfg, **kwargs):
        outcome = original(db, pid, cfg, **kwargs)
        if not edited:
            led.db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', ('f' * 64,))
            led.db.commit()
            edited.append(True)
        return outcome

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', pending)
    assert step(runtime)['status'] == 'source_changed'
    assert calls == [] and document(led, patient_context_refresh_state.PROGRESS, pid=1) is None
    assert not led.db.in_transaction


def test_coverage_probe_is_small_and_requires_every_snapshot_patient(runtime):
    led, clock, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    with patient_context_refresh_sources._candidate_reader(led, clock.mono+600) as reader:
        queries = []
        reader.db.set_trace_callback(queries.append)
        assert not refresh._scan_covered(reader.db, {1, 2})
        assert refresh._scan_covered(reader.db, {1, 2, 3})
        assert len(queries) == 2
        assert all('LIMIT 1' in query and 'COUNT' not in query for query in queries)


def test_selection_reuses_the_pending_scan_read(runtime, monkeypatch):
    """One stored-scan read serves _pending's due check and the scope
    listing — a second identical read would be pure duplicate work."""
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    reads = []
    original = patient_context_refresh_sources._current_scans
    original_select = refresh._select_patient

    def counted(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)

    during = []
    def select(*args, **kwargs):
        start = len(reads)
        outcome = original_select(*args, **kwargs)
        during.append(len(reads) - start)
        return outcome

    monkeypatch.setattr(patient_context_refresh_sources, '_current_scans', counted)
    monkeypatch.setattr(refresh, '_select_patient', select)
    result = step(runtime)
    assert result['status'] == 'progressed'
    # Each _pending performs exactly one stored read; the scope listing
    # reuses it instead of running another identical _current_scans.
    assert during == [result['scan_reads']] >= [1]


def test_scan_stats_split_interrupts_from_ordinary_incompletion(runtime, monkeypatch):
    """CAS/contention interrupts are counted apart from a normal
    finite-slice 'incomplete' — same reason, distinguishable counters."""
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    monkeypatch.setattr(refresh, '_advance_patient_scan', lambda *a, **k: None)
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'patient_context_scan_incomplete'
    assert result['scan_interrupts'] == 1 and result['scan_reads'] == 0
    assert result['covered'] is False and result['waiting'] == 0


def test_finite_slice_incompletion_is_not_an_interrupt(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in range(1, 14):
        seed(led, mid=pid, pid=pid)
    original = patient_context_refresh_readiness._pending

    def pending(db, pid, cfg, **kwargs):
        snapshot, progress, _ = original(db, pid, cfg, **kwargs)
        snapshot['unfinished'] = False
        return snapshot, progress, {'A': False, 'B': False, 'C': False}

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', pending)
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'patient_context_scan_incomplete'
    assert result['scan_interrupts'] == 0
    assert result['scanned'] > 0 and result['scan_reads'] <= result['scanned']
