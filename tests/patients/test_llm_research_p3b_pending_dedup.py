"""Synthetic acceptance of the single selection-time read in run_due (p3b)."""

import patient_context_quality_controller
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import json
import sqlite3

import pytest

import inference_version as inference
import patient_context_refresh as context_refresh
from test_patient_context_refresh import (
    _message, _prepare_summary, _raw_proof, _semantic_proof, document, finish, runtime, seed, step,
)

__all__ = ["runtime"]   # the shared fixture is used by name below


def counted(monkeypatch, led):
    """Count _snapshot/_pending calls and whole-patient messages reads."""
    counts = {'_snapshot': 0, '_pending': 0, 'messages': 0}
    for module, name in ((patient_context_refresh_sources, '_snapshot'),
                         (patient_context_refresh_readiness, '_pending')):
        original = getattr(module, name)
        def wrapper(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, wrapper)
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
    seed(led)
    _prepare_summary(runtime)
    counts = counted(monkeypatch, led)
    result = step(runtime)
    led.db.set_trace_callback(None)
    assert result['status'] == 'progressed' and result['lane'] == 'A' and document(led)
    # Include whole eligibility, quota input and fresh pre-reserve/save authority checks.
    assert counts == {'_pending': 1, '_snapshot': 8, 'messages': 1}


def test_lane_b_reuses_selection_snapshot(runtime, monkeypatch):
    led, _, _ = runtime
    seed_three(led)
    # Source scanning precedes the first nonempty overview.
    counts = counted(monkeypatch, led)
    result = step(runtime)
    led.db.set_trace_callback(None)
    assert result['status'] == 'progressed' and result['lane'] == 'B'
    assert counts == {'_pending': 1, '_snapshot': 4, 'messages': 0}
    assert led.db.execute('SELECT count(*) FROM artifacts WHERE kind=?', (patient_context_refresh_state.SCAN,)).fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['A', 'B'])
def test_source_edit_during_selection_is_rejected(runtime, monkeypatch, lane):
    led, _, calls = runtime
    seed(led)
    if lane == 'A':
        _prepare_summary(runtime)
    calls.clear()
    original = context_refresh._select_patient
    edited = []
    def select_then_edit(*args, **kwargs):
        selected = original(*args, **kwargs)
        assert selected[1] == 1 and lane in selected[0]
        if not edited:
            edited.append(1)
            body = '変更後の完全合成の居住記録。'
            led.save_messages([_message(1, body, 'full', project_id=1)])
            assert led.db.execute('SELECT count(*) FROM message_revisions WHERE message_id=1').fetchone()[0] >= 2
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
    # A real unprocessed edit excludes patient 2; scoped work ignores the legacy whole-patient callback.
    led.save_messages([_message(2, '生活環境: 合成の編集後記録。', 'full', project_id=2)])
    assert step(runtime)['scanned'] == 2
    _raw_proof(led, 2, 2)
    _semantic_proof(led, 2, 2)
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
