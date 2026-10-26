"""Synthetic full-history paging, retry and live arrival preserve one immutable cohort."""
import json
import itertools
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import initial_import_state as state
import job_ops
import history_walk
import mcs_adapter
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


def _batch(messages, *, start=1, pages=1, reached=True, error=None):
    observations = [history_walk.observation(1, 0, start+i, 10,
                    not (reached and i == pages-1), [m.message_id for m in messages] if i == 0 else [])
                    for i in range(pages)]
    return mcs_contract.MessageBatch(messages, pages, reached, error, page_observations=observations)


class _Pages:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_history(self, pid, since, *, max_pages, start_page):
        self.calls.append((pid,start_page,max_pages))
        observations, messages = [], []
        for page in range(start_page, start_page+max_pages):
            rows = self.pages[page-1] if page <= len(self.pages) else []
            tail = page >= len(self.pages)
            observations.append(history_walk.observation(pid,since,page,10,not tail,[m.message_id for m in rows]))
            messages.extend(rows)
            if tail:
                break
        return mcs_contract.MessageBatch(messages,len(observations),tail,page_observations=observations)


def _run(store, adapter, *, expected_errors=(), **kwargs):
    store.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE state='pending'")
    store.db.commit()
    result = {'errors': []}
    job_ops.run_history_jobs(adapter, store, result, time.monotonic() + 300, **kwargs)
    assert result['errors'] == list(expected_errors)
    return result


def test_5000_history_posts_are_saved_once_across_overlap_and_live_reply(store):
    first = [_post(mid) for mid in range(1, 2501)]
    last = [_post(mid) for mid in range(2400, 5001)]
    live = _post(6000, BOUNDARY + 1, parent_id=1, unread=True)
    # Preserve the original overlap/live assertions while giving both full scans real page boundaries.
    adapter = _Pages([first[:1000], first[1000:2000], first[2000:],
                      last[101:1601], [*last[1601:], live]])
    for _ in range(4):
        _run(store, adapter, notify_max_age_s=1)
    assert [call[1] for call in adapter.calls] == [1, 3, 1, 3]
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 5001
    assert state.progress(store.db, 1)['saved_count'] == 5000
    assert len(state.message_ids(store.db, 1)) == 5000
    individual = [json.loads(r[0])['message_ids'] for r in store.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='new_messages'")]
    assert individual == [[6000]]
    assert store.job_state('history', 1) == 'done'
    assert state.progress(store.db, 1)['capture_phase'] == 'complete'


def test_save_commit_before_cursor_crash_replays_without_duplicate_counts_or_intents(store, monkeypatch):
    adapter = SimpleNamespace(fetch_history=lambda *a, **kw: _batch(
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
        return mcs_contract.MessageBatch([], pages=3, reached=False, page_observations=[
            history_walk.observation(pid,0,kw["start_page"]+i,10,True,[]) for i in range(3)])
    adapter = SimpleNamespace(fetch_history=fetch)
    for _ in range(4):
        _run(store, adapter, max_jobs=1)
    assert visited == [1, 2, 1, 2]
    assert all(state.progress(store.db, pid)['capture_phase'] == 'capturing' for pid in (1, 2))


def test_invalid_initial_state_is_held_before_history_http_and_not_retried(store):
    with store.db:
        store.db.execute("UPDATE patients SET initial_import='{}' WHERE project_id=1")
    adapter = SimpleNamespace(fetch_history=lambda *a, **kw: pytest.fail("corrupt cohort fetched history"))
    for _ in range(2):
        result = {"errors": []}
        job_ops.run_history_jobs(adapter, store, result, time.monotonic() + 300)
    assert store.job_state("history", 1) == "failed"
    assert store.db.execute("SELECT reason_code FROM fetch_jobs WHERE kind='history'").fetchone()[0] == "schema_error"


def test_corruption_after_capture_keeps_failed_job_instead_of_rolling_back_progress(store):
    job = store.job_pending("history", 1)
    with store.db:
        store.db.execute("UPDATE patients SET initial_import='{}' WHERE project_id=1")
    store.job_done(job["job_id"])
    assert store.job_state("history", 1) == "failed"


class _Wire(mcs_adapter.MCSAdapter):
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def _get(self, path, params=None, extend_session=True):
        page = params['page']
        self.calls.append(page)
        rows = self.pages[page-1] if page <= len(self.pages) else []
        return {'messages': rows, 'paginate': {'has_next': page < len(self.pages)}}


def _wire_pages(ids):
    return [[{'id': mid, 'comment': f'synthetic {mid}', 'created_at': '2026-09-01T00:00:00+09:00'}
             for mid in ids[i:i+2]] for i in range(0,len(ids),2)]


def test_deleted_earlier_pages_reset_overlap_and_recover_surviving_unseen_posts(store):
    adapter = _Wire(_wire_pages(list(range(1,11))))
    _run(store,adapter)
    assert adapter.calls == [1,2,3]
    adapter.pages = _wire_pages(list(range(5,11)))
    _run(store,adapter)
    assert adapter.calls == [1,2,3,3]  # mismatch defers; no same-tick restart
    assert store.job_payload('history',1)['page'] == 1
    assert state.progress(store.db,1)['capture_phase'] != 'complete'
    for _ in range(2):
        _run(store,adapter)
    assert store.has_message(7) and store.has_message(8)
    assert state.progress(store.db,1)['capture_phase'] == 'complete'
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 10


def test_unchanged_overlap_anchor_does_not_hide_reordered_unseen_post(store):
    adapter = _Wire(_wire_pages(list(range(1,11))))
    _run(store,adapter)
    adapter.pages = _wire_pages([7,2,3,4,5,6,1,8,9,10])
    _run(store,adapter)
    assert not store.has_message(7)
    assert state.progress(store.db,1)['capture_phase'] != 'complete'
    _run(store,adapter)
    _run(store,adapter)
    assert store.has_message(7)
    assert store.job_payload('history',1)['walk']['mismatches'] == 1
    assert state.progress(store.db,1)['capture_phase'] != 'complete'
    _run(store,adapter)
    _run(store,adapter)
    assert adapter.calls == [1,2,3,3,4,5]*3
    assert state.progress(store.db,1)['capture_phase'] == 'complete'


@pytest.mark.parametrize('pages',[1,3])
def test_real_adapter_stable_walk_budget_and_complete_proof(store,pages):
    job = store.job_pending('history',1)
    payload = dict(job_ops.loads_dict(job['payload']),pages=pages)
    store.job_defer(job['job_id'],0,payload=payload)
    adapter = _Wire(_wire_pages(list(range(1,11))))
    for _ in range(10 if pages == 1 else 4):
        before = len(adapter.calls)
        _run(store,adapter)
        assert len(adapter.calls)-before <= pages
    current = state.load(store.db,1)
    assert current['capture_phase'] == 'complete'
    assert history_walk.capture_proof(current['capture_proof'],1,current['cohort_id'])
    assert store.job_payload('history',1)['walk']['phase'] == 'accepted'


@pytest.mark.parametrize('body,expected',[('', 'complete'),(None,'complete')])
def test_empty_full_and_body_unprovided_root_complete_without_converting_unknown(store,body,expected):
    row = {'id':1,'created_at':'2026-09-01T00:00:00+09:00'}
    if body is not None:
        row['comment'] = body
    adapter = _Wire([[row]])
    for _ in range(2):
        _run(store,adapter)
    assert state.load(store.db,1)['capture_phase'] == expected
    assert store.job_state('history',1) == 'done'
    assert store.db.execute('SELECT body_state FROM messages').fetchone()[0] == ('full' if body is not None else 'unknown')


def test_snippet_parent_fails_visibly_without_advancing_proof(store):
    adapter = _Wire([[{'id':1,'comment_snippet':'synthetic snippet','created_at':'2026-09-01T00:00:00+09:00'}]])
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        result = {'errors':[]}
        store.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE state='pending'")
        store.db.commit()
        job_ops.run_history_jobs(adapter,store,result,time.monotonic()+300)
    assert store.job_state('history',1) == 'failed'
    assert state.load(store.db,1)['capture_phase'] == 'held'
    assert not state.load(store.db,1).get('capture_proof')


def test_accepted_walk_waits_for_reply_jobs_without_tail_http_and_failed_job_holds(store):
    adapter = _Pages([[_post(1)]])
    store.job_add('thread',1,1,parent_id=1,payload={'page':1})
    for _ in range(2):
        _run(store,adapter)
    assert store.job_payload('history',1)['walk']['phase'] == 'accepted'
    assert len(adapter.calls) == 2 and store.job_state('history',1) == 'pending'
    _run(store,adapter)
    assert len(adapter.calls) == 2
    thread = store.job_pending('thread',1,1)
    store.job_fail(thread['job_id'],reason_code='fetch_error')
    _run(store,adapter)
    assert len(adapter.calls) == 2 and state.load(store.db,1)['capture_phase'] == 'held'


def test_accepted_walk_settles_after_reply_without_tail_http(store):
    adapter = _Pages([[_post(1)]])
    store.job_add('thread',1,1,parent_id=1,payload={'page':1})
    for _ in range(2):
        _run(store,adapter)
    store.job_done(store.job_pending('thread',1,1)['job_id'])
    _run(store,adapter)
    assert len(adapter.calls) == 2
    assert state.load(store.db,1)['capture_phase'] == 'complete'


def test_partial_history_auth_failure_saves_data_and_evidence_before_propagating(store):
    class Wire(_Wire):
        def _get(self,path,params=None,extend_session=True):
            if params['page'] == 2:
                raise mcs_contract.SessionExpired()
            return super()._get(path,params,extend_session)
    adapter = Wire(_wire_pages([1,2,3,4]))
    with pytest.raises(mcs_contract.SessionExpired):
        job_ops.run_history_jobs(adapter,store,{'errors':[]},time.monotonic()+300)
    assert store.has_message(1) and store.has_message(2)
    payload = store.job_payload('history',1)
    assert payload['page'] == 2 and payload['walk']['phase'] == 'scan'
    assert state.load(store.db,1)['capture_phase'] != 'complete'


def test_legacy_cursor_resets_once_and_corrupt_walk_fails_before_http(store):
    job = store.job_pending('history',1)
    store.job_defer(job['job_id'],0,payload={'since':0,'page':99,'pages':1})
    adapter = _Pages([[_post(1)],[_post(2)],[_post(3)]])
    _run(store,adapter)
    _run(store,adapter)
    assert [call[1] for call in adapter.calls] == [1,2]
    payload = store.job_payload('history',1)
    payload['walk']['scope'] = 'b'*64
    store.job_defer(job['job_id'],0,payload=payload)
    result = {'errors':[]}
    job_ops.run_history_jobs(adapter,store,result,time.monotonic()+300)
    assert store.job_state('history',1) == 'failed'
    assert len(adapter.calls) == 2 and result['errors']


def test_repeated_full_pass_disagreement_is_finite_and_never_publishes_floor(store):
    adapter = _Pages([[_post(1)]])
    for i in range(job_ops.HISTORY_STALL_LIMIT+1):
        adapter.pages = [[_post(i+1)]]
        result = _run(store,adapter,expected_errors=['import 1: window_stalled'] if i == job_ops.HISTORY_STALL_LIMIT else [])
    import collection_diagnostics
    assert collection_diagnostics.classify(result['errors'][0]) == 'incomplete'
    assert store.job_state('history',1) == 'failed'
    assert store.job_payload('history',1)['walk']['mismatches'] == job_ops.HISTORY_STALL_LIMIT
    assert state.load(store.db,1)['capture_phase'] == 'held'
    assert store.history_floor(1) != -1


def test_unknown_reply_remains_nonterminal_and_is_not_parent_no_body(store):
    root = _post(1)
    root.replies = [_post(2,parent_id=1,state='unknown')]
    root.reply_count = 1
    adapter = _Pages([[root]])
    def unavailable(*a,**kw):
        raise mcs_contract.MCSError('network_error')
    adapter.fetch_thread = unavailable
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        store.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='history' AND state='pending'")
        store.db.commit()
        job_ops.run_history_jobs(adapter,store,{'errors':[]},time.monotonic()+300)
    assert store.job_state('history',1) == 'failed'
    assert store.job_state('reply',1,2) == 'pending'
    assert state.load(store.db,1)['capture_phase'] == 'held'
    assert not state.load(store.db,1).get('capture_proof')
