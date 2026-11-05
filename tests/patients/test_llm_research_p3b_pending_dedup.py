"""Synthetic acceptance of the single selection-time read in run_due (p3b)."""

import patient_context_quality_controller
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import hashlib
import json
import sqlite3

import pytest

import inference_version as inference
import patient_context_refresh as context_refresh
from test_patient_context_refresh import _raw_proof, _semantic_proof, document, finish, runtime, seed, step

__all__ = ["runtime"]   # the shared fixture is used by name below


def counted(monkeypatch, led):
    """Count _snapshot/_pending calls and whole-patient messages reads."""
    counts = {'_snapshot': 0, '_pending': 0, 'messages': 0}
    for name in ('_snapshot', '_pending'):
        original = getattr(context_refresh, name)
        def wrapper(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(context_refresh, name, wrapper)
    def trace(sql):
        if 'FROM messages m WHERE project_id=' in sql and 'AND message_id=' not in sql:
            counts['messages'] += 1
    led.db.set_trace_callback(trace)
    return counts


def seed_three(led):
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)


def test_lane_a_reads_selection_snapshot_once(runtime, monkeypatch):
    led, _, _ = runtime
    seed_three(led)
    counts = counted(monkeypatch, led)
    result = step(runtime)
    led.db.set_trace_callback(None)
    assert result['status'] == 'progressed' and result['lane'] == 'A' and document(led)
    assert counts == {'_pending': 1, '_snapshot': 4, 'messages': 1}


def test_lane_b_reuses_selection_snapshot(runtime, monkeypatch):
    led, _, _ = runtime
    seed_three(led)
    # Every patient receives one fair unit before the first patient resumes.
    for _ in range(3):
        assert step(runtime)['lane'] == 'A'
    counts = counted(monkeypatch, led)
    result = step(runtime)
    led.db.set_trace_callback(None)
    assert result['status'] == 'progressed' and result['lane'] == 'B'
    assert counts == {'_pending': 1, '_snapshot': 3, 'messages': 0}
    assert led.db.execute('SELECT count(*) FROM artifacts WHERE kind=?', (patient_context_refresh_state.SCAN,)).fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['A', 'B'])
def test_source_edit_during_selection_is_rejected(runtime, monkeypatch, lane):
    led, _, calls = runtime
    seed_three(led)
    if lane == 'B':
        for _ in range(3):
            assert step(runtime)['lane'] == 'A'
        del calls[:]
    original = context_refresh._select_patient
    edited = []
    def select_then_edit(*args, **kwargs):
        selected = original(*args, **kwargs)
        assert selected[1] == 1 and lane in selected[0]
        if not edited:
            edited.append(1)
            body = '変更後の完全合成の居住記録。'
            led.db.execute("UPDATE messages SET body_html=?,body_text=?,content_hash=? WHERE message_id=1",
                           (body, body, hashlib.sha256(body.encode()).hexdigest()))
            led.db.commit()
        return selected
    monkeypatch.setattr(context_refresh, '_select_patient', select_then_edit)
    result = step(runtime)
    assert result['status'] == 'source_changed' and not led.db.in_transaction
    assert edited == [1] and calls == []
    monkeypatch.setattr(context_refresh, '_select_patient', original)
    if lane == 'A':
        assert calls == [] and document(led) is None
    else:
        assert document(led, patient_context_refresh_state.SCAN) is None
    _raw_proof(led, 1, 1)
    _semantic_proof(led, 1, 1)
    assert step(runtime)['status'] == 'progressed'


def test_result_reports_scan_size(runtime):
    led, _, _ = runtime
    seed_three(led)
    result = step(runtime)
    assert result['scanned'] == 1 and type(result['scan_ms']) is int and result['scan_ms'] >= 0
    # Patient 1 has just run; the next walk starts with patients 2, then 3.
    assert step(runtime, complete=lambda pid: pid == 3)['scanned'] == 2
    finish(runtime)
    result = step(runtime)
    assert result['status'] == 'complete' and result['scanned'] == 3
    assert 'synthetic' not in json.dumps(result) and '合成' not in json.dumps(result)


@pytest.mark.parametrize('during_pending', [False, True])
def test_progress_published_during_selection_is_preserved(runtime, monkeypatch, during_pending):
    led, clock, calls = runtime
    seed(led)
    newest = {'attempts': 5, 'next_try': clock.wall + 3600,
              'failure': 'TimeoutError', 'last_attempt': clock.wall,
              'source_sig': patient_context_refresh_sources._snapshot(led.db, 1, {})['source_sig']}
    conn = sqlite3.connect(led.db.execute('PRAGMA database_list').fetchone()[2])
    inserted = []

    def publish():
        if not inserted:
            inserted.append(True)
            conn.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                         "VALUES (?,1,NULL,?,'',?,?)",
                         (patient_context_refresh_state.PROGRESS, json.dumps(newest),
                          json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION}), clock.wall))
            conn.commit()

    pending, select_patient = patient_context_refresh_readiness._pending, context_refresh._select_patient

    def racing_pending(*args, **kwargs):
        result = pending(*args, **kwargs)
        if during_pending:
            publish()
        return result

    def racing_selection(*args, **kwargs):
        selected = select_patient(*args, **kwargs)
        if not during_pending:
            assert selected[1] == 1 and selected[0]
            publish()
        return selected

    def fail_summary(*args):
        raise TimeoutError('synthetic')

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', racing_pending)
    monkeypatch.setattr(context_refresh, '_select_patient', racing_selection)
    monkeypatch.setattr(patient_context_quality_controller, '_summary', fail_summary)
    try:
        result = step(runtime)
        assert result['status'] == 'deferred' and result['done'] == 0
        assert result['reason'] == 'patient_context_readiness_unverified'
        assert inserted == [True] and calls == []
        assert document(led, patient_context_refresh_state.PROGRESS) == newest
        assert not led.db.in_transaction
    finally:
        conn.close()
