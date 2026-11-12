"""The scan bookmark advances in memory and is saved once per evaluated patient."""
from contextlib import closing

from ledger import Ledger
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state as state
from test_patient_context_refresh import runtime, seed, step

__all__ = ['runtime']


def _no_due_lane(monkeypatch, hook=None):
    original = patient_context_refresh_readiness._pending
    checked = []

    def pending(db, pid, cfg, **kwargs):
        checked.append(pid)
        if hook is not None:
            hook(pid, len(checked))
        snapshot, progress, _ = original(db, pid, cfg, **kwargs)
        snapshot['unfinished'] = False
        return snapshot, progress, {'A': False, 'B': False, 'C': False}

    monkeypatch.setattr(patient_context_refresh_readiness, '_pending', pending)
    return checked


def _count_bookmark_writes(monkeypatch):
    writes = []
    original = state._replace

    def replace(ledger, kind, pid, *args, **kwargs):
        if kind == state.PROGRESS and pid == 0:
            writes.append(args[0].get('patient_scan'))
        return original(ledger, kind, pid, *args, **kwargs)

    monkeypatch.setattr(state, '_replace', replace)
    return writes


def test_scan_saves_the_bookmark_once_per_evaluated_patient(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in range(1, 14):
        seed(led, mid=pid, pid=pid)
    _no_due_lane(monkeypatch)
    writes = _count_bookmark_writes(monkeypatch)
    result = step(runtime)
    assert result['status'] == 'deferred' and result['scanned'] >= 2
    # Reservation and confirmation used to be two commits per scan turn.
    assert result['scanned'] <= len(writes) <= result['scanned'] + 1
    assert state._latest(led.db, state.PROGRESS, 0)[0]['patient_scan'] == writes[-1]


def test_interrupted_scan_keeps_reserved_bookmark_and_continues(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in range(1, 14):
        seed(led, mid=pid, pid=pid)

    def interrupt(pid, count):
        if count == 2:
            raise patient_context_refresh_sources._ScanInterrupted('patient_context_scan_incomplete')

    checked = _no_due_lane(monkeypatch, interrupt)
    first = step(runtime)
    assert first['status'] == 'deferred' and first['scan_interrupts'] == 1
    interrupted = list(checked)
    assert len(interrupted) == 2
    resumed = _no_due_lane(monkeypatch)
    step(runtime)
    # Both patients the interrupted scan consumed stay consumed.
    assert resumed and not set(resumed[:1]) & set(interrupted)


def test_competing_bookmark_write_refuses_the_scan(runtime, monkeypatch):
    led, _, _ = runtime
    for pid in range(1, 14):
        seed(led, mid=pid, pid=pid)
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(Ledger(db_path)) as other:
        def compete(pid, count):
            if count == 1:
                content, meta = state._latest(other.db, state.PROGRESS, 0)
                state._replace(other, state.PROGRESS, 0, {**content, 'cursor': 7}, meta)
                other.db.commit()

        checked = _no_due_lane(monkeypatch, compete)
        result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'patient_context_scan_incomplete'
    assert result['scan_refusal'] == 'bookmark_cas' and result['scan_interrupts'] == 1
    assert len(checked) == 1
    assert state._latest(led.db, state.PROGRESS, 0)[0]['cursor'] == 7
