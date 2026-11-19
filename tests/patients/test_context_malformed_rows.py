"""Synthetic malformed rows stay with their own patient or source, never the whole pass."""
import json
import sqlite3
import sys

import pytest

import patient_context_plan as context_plan
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_patient_context_refresh import CFG, document, runtime as runtime, seed, step


def _scan_rows(led):
    return led.db.execute('SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id',
                          (state.SCAN,)).fetchall()


def _arm_on_lane(monkeypatch, lane):
    """True once the pass chose ``lane``; selection itself is never affected."""
    armed = {'on': False}
    real = context_refresh.context_schedule.lane_rotation

    def rotation(best, rot):
        out = real(best, rot)
        armed['on'] = out[0] == lane
        return out
    monkeypatch.setattr(context_refresh.context_schedule, 'lane_rotation', rotation)
    return armed


def _from_unit():
    return sys._getframe(2).f_code.co_name == '_run_due_unit'


def _bad_progress(led, pid, scope='bad'):
    led.artifact_add(state.PROGRESS, json.dumps({'source_scope': scope}), project_id=pid,
                     meta={VERSION_KEY: CURRENT_VERSION})


# S10: lane B/C failures are bound to the chosen source only.

@pytest.mark.parametrize('lane', ['B', 'C'])
def test_scan_read_failure_before_source_choice_writes_no_scan(runtime, monkeypatch, lane):
    led, _, _ = runtime
    seed(led)
    if lane == 'C':
        result = step(runtime)
        assert result['status'] == 'progressed' and result['lane'] == 'B', result
    before = [tuple(row) for row in _scan_rows(led)]
    units = (document(led, state.PROGRESS, pid=0) or {}).get('units', 0)
    armed = _arm_on_lane(monkeypatch, lane)
    real = sources._current_scans

    def scans(db, *args, **kwargs):
        if armed['on'] and db is led.db and _from_unit():
            raise ValueError('patient_context_source_scope_invalid')
        return real(db, *args, **kwargs)
    monkeypatch.setattr(sources, '_current_scans', scans)
    result = step(runtime)
    assert result['status'] == 'failed' and result['lane'] == lane, result
    assert result['reason'] == 'patient_context_source_scope_invalid'
    assert [tuple(row) for row in _scan_rows(led)] == before
    assert document(led, state.PROGRESS)['last_attempt_unit'] == units + 1
    assert not led.db.in_transaction


def test_lasting_failure_before_source_choice_is_skipped_by_the_next_selection(runtime, monkeypatch):
    """No source backoff exists before a choice; the same reader stops re-selection."""
    led, _, _ = runtime
    seed(led)
    armed = _arm_on_lane(monkeypatch, 'B')
    real = sources._current_scans
    lasting = {'on': False}

    def scans(db, *args, **kwargs):
        if lasting['on'] or armed['on'] and db is led.db and _from_unit():
            lasting['on'] = True
            raise ValueError('patient_context_source_scope_invalid')
        return real(db, *args, **kwargs)
    monkeypatch.setattr(sources, '_current_scans', scans)
    result = step(runtime)
    assert result['status'] == 'failed' and result['lane'] == 'B', result
    units = document(led, state.PROGRESS)['last_attempt_unit']
    for _ in range(3):
        result = step(runtime)
        assert result['status'] == 'deferred', result
        assert result['reason'] == 'patient_context_patient_invalid'
        assert result['skip_patient_invalid'] == 1
    assert document(led, state.PROGRESS)['last_attempt_unit'] == units
    assert not _scan_rows(led)


def _two_sources_one_backed_off(runtime):
    led, clock, _ = runtime
    seed(led, mid=1, body='生活環境: 合成の一つ目の記録。')
    seed(led, mid=2, body='生活環境: 合成の二つ目の記録。')
    result = step(runtime)
    assert result['status'] == 'progressed' and result['lane'] == 'B', result
    rows = led.db.execute('SELECT artifact_id,message_id,content FROM artifacts WHERE kind=?',
                          (state.SCAN,)).fetchall()
    assert len(rows) == 1
    aid, scanned, content = rows[0]
    stored = json.loads(content)
    stored.update(next_try=clock.wall + 99999, attempts=1)
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(stored), aid))
    led.db.commit()
    return scanned, 2 if scanned == 1 else 1


def test_failure_on_later_source_never_writes_earlier_source_record(runtime, monkeypatch):
    led, _, _ = runtime
    scanned, other = _two_sources_one_backed_off(runtime)
    before = [tuple(row) for row in _scan_rows(led)]
    armed = _arm_on_lane(monkeypatch, 'B')
    real = context_plan.source_usable

    def usable(source):
        if armed['on'] and source.get('message_id') == other and _from_unit():
            raise TypeError('synthetic')
        return real(source)
    monkeypatch.setattr(context_plan, 'source_usable', usable)
    result = step(runtime)
    assert result['status'] == 'failed' and result['lane'] == 'B', result
    assert [tuple(row) for row in _scan_rows(led)] == before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE kind=? AND message_id=?',
                              (state.SCAN, other)).fetchone()
    assert not led.db.in_transaction


def test_failure_after_source_choice_still_backs_off_that_source(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, mid=1, body='生活環境: 合成の一つ目の記録。')
    armed = _arm_on_lane(monkeypatch, 'B')
    real = context_refresh.context_queries.patient_context_sources

    def hydrate(db, pid, *args, **kwargs):
        if armed['on'] and db is led.db and 'mid' in kwargs and _from_unit():
            raise ValueError('patient_context_source_changed')
        return real(db, pid, *args, **kwargs)
    monkeypatch.setattr(context_refresh.context_queries, 'patient_context_sources', hydrate)
    result = step(runtime)
    assert result['status'] == 'failed' and result['lane'] == 'B', result
    rows = led.db.execute('SELECT message_id,content,meta FROM artifacts WHERE kind=?',
                          (state.SCAN,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == 1
    stored, meta = json.loads(rows[0][1]), json.loads(rows[0][2])
    assert stored['failure'] == 'patient_context_source_changed'
    assert stored['attempts'] == 1 and stored['next_try'] > clock.wall
    assert meta['source_key'] == 1
    assert '二つ目' not in rows[0][1]


# S1: one patient's unreadable stored state skips only that patient.

def _until_documents(runtime, pids, limit=40):
    led = runtime[0]
    results = []
    for _ in range(limit):
        results.append(step(runtime))
        assert not led.db.in_transaction
        if all(document(led, pid=pid) for pid in pids):
            return results
    pytest.fail(f'synthetic patients did not finish: {results[-3:]}')


@pytest.mark.parametrize('scope', ['bad', [[1]], [1, 'x']])
def test_malformed_progress_scope_skips_only_that_patient(runtime, scope):
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    _bad_progress(led, 1, scope)
    bad = led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1',
                         (state.PROGRESS,)).fetchall()
    results = _until_documents(runtime, (2, 3))
    results += [step(runtime) for _ in range(3)]
    assert any(r.get('skip_patient_invalid', 0) >= 1 for r in results)
    assert all(r['status'] != 'complete' for r in results)
    assert led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1',
                          (state.PROGRESS,)).fetchall() == bad
    assert document(led, pid=1) is None
    assert results[-1]['status'] == 'deferred'
    assert results[-1]['reason'] == 'patient_context_patient_invalid'
    assert results[-1]['skip_patient_code'] == 'patient_context_source_scope_invalid'


def test_malformed_summary_meta_scope_skips_only_that_patient(runtime):
    led, _, _ = runtime
    for pid in (1, 2):
        seed(led, mid=pid, pid=pid)
    led.artifact_add(state.KIND, json.dumps({'facts': []}), project_id=1,
                     meta={VERSION_KEY: CURRENT_VERSION, 'source_scope': 'bad'})
    results = _until_documents(runtime, (2,))
    results += [step(runtime) for _ in range(2)]
    assert any(r.get('skip_patient_invalid', 0) >= 1 for r in results)
    assert all(r['status'] != 'complete' for r in results)


def test_only_invalid_patient_left_is_deferred_not_complete(runtime):
    led, _, _ = runtime
    seed(led, mid=2, pid=2)
    _until_documents(runtime, (2,))
    for _ in range(3):
        step(runtime)
    seed(led, mid=1, pid=1)
    _bad_progress(led, 1)
    for _ in range(3):
        result = step(runtime)
        assert result['status'] == 'deferred', result
        assert result['reason'] == 'patient_context_patient_invalid'
        assert result['skip_patient_invalid'] == 1 and result['waiting'] >= 1
        assert result['skip_patient_code'] == 'patient_context_source_scope_invalid'
        assert not led.db.in_transaction


def test_out_of_scope_patient_with_malformed_row_is_not_counted(runtime):
    led, _, _ = runtime
    seed(led, mid=2, pid=2)
    _until_documents(runtime, (2,))
    for _ in range(3):
        step(runtime)
    seed(led, mid=1, pid=1)
    led.db.execute("UPDATE patients SET fetch_reason='forbidden' WHERE project_id=1")
    led.db.commit()
    _bad_progress(led, 1)
    for _ in range(3):
        result = step(runtime)
        assert result.get('skip_patient_invalid', 0) == 0, result
        assert result.get('reason') != 'patient_context_patient_invalid'
    assert readiness.status(led.db, CFG)['invalid'] == 0


@pytest.mark.parametrize('error', [ValueError('synthetic_scan'), TypeError('synthetic'), KeyError('synthetic')])
def test_scan_read_error_skips_patient(runtime, monkeypatch, error):
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    real = sources._scans_from_read

    def scans(db, pid, *args, **kwargs):
        if pid == 2:
            raise error
        return real(db, pid, *args, **kwargs)
    monkeypatch.setattr(sources, '_scans_from_read', scans)
    results = _until_documents(runtime, (1, 3))
    results += [step(runtime) for _ in range(2)]
    assert any(r.get('skip_patient_invalid', 0) >= 1 for r in results)
    assert all(r['status'] != 'complete' for r in results)
    assert document(led, pid=2) is None


def test_same_error_from_every_patient_is_raised_not_skipped(runtime, monkeypatch):
    """A configuration or code fault is not reported as many unreadable patients."""
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)

    def keys(*args, **kwargs):
        raise ValueError('synthetic_cfg_invalid')
    monkeypatch.setattr(readiness, '_eligible_source_keys', keys)
    with pytest.raises(ValueError, match='synthetic_cfg_invalid'):
        step(runtime)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE kind IN (?,?)',
                              (state.SCAN, state.KIND)).fetchone()


def test_different_errors_from_every_patient_are_skipped(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in (1, 2):
        seed(led, mid=pid, pid=pid)

    def keys(reader, pid, *args, **kwargs):
        raise (ValueError('synthetic_one') if pid == 1 else TypeError('synthetic_two'))
    monkeypatch.setattr(readiness, '_eligible_source_keys', keys)
    result = step(runtime)
    assert result['status'] == 'deferred', result
    assert result['reason'] == 'patient_context_patient_invalid'
    assert result['skip_patient_invalid'] == result['scanned'] == 2


def test_interrupted_pass_reports_interruption_over_skip(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    _bad_progress(led, 1)
    real_pending, real_unreadable = readiness._pending, readiness._unreadable_in_scope
    skipped = {'on': False}

    def unreadable(*args, **kwargs):
        skipped['on'] = True
        return real_unreadable(*args, **kwargs)

    def pending(db, pid, *args, **kwargs):
        if skipped['on'] and pid != 1:
            skipped['on'] = False
            raise sources._ScanInterrupted('patient_context_scan_changed')
        return real_pending(db, pid, *args, **kwargs)
    monkeypatch.setattr(readiness, '_unreadable_in_scope', unreadable)
    monkeypatch.setattr(readiness, '_pending', pending)
    for _ in range(12):
        result = step(runtime)
        assert not led.db.in_transaction
        if result.get('skip_patient_invalid') and result.get('scan_interrupts'):
            break
    else:
        pytest.fail('no pass both skipped and was interrupted')
    assert result['status'] == 'deferred', result
    assert result['reason'] == 'patient_context_scan_incomplete'


@pytest.mark.parametrize('error,refusal', [
    (sqlite3.OperationalError('synthetic'), 'read_sqlite'),
    (OSError('synthetic'), 'read_io'),
    (sources._ScanInterrupted('patient_context_scan_changed'), 'scan_interrupted'),
])
def test_read_failures_still_abort_pass(runtime, monkeypatch, error, refusal):
    led, _, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    real = readiness._pending
    visited = []

    def pending(db, pid, *args, **kwargs):
        if not visited or visited[0] == pid:
            visited.append(pid)
            raise error
        return real(db, pid, *args, **kwargs)
    monkeypatch.setattr(readiness, '_pending', pending)
    result = step(runtime)
    assert visited, 'the pass must reach a patient read'
    assert result['status'] == 'deferred', result
    assert result['scan_interrupts'] == 1 and result['scan_refusal'] == refusal
    assert result.get('skip_patient_invalid', 0) == 0
    assert result['reason'] == 'patient_context_scan_incomplete'
    assert not led.db.in_transaction


def test_skipped_patient_partial_lane_choice_is_discarded(runtime, monkeypatch):
    """A patient whose A scope fails after its B/C scope was chosen contributes no lane."""
    led, _, _ = runtime
    seed(led, mid=1, pid=1, body='生活環境: 合成の一つ目の記録。')
    _until_documents(runtime, (1,))
    for _ in range(3):
        step(runtime)
    seed(led, mid=2, pid=1, body='生活環境: 合成の二つ目の記録。')
    # No stored summary: the separate summary scope (only the finished post) is due.
    led.db.execute('DELETE FROM artifacts WHERE kind IN (?,?) AND project_id=1',
                   (state.KIND, state.LEGACY_KIND))
    led.db.commit()
    progress_before = led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1',
                                     (state.PROGRESS,)).fetchall()
    scan_before = [tuple(row) for row in _scan_rows(led)]
    calls = []

    def plan(db, pid, *args, **kwargs):
        calls.append(pid)
        raise TypeError('synthetic')
    monkeypatch.setattr(context_refresh, '_generation_plan', plan)
    result = step(runtime)
    assert calls == [1], (calls, result)
    assert result['status'] == 'deferred', result
    assert result['skip_patient_invalid'] == 1
    assert led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1',
                          (state.PROGRESS,)).fetchall() == progress_before
    assert [tuple(row) for row in _scan_rows(led)] == scan_before


@pytest.mark.parametrize('content', [
    {'complete': True, 'audited': True, 'quality_state': 'held', 'quality_hold_key': 5},
    {'complete': 'x', 'cursor': 'x', 'chunks': 'y'},
    {'source_occurrence_version': 1, 'source_occurrence': 'x'},
    {'accepted_candidate_ref': 'x'},
    {'attempts': 'x', 'next_try': 'y', 'held': [1]},
    {'complete': True, 'failure': {'a': 1}},
])
def test_known_malformed_scan_shapes_neither_abort_nor_skip(runtime, content):
    led, _, _ = runtime
    seed(led)
    step(runtime)
    aid = led.db.execute('SELECT artifact_id FROM artifacts WHERE kind=? ORDER BY artifact_id DESC',
                         (state.SCAN,)).fetchone()[0]
    led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), aid))
    led.db.commit()
    result = step(runtime)
    assert result.get('skip_patient_invalid', 0) == 0, result


# Observation: one unreadable row is counted, never fatal to the report.

def test_status_counts_invalid_patient_and_keeps_others(runtime):
    led, _, _ = runtime
    for pid in (1, 2):
        seed(led, mid=pid, pid=pid)
    _bad_progress(led, 1)
    counts = readiness.status(led.db, CFG)
    assert counts['invalid'] == 1
    assert counts['initial_pending'] == 1 and counts['history_pending'] == 1
    assert not led.db.in_transaction


def test_pending_estimate_reports_an_unreadable_scope_as_invalid_and_keeps_others(runtime):
    led, _, _ = runtime
    for pid in (1, 2):
        seed(led, mid=pid, pid=pid)
    _bad_progress(led, 1)
    bad = readiness.pending_estimate(led.db, 1, CFG)
    good = readiness.pending_estimate(led.db, 2, CFG)
    assert bad == {key: int(key == 'invalid') for key in bad}
    assert good['invalid'] == 0 and good['targets'] == 1
    assert not led.db.in_transaction
