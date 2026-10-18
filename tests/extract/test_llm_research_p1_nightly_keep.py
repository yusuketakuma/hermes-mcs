"""Synthetic acceptance of the run-lock wait after a finished nightly inference (p1)."""
import os
import sqlite3

import pytest

import patient_nightly as nightly
from test_patient_nightly import document, runtime, seed, step

__all__ = ["runtime"]   # the shared fixture is used by name below


TWO_CHUNKS = '合成記録の第一の区間。' * 240 + '\n' + '合成記録の第二の区間。' * 240


def contended_lock(monkeypatch, clock, *, grant_after, on_try=None):
    """Return the try list of a lock stub that yields None for the first tries."""
    tries = []
    def acquire():
        tries.append(1)
        if on_try is not None:
            on_try(len(tries))
        return os.open(os.devnull, os.O_RDONLY) if len(tries) > grant_after else None
    def sleep(seconds):
        clock.mono += seconds
        clock.wall += seconds
    monkeypatch.setattr(nightly, 'acquire_run_lock', acquire)
    monkeypatch.setattr(nightly.time, 'sleep', sleep, raising=False)
    return tries


@pytest.fixture
def other(runtime):
    led, _, _ = runtime
    conn = sqlite3.connect(led.db.execute('PRAGMA database_list').fetchone()[2])
    yield conn
    conn.close()


def test_source_edit_during_lock_wait_is_not_published(runtime, monkeypatch, other):
    led, clock, calls = runtime
    seed(led)
    def edit(n):
        if n == 3:
            other.execute("UPDATE messages SET content_hash='edited-while-waiting' WHERE message_id=1")
            other.commit()
    contended_lock(monkeypatch, clock, grant_after=3, on_try=edit)
    assert step(runtime)['status'] == 'source_changed'
    assert document(led) is None and len(calls) == 1


def test_scan_row_inserted_during_lock_wait_is_not_published(runtime, monkeypatch, other):
    led, clock, calls = runtime
    seed(led)
    def insert(n):
        if n == 3:
            other.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                          "VALUES (?,1,1,'{}','','{}',0)", (nightly.SCAN,))
            other.commit()
    contended_lock(monkeypatch, clock, grant_after=3, on_try=insert)
    assert step(runtime)['status'] == 'source_changed'
    assert document(led) is None and len(calls) == 1


def test_withdrawn_admission_stops_the_wait_immediately(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    withdrawn = []
    tries = contended_lock(monkeypatch, clock, grant_after=99,
                           on_try=lambda n: withdrawn.append(1) if n == 2 else None)
    result = step(runtime, admit=lambda: not withdrawn)
    assert result == {'status': 'deferred', 'done': 0, 'reason': 'run_lock', 'lock_wait_s': 1}
    assert len(tries) == 2 and document(led) is None


def test_run_lock_is_first_requested_after_inference(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    events = []
    chat = nightly.local_llm.admitted_chat
    monkeypatch.setattr(nightly.local_llm, 'admitted_chat',
                        lambda *a, **k: (events.append('chat'), chat(*a, **k))[1])
    contended_lock(monkeypatch, clock, grant_after=0, on_try=lambda n: events.append('lock'))
    assert step(runtime)['status'] == 'progressed'
    assert events[0] == 'chat' and 'lock' in events and 'lock' not in events[:events.index('chat') + 1]


def test_lock_wait_stays_inside_the_unit_deadline(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    chat = nightly.local_llm.admitted_chat
    def slow(*a, **k):
        clock.mono += 580
        return chat(*a, **k)
    monkeypatch.setattr(nightly.local_llm, 'admitted_chat', slow)
    contended_lock(monkeypatch, clock, grant_after=99)
    before = clock.mono + 580
    result = step(runtime, seconds=600)
    assert result['status'] == 'deferred' and result['reason'] == 'run_lock'
    # the deadline, not the 60 s cap, ended the wait
    assert clock.mono - 1 - before <= 20 and result['lock_wait_s'] == 20 - nightly.COMMIT_MARGIN
    assert document(led) is None


def test_lane_b_cursor_survives_short_contention(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led, body=TWO_CHUNKS)
    assert step(runtime)['lane'] == 'A'
    del calls[:]
    contended_lock(monkeypatch, clock, grant_after=3)
    result = step(runtime)
    assert result['status'] == 'progressed' and result['lane'] == 'B'
    assert result['lock_wait_s'] == 3 and len(calls) == 1
    assert document(led, nightly.SCAN)['cursor'] == 1
    first = calls[0]['source']
    assert step(runtime)['lane'] == 'B' and calls[-1]['source'] != first


def test_lane_b_result_lost_past_the_cap_is_counted_and_redone(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led, body=TWO_CHUNKS)
    assert step(runtime)['lane'] == 'A'
    del calls[:]
    contended_lock(monkeypatch, clock, grant_after=999)
    result = step(runtime)
    assert result == {'status': 'deferred', 'done': 0, 'reason': 'run_lock', 'lock_wait_s': 60}
    assert document(led, nightly.SCAN) is None and len(calls) == 1
    # the measured loss: a free lock on the next unit re-sends chunk 0
    monkeypatch.setattr(nightly, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
    assert step(runtime)['lane'] == 'B'
    assert len(calls) == 2 and calls[1]['source'] == calls[0]['source']
    assert document(led, nightly.SCAN)['cursor'] == 1


def test_progressed_result_reports_zero_wait_when_lock_is_free(runtime):
    led, _, _ = runtime
    seed(led)
    result = step(runtime)
    assert result['status'] == 'progressed' and result['lock_wait_s'] == 0
