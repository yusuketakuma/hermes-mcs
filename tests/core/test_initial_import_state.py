"""Synthetic cohorts keep history membership, semantic origins and receipts distinct."""
import json
import itertools
from datetime import datetime, timezone

import pytest

import initial_import_state as state
import history_walk
import mcs_contract
from common_testkit import make_ledger
from ingest_testkit import _message, _unread_patient

BOUNDARY = 1800000000


def _post(mid, seconds, **kwargs):
    return _message(mid=mid, posted_at=datetime.fromtimestamp(seconds, timezone.utc).isoformat(), **kwargs)


@pytest.fixture
def store(tmp_path, monkeypatch):
    ticks = itertools.count()
    monkeypatch.setattr("ledger.time.time", lambda: BOUNDARY + next(ticks) / 1000000)
    store = make_ledger(tmp_path)
    store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    yield store
    store.close()


def _events(store, kind):
    return [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind=? ORDER BY event_id", (kind,))]


def _accepted_history(store):
    cohort = state.load(store.db, 1)["cohort_id"]
    pl = {"since":0,"page":1,"walk":history_walk.start(1,cohort)}
    batch = mcs_contract.MessageBatch([],1,True,page_observations=[history_walk.observation(1,0,1,10,False,[])])
    for _ in range(2):
        history_walk.advance(pl,batch,1,cohort,1,3,checkpoint_safe=True)
    store.job_defer(store.job_pending("history",1)["job_id"],0,payload=pl)


def test_new_cohort_is_immutable_and_existing_patients_are_not_reclassified(store):
    first = state.load(store.db, 1)
    store.upsert_patient_info(_unread_patient(1), is_archived=True)
    store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY + 100)
    assert state.load(store.db, 1) == first
    store.upsert_patient_info(_unread_patient(2))
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True)
    assert state.load(store.db, 2) is None


def test_common_save_boundary_aggregates_all_old_history_and_keeps_live_reply_individual(store, monkeypatch):
    origins = []
    monkeypatch.setattr(store, 'semantic_seed_tx', lambda pid, mids, origin, **kw: origins.append((set(mids), origin)))
    old = _post(1, BOUNDARY - 1000000, unread=True)
    old.replies = [_post(2, BOUNDARY + 1, parent_id=1, unread=True)]
    store.save_messages([old], project_id=1, notify={'source': 'discovery'},
                         semantic=True, notify_all_new=True, notify_max_age_s=1)
    assert state.message_ids(store.db, 1) == [1]
    assert state.progress(store.db, 1)['saved_count'] == 1
    assert _events(store, 'patient_history')[-1]['message_ids'] == [1]
    assert _events(store, 'new_messages')[-1]['message_ids'] == [2]
    assert next(origin for ids, origin in origins if ids == {1})['notification_free'] is True
    assert next(origin for ids, origin in origins if ids == {2})['source'] == 'discovery'
    assert store.db.execute('SELECT count(*) FROM notification_renders').fetchone()[0] == 0
    assert all(r[0] is not None for r in store.db.execute('SELECT notified_at FROM messages'))


def test_silent_import_and_body_timestamp_correction_preserve_first_role(store):
    old = _post(1, BOUNDARY - 1)
    store.save_messages([old], project_id=1)
    before = state.progress(store.db, 1)
    edited = _post(1, BOUNDARY + 10, body='synthetic correction')
    edited.metadata = {'reaction_count': 2, state.META_KEY: {'cohort_id': 'untrusted', 'role': 'live'}}
    store.save_messages([edited], project_id=1, notify={'source': 'unread'})
    assert state.message_ids(store.db, 1) == [1]
    assert state.progress(store.db, 1)['saved_count'] == before['saved_count'] == 1
    assert _events(store, 'new_messages') == []
    assert len(store.db.execute('SELECT * FROM message_metadata').fetchall()) == 1


@pytest.mark.parametrize('seconds', [BOUNDARY, BOUNDARY + 1, None])
def test_boundary_same_second_new_and_missing_times_are_individual(store, seconds):
    post = _post(1, seconds, unread=True) if seconds is not None else _message(mid=1, posted_at='', unread=True)
    store.save_thread_replies([post], 1, notify={'source': 'unread'})
    assert state.message_ids(store.db, 1) == []
    assert _events(store, 'new_messages')[-1]['message_ids'] == [1]


def test_registration_failure_rolls_back_patient_cohort_job_and_intent(tmp_path, monkeypatch):
    store = make_ledger(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic write failure')
    monkeypatch.setattr(store, 'job_add_tx', fail)
    with pytest.raises(RuntimeError):
        store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True)
    for table in ('patients', 'fetch_jobs', 'notify_outbox'):
        assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    store.close()


def test_invalid_cohort_fails_closed_without_individual_fallback(store):
    with store.db:
        store.db.execute("UPDATE patients SET initial_import='{}'")
    with pytest.raises(ValueError, match='initial_import_invalid'):
        store.save_messages([_post(1, BOUNDARY - 1)], project_id=1, notify={'source': 'unread'})
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert _events(store, 'new_messages') == []


def test_capture_never_counts_failed_or_unfetched_bodies_as_complete(store):
    store.save_messages([_post(1, BOUNDARY - 1, state='snippet')], project_id=1)
    store.set_history_floor(1, 0)
    job = store.job_pending('history', 1)
    _accepted_history(store)
    store.job_done(job['job_id'])
    assert state.progress(store.db, 1)['capture_phase'] == 'capturing'
    store.save_messages([_post(1, BOUNDARY - 1)], project_id=1)
    assert state.progress(store.db, 1)['capture_phase'] == 'complete'
    store.job_add('reply', 1, 1)
    job = store.job_pending('reply', 1, 1)
    store.job_fail(job['job_id'], reason_code='fetch_error')
    assert state.progress(store.db, 1)['capture_phase'] == 'complete'


def test_opt_in_fair_selection_preserves_default_fifo_and_never_starves_live(store):
    for i in range(20):
        store.outbox_add('patient_history', i + 10, {'cohort_id': 'synthetic'})
    for i in range(15):
        store.outbox_add('new_messages', 100 + i, {'message_ids': [i]})
    assert all(r['kind'] == 'patient_history' for r in store.outbox_due(10))
    fair = store.outbox_due(10, history_fair=True)
    assert sum(r['kind'] == 'patient_history' for r in fair) == 2
    assert sum(r['kind'] == 'new_messages' for r in fair) == 8
    assert store.outbox_due(1, history_fair=True)[0]['event_id'] == store.outbox_due(1)[0]['event_id']


@pytest.mark.parametrize("semantic", [False, True])
def test_mixed_project_silent_init_save_classifies_history_without_individual_notifications(store, semantic):
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    store.upsert_patient_info(_unread_patient(3))
    old = _post(10, BOUNDARY - 100, unread=True)
    old.replies = [_post(11, BOUNDARY + 1, parent_id=10, unread=True)]
    second = _post(20, BOUNDARY - 200, project_id=2, unread=True)
    legacy = _post(30, BOUNDARY - 300, project_id=3, unread=True)
    store.save_messages([old, second, legacy], semantic=semantic)
    assert state.message_ids(store.db, 1) == [10]
    assert state.message_ids(store.db, 2) == [20]
    assert state.progress(store.db, 1)["saved_count"] == state.progress(store.db, 2)["saved_count"] == 1
    assert state.load(store.db, 3) is None
    assert _events(store, "new_messages") == []
    history = _events(store, "patient_history")
    assert {mid for event in history for mid in event["message_ids"]} == {10, 20}
    # Replaying init_data's overlapping page cannot increment counts or reserve another aggregate.
    store.save_messages([old, second, legacy], semantic=semantic)
    assert _events(store, "patient_history") == history
    assert store.db.execute("SELECT notified_at FROM messages WHERE message_id=11").fetchone()[0] is None
    assert store.db.execute("SELECT notified_at FROM messages WHERE message_id=30").fetchone()[0] is None


@pytest.mark.parametrize("path", ["messages", "mixed", "replies"])
def test_full_body_save_retires_failed_reply_and_unholds_capture_without_manual_refresh(store, path):
    snippet = _post(1, BOUNDARY - 1, state="snippet", parent_id=100)
    store.save_messages([snippet], project_id=1)
    store.set_history_floor(1, 0)
    store.job_add("reply", 1, 1, parent_id=100)
    failed = store.job_pending("reply", 1, 1)
    store.job_fail(failed["job_id"], reason_code="fetch_error")
    history = store.job_pending("history", 1)
    _accepted_history(store)
    store.job_done(history["job_id"])
    assert state.progress(store.db, 1)["capture_phase"] == "held"
    full = _post(1, BOUNDARY - 1, parent_id=100)
    if path == "replies":
        store.save_thread_replies([full], 1)
    else:
        store.save_messages([full], project_id=1 if path == "messages" else None)
    assert store.job_state("reply", 1, 1) == "done"
    assert state.progress(store.db, 1)["capture_phase"] == "complete"
    assert _events(store, "patient_history")[-1]["message_ids"] == []
    assert _events(store, "new_messages") == []


def test_completed_capture_does_not_reopen_for_live_thread_jobs_or_manual_history(store):
    store.save_messages([_post(1, BOUNDARY - 1)], project_id=1)
    store.set_history_floor(1, 0)
    _accepted_history(store)
    store.job_done(store.job_pending("history", 1)["job_id"])
    assert state.progress(store.db, 1)["capture_phase"] == "complete"
    events = _events(store, "patient_history")
    store.job_add("thread", 1, 1)
    store.job_fail(store.job_pending("thread", 1, 1)["job_id"], reason_code="fetch_error")
    store.job_add("history", 1, payload={"since": 0, "page": 1})
    store.job_defer(store.job_pending("history", 1)["job_id"], 30)
    assert state.progress(store.db, 1)["capture_phase"] == "complete"
    assert _events(store, "patient_history") == events


def test_classification_only_metadata_has_no_observed_reaction_time(store):
    from message_observations import get_message_metadata
    store.save_messages([_post(1, BOUNDARY - 1)], project_id=1)
    metadata = get_message_metadata(store.db, 1)
    assert metadata["checked_at"] is None and metadata["reactions_status"] == "not_fetched"


def test_registration_reserves_normal_history_once_and_reactivation_reuses_job(store, monkeypatch):
    calls = []
    original = store.job_add_tx
    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "job_add_tx", record)
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    assert len(calls) == 1 and calls[0][0][:2] == ("history", 2)
    assert store.job_pending("history", 2) is not None
    store.upsert_patient_info(_unread_patient(2), is_archived=True)
    calls.clear()
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True)
    assert len(calls) == 1 and calls[0][0][:2] == ("history", 2)


def test_cancelled_history_keeps_save_analysis_and_reactivation_without_reissuing_summary(store, monkeypatch):
    origins = []
    monkeypatch.setattr(store, "semantic_seed_tx", lambda pid, mids, origin, **kw: origins.append((set(mids),origin)))
    current = state.load(store.db, 1)
    with store.db:
        current["notification_cancelled"] = True
        state._write(store.db, 1, current)
    before = _events(store, "patient_history")
    store.save_messages([_post(1, BOUNDARY-1), _post(2, BOUNDARY+1, unread=True)], project_id=1,
                         notify={"source":"unread"}, semantic=True)
    assert state.progress(store.db, 1)["saved_count"] == 1
    assert {mid for mids,_ in origins for mid in mids} == {1,2}
    assert _events(store, "patient_history") == before
    assert _events(store, "new_messages")[-1]["message_ids"] == [2]
    store.upsert_patient_info(_unread_patient(1), is_archived=True)
    store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True, observation_ts=BOUNDARY+100)
    assert state.load(store.db, 1)["notification_cancelled"] is True
    assert state.load(store.db, 1)["cohort_id"] == current["cohort_id"]
    assert _events(store, "patient_history") == before


@pytest.mark.parametrize("invalid", [False, 0, 1, None, "true"])
def test_notification_cancelled_flag_accepts_only_explicit_true(store, invalid):
    current = state.load(store.db, 1)
    current["notification_cancelled"] = invalid
    with store.db:
        state._write(store.db, 1, current)
    with pytest.raises(ValueError, match="initial_import_invalid"):
        state.load(store.db, 1)


def test_unverified_candidate_complete_requeues_normal_history_once_instead_of_reusing_old_floor(store):
    store.save_messages([_post(1,BOUNDARY-1)],project_id=1)
    current = state.load(store.db,1)
    current['capture_phase'] = 'complete'
    with store.db:
        state._write(store.db,1,current)
    store.set_history_floor(1,0)
    store.job_done(store.job_pending('history',1)['job_id'])
    assert state.load(store.db,1)['capture_phase'] == 'capturing'
    assert store.job_pending('history',1) is not None
    assert store.job_payload('history',1)['page'] == 1
    assert store.has_message(1)
    _accepted_history(store)
    store.job_done(store.job_pending('history',1)['job_id'])
    assert state.load(store.db,1)['capture_phase'] == 'complete'
    assert state.load(store.db,1)['capture_proof']['scope'] == history_walk.scope(1,current['cohort_id'])


def test_capture_proof_scope_or_shape_cannot_be_borrowed_by_another_cohort(store):
    current = state.load(store.db,1)
    current['capture_proof'] = {'v':1,'scope':history_walk.scope(2,current['cohort_id']),'digest':'a'*64}
    with store.db:
        state._write(store.db,1,current)
    with pytest.raises(ValueError,match='initial_import_invalid'):
        state.load(store.db,1)


def _ready_acquisition(store, pid=1):
    """Synthetic completed cohort and fetch journal, with no model or network."""
    capture = state.load(store.db, pid)
    capture['capture_phase'] = 'complete'
    capture['capture_proof'] = {
        'v': 1, 'scope': history_walk.scope(pid, capture['cohort_id']), 'digest': 'a' * 64}
    with store.db:
        state._write(store.db, pid, capture)
        store.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                         "WHERE project_id=?", (pid,))
        store.db.execute("UPDATE fetch_jobs SET state='done' WHERE project_id=?", (pid,))


def test_acquisition_requires_borrowed_snapshot_and_preserves_caller_transaction(store):
    _ready_acquisition(store)
    assert not store.db.in_transaction
    with pytest.raises(RuntimeError, match='history_snapshot_required'):
        state.acquisition_status(store.db, 1)
    assert not store.db.in_transaction
    store.db.execute('BEGIN')
    before = state.acquisition_status(store.db, 1)
    assert before['complete'] is True
    store.db.execute("UPDATE patients SET fetch_state='pending' WHERE project_id=1")
    during = state.acquisition_status(store.db, 1)
    assert during['complete'] is False and during['signature'] != before['signature']
    assert store.db.in_transaction
    store.db.rollback()
    store.db.execute('BEGIN')
    try:
        assert state.acquisition_status(store.db, 1) == before
    finally:
        store.db.rollback()


@pytest.mark.parametrize('case', [
    'fetch_null', 'fetch_pending', 'fetch_failed', 'reason', 'floor_null', 'floor_partial',
    'capture_pending', 'capture_proof_absent', 'capture_wrong_scope', 'capture_malformed', 'capture_oversized',
    'history_pending', 'thread_pending', 'reply_failed',
])
def test_acquisition_never_treats_missing_or_unfinished_evidence_as_complete(store, case):
    _ready_acquisition(store)
    store.db.execute('BEGIN')
    baseline = state.acquisition_status(store.db, 1)
    assert baseline['complete'] is True
    if case.startswith('fetch_'):
        value = {'fetch_null': None, 'fetch_pending': 'pending', 'fetch_failed': 'failed'}[case]
        store.db.execute('UPDATE patients SET fetch_state=? WHERE project_id=1', (value,))
    elif case == 'reason':
        store.db.execute("UPDATE patients SET fetch_reason='synthetic_failure' WHERE project_id=1")
    elif case.startswith('floor_'):
        store.db.execute('UPDATE patients SET history_floor=? WHERE project_id=1',
                         (None if case == 'floor_null' else 0,))
    elif case in ('capture_malformed', 'capture_oversized'):
        store.db.execute('UPDATE patients SET initial_import=? WHERE project_id=1',
                         ('{' if case == 'capture_malformed' else 'x' * 16385,))
    elif case.startswith('capture_'):
        capture = state.load(store.db, 1)
        if case == 'capture_pending':
            capture['capture_phase'] = 'capturing'
        elif case == 'capture_proof_absent':
            capture.pop('capture_proof')
        else:
            capture['capture_proof']['scope'] = history_walk.scope(2, capture['cohort_id'])
        state._write(store.db, 1, capture)
    else:
        kind, job_state = case.split('_')
        store.job_add_tx(kind, 1, 99)
        store.db.execute('UPDATE fetch_jobs SET state=? WHERE project_id=1 AND kind=? AND message_id=99',
                         (job_state, kind))
    actual = state.acquisition_status(store.db, 1)
    assert actual['complete'] is False and actual['signature'] != baseline['signature']
    assert store.db.in_transaction
    store.db.rollback()


def test_acquisition_scope_and_other_patient_writes_do_not_change_evidence(store):
    store.upsert_patient_info(_unread_patient(2), is_archived=False, notify_new=True,
                              observation_ts=BOUNDARY)
    _ready_acquisition(store, 1)
    _ready_acquisition(store, 2)
    store.db.execute('BEGIN')
    before = state.acquisition_status(store.db, 1)
    other = state.acquisition_status(store.db, 2)
    assert before['complete'] and other['complete'] and before['signature'] != other['signature']
    store.db.execute("UPDATE patients SET fetch_state='failed' WHERE project_id=2")
    assert state.acquisition_status(store.db, 1) == before
    assert not state.acquisition_status(store.db, 2)['complete']
    assert not state.acquisition_status(store.db, 999)['complete']
    with pytest.raises(ValueError, match='history_scope_invalid'):
        state.acquisition_status(store.db, True)
    assert store.db.in_transaction
    store.db.rollback()


def test_legacy_without_initial_import_requires_full_fetch_floor_and_jobs(store):
    store.upsert_patient_info(_unread_patient(2))
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                         "WHERE project_id=2")
    store.db.execute('BEGIN')
    try:
        assert state.load(store.db, 2) is None
        assert state.acquisition_status(store.db, 2)['complete'] is True
        store.job_add_tx('history', 2)
        assert not state.acquisition_status(store.db, 2)['complete']
    finally:
        store.db.rollback()
