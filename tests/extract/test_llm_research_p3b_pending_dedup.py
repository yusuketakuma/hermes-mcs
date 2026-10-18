"""Synthetic acceptance of the single selection-time read in run_due (p3b)."""
import json

import pytest

import patient_nightly as nightly
from test_patient_nightly import document, finish, runtime, seed, step

__all__ = ["runtime"]   # the shared fixture is used by name below


def counted(monkeypatch, led):
    """Count _snapshot/_pending calls and whole-patient messages reads."""
    counts = {'_snapshot': 0, '_pending': 0, 'messages': 0}
    for name in ('_snapshot', '_pending'):
        original = getattr(nightly, name)
        def wrapper(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(nightly, name, wrapper)
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
    assert counts == {'_pending': 3, '_snapshot': 5, 'messages': 5}


def test_lane_b_reuses_selection_snapshot(runtime, monkeypatch):
    led, _, _ = runtime
    seed_three(led)
    for _ in range(3):
        assert step(runtime)['lane'] == 'A'
    counts = counted(monkeypatch, led)
    result = step(runtime)
    led.db.set_trace_callback(None)
    assert result['status'] == 'progressed' and result['lane'] == 'B'
    assert counts == {'_pending': 3, '_snapshot': 4, 'messages': 4}
    assert led.db.execute('SELECT count(*) FROM artifacts WHERE kind=?', (nightly.SCAN,)).fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['A', 'B'])
def test_source_edit_during_selection_is_rejected(runtime, monkeypatch, lane):
    led, _, calls = runtime
    seed_three(led)
    if lane == 'B':
        for _ in range(3):
            assert step(runtime)['lane'] == 'A'
        del calls[:]
    original = nightly._latest
    edited = []
    def latest(db, kind, pid, mid=None):
        if (kind, pid) == (nightly.PROGRESS, 0) and not edited:
            edited.append(1)
            db.execute("UPDATE messages SET content_hash='edited' WHERE message_id=1")
            db.commit()
        return original(db, kind, pid, mid)
    monkeypatch.setattr(nightly, '_latest', latest)
    result = step(runtime)
    assert result['status'] == 'source_changed' and not led.db.in_transaction
    if lane == 'A':
        assert calls == [] and document(led) is None
    else:
        assert document(led, nightly.SCAN) is None
    assert step(runtime)['status'] == 'progressed'


def test_result_reports_scan_size(runtime):
    led, _, _ = runtime
    seed_three(led)
    result = step(runtime)
    assert result['scanned'] == 3 and type(result['scan_ms']) is int and result['scan_ms'] >= 0
    assert step(runtime, complete=lambda pid: pid != 2)['scanned'] == 2
    finish(runtime)
    result = step(runtime)
    assert result['status'] == 'complete' and result['scanned'] == 3
    assert 'synthetic' not in json.dumps(result) and '合成' not in json.dumps(result)
