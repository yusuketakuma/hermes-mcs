"""The scan bookmark advances in memory and is saved once per evaluated patient."""
from contextlib import closing

import pytest

from ledger import Ledger
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state as state
from test_patient_context_refresh import runtime, seed, step

__all__ = ['runtime']


def _no_due_lane(monkeypatch, hook=None, ready=()):
    original = patient_context_refresh_readiness._pending
    checked = []

    def pending(db, pid, cfg, **kwargs):
        checked.append(pid)
        if hook is not None:
            hook(pid, len(checked))
        snapshot, progress, due = original(db, pid, cfg, **kwargs)
        if pid in ready:
            return snapshot, progress, due
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


def _idle(led):
    return state._latest(led.db, state.PROGRESS, 0)[0].get('scan_idle')


def _reach_idle(runtime, writes=None, limit=30):
    """Run workless slices until the shared idle is set; return the slices taken."""
    led, clock, _ = runtime
    for count in range(1, limit + 1):
        before = None if writes is None else len(writes)
        result = step(runtime)
        assert result['status'] == 'deferred' and result['covered'] is False
        assert 'scan_idle_s' not in result
        if writes is not None:
            assert result['scanned'] <= len(writes) - before <= result['scanned'] + 1
        idle = _idle(led)
        if idle and idle['until']:
            assert idle['cycles'] == 2 and 0 < idle['until'] - clock.wall <= 60
            return count
        assert idle is None or idle['until'] == 0
    pytest.fail('two workless activity cycles must set the shared scan idle')


def _seed_workless(runtime, monkeypatch, patients=12, **kwargs):
    led, _, _ = runtime
    for pid in range(1, patients + 1):
        seed(led, mid=pid, pid=pid)
    return _no_due_lane(monkeypatch, **kwargs)


def test_workless_rotation_idles_only_after_two_activity_cycles(runtime, monkeypatch):
    _seed_workless(runtime, monkeypatch)
    writes = _count_bookmark_writes(monkeypatch)
    # 12 patients at 6 activity turns per slice: two cycles take several slices.
    assert _reach_idle(runtime, writes) >= 3


def test_idle_scan_returns_without_reading_candidates_or_writing(runtime, monkeypatch):
    checked = _seed_workless(runtime, monkeypatch)
    _reach_idle(runtime)
    del checked[:]
    writes = _count_bookmark_writes(monkeypatch)
    monkeypatch.setattr(context_refresh.context_schedule, 'plan',
                        lambda *a, **k: pytest.fail('an idle scan must not read candidates'))
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'patient_context_waiting'
    assert result['scanned'] == 0 and result['covered'] is True and result['scan_reads'] == 0
    assert 0 < result['scan_idle_s'] <= 60
    assert not checked and not writes


def test_idle_scan_selects_a_startable_patient_within_one_collection_interval(runtime, monkeypatch):
    led, clock, _ = runtime
    ready = set()
    checked = _seed_workless(runtime, monkeypatch, ready=ready)
    _reach_idle(runtime)
    ready.add(1)
    elapsed = 0
    for _ in range(60):
        result = step(runtime)
        elapsed += 1
        if result['status'] == 'progressed':
            break
        assert result['status'] == 'deferred'
        wait = result.get('scan_idle_s') or 5
        clock.wall += wait
        elapsed += wait
    else:
        pytest.fail('a startable patient must be selected while the scan idles')
    assert elapsed <= 600
    # Selecting a patient clears the mark, so the next slice scans at once.
    assert _idle(led) is None
    before = len(checked)
    following = step(runtime)
    # A progressed unit carries no scan stats; the candidate read proves the scan ran.
    assert 'scan_idle_s' not in following and len(checked) > before


@pytest.mark.parametrize('mark', [
    {'cycles': 2, 'until': 3600},
    {'cycles': 2},
    {'cycles': 'two', 'until': 30},
    {'cycles': True, 'until': 30},
    {'cycles': 2, 'until': 30, 'extra': 1},
    [2, 30],
])
def test_out_of_range_or_malformed_idle_mark_is_ignored(runtime, monkeypatch, mark):
    led, clock, _ = runtime
    checked = _seed_workless(runtime, monkeypatch)
    step(runtime)
    content, meta = state._latest(led.db, state.PROGRESS, 0)
    if isinstance(mark, dict) and type(mark.get('until')) is int:
        # Relative to the fake clock: 3600 s ahead stands for a clock moved backwards.
        mark = {**mark, 'until': clock.wall + mark['until']}
    state._replace(led, state.PROGRESS, 0, {**content, 'scan_idle': mark}, meta)
    led.db.commit()
    del checked[:]
    result = step(runtime)
    assert 'scan_idle_s' not in result and result['scanned'] > 0 and checked


def test_refused_or_interrupted_scans_never_advance_the_idle(runtime, monkeypatch):
    led, _, _ = runtime
    mode = {'name': 'interrupt'}

    def hook(pid, count):
        if mode['name'] == 'interrupt' and count % 2 == 0:
            raise patient_context_refresh_sources._ScanInterrupted('patient_context_scan_incomplete')
        if mode['name'] == 'compete':
            content, meta = state._latest(other.db, state.PROGRESS, 0)
            state._replace(other, state.PROGRESS, 0, {**content, 'cursor': count}, meta)
            other.db.commit()

    _seed_workless(runtime, monkeypatch, hook=hook)
    # Every slice stops at its second patient, so the activity order wraps
    # several times without one completed slice.
    for _ in range(30):
        result = step(runtime)
        assert result['scan_interrupts'] == 1
        assert _idle(led) is None
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(Ledger(db_path)) as other:
        mode['name'] = 'compete'
        for _ in range(30):
            result = step(runtime)
            assert result['scan_refusal'] == 'bookmark_cas'
            assert _idle(led) is None
