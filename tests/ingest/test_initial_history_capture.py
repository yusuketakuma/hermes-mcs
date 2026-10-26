"""Synthetic full-history paging, retry and live arrival preserve one immutable cohort."""
import json
import itertools
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import initial_import_state as state
import job_ops
import mcs_contract
from common_testkit import make_ledger
from ingest_testkit import _message, _unread_patient

BOUNDARY = 1800000000


def _post(mid, stamp=None, **kwargs):
    stamp = BOUNDARY - mid if stamp is None else stamp
    return _message(mid=mid, posted_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(), **kwargs)


@pytest.fixture
def store(tmp_path, monkeypatch):
    ticks = itertools.count()
    monkeypatch.setattr("ledger.time.time", lambda: BOUNDARY + next(ticks) / 1000000)
    store = make_ledger(tmp_path)
    store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    yield store
    store.close()


def _run(store, adapter, **kwargs):
    result = {'errors': []}
    job_ops.run_history_jobs(adapter, store, result, time.monotonic() + 300, **kwargs)
    assert result['errors'] == []


def test_5000_history_posts_are_saved_once_across_overlap_and_live_reply(store):
    first = [_post(mid) for mid in range(1, 2501)]
    last = [_post(mid) for mid in range(2400, 5001)]
    live = _post(6000, BOUNDARY + 1, parent_id=1, unread=True)
    responses = iter([mcs_contract.MessageBatch(first, pages=3, reached=False),
                      mcs_contract.MessageBatch([*last, live], pages=3, reached=True)])
    pages = []
    def fetch(*args, **kw):
        pages.append(kw['start_page'])
        return next(responses)
    adapter = SimpleNamespace(fetch_history=fetch)
    for _ in range(2):
        _run(store, adapter, notify_max_age_s=1)
    assert pages == [1, 3]
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 5001
    assert state.progress(store.db, 1)['saved_count'] == 5000
    assert len(state.message_ids(store.db, 1)) == 5000
    individual = [json.loads(r[0])['message_ids'] for r in store.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='new_messages'")]
    assert individual == [[6000]]
    assert store.job_state('history', 1) == 'done'
    assert state.progress(store.db, 1)['capture_phase'] == 'complete'


def test_save_commit_before_cursor_crash_replays_without_duplicate_counts_or_intents(store, monkeypatch):
    adapter = SimpleNamespace(fetch_history=lambda *a, **kw: mcs_contract.MessageBatch(
        [_post(1)], pages=3, reached=False))
    original = store.job_defer
    def crash(*args, **kwargs):
        raise RuntimeError('synthetic crash after save before cursor')
    monkeypatch.setattr(store, 'job_defer', crash)
    with pytest.raises(RuntimeError, match='synthetic crash'):
        _run(store, adapter)
    before = store.db.execute('SELECT count(*) FROM notify_outbox').fetchone()[0]
    assert store.job_payload('history', 1)['page'] == 1
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    monkeypatch.setattr(store, 'job_defer', original)
    _run(store, adapter)
    assert state.progress(store.db, 1)['saved_count'] == 1
    assert store.db.execute('SELECT count(*) FROM notify_outbox').fetchone()[0] == before
    assert store.job_payload('history', 1)['page'] == 4


def test_two_patients_rotate_and_incomplete_capture_is_not_called_complete(store):
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    visited = []
    def fetch(pid, *args, **kw):
        visited.append(pid)
        return mcs_contract.MessageBatch([], pages=3, reached=False)
    adapter = SimpleNamespace(fetch_history=fetch)
    for _ in range(4):
        _run(store, adapter, max_jobs=1)
    assert visited == [1, 2, 1, 2]
    assert all(state.progress(store.db, pid)['capture_phase'] == 'capturing' for pid in (1, 2))
