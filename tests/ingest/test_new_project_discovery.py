"""New read rooms enter the normal durable capture and notification path."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import collection_stages
import history_walk
import initial_import_state
import job_ops
import mcs_contract
import notify_cards
from adapters.common.spec import validate as validate_discord
from adapters.lineworks.cards import validate as validate_lineworks
from adapters.slack.cards import validate as validate_slack
from common_testkit import ledger_fixture
from ingest_testkit import _message, _unread_patient
from slack_testkit import SLACK
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import CFG
from test_initial_history_capture import _Pages

db = ledger_fixture(data_dir=True, name="db")


def _capture(db, patients, *, latest=None):
    adapter = SimpleNamespace(fetch_latest=latest, list_unread=lambda: mcs_contract.UnreadSnapshot(
        int(time.time()), [], listed_patients=patients))
    result = {"errors": [], "messages": 0, "incomplete": []}
    collection_stages.stage_unread(adapter, db, result, time.monotonic() + 60,
                                  db.begin_run(None), mark_read=True)
    return result


def _posted(mid, age, *, parent=None):
    return _message(mid=mid, parent_id=parent, posted_at=datetime.fromtimestamp(
        time.time() - age, timezone.utc).isoformat(), unread=False)


def _events(db):
    return db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchall()


def test_failed_first_registration_retry_starts_exactly_one_silent_initial_history(db, monkeypatch):
    boundary = int(time.time())
    post = _posted(10, 3 * 86400)
    marked = []
    patient = _unread_patient(1)
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_contract.UnreadSnapshot(boundary, [patient], listed_patients=[patient]),
        fetch_unread_messages=lambda pid, stamp: mcs_contract.MessageBatch([post], 1, True),
        fetch_unread_replies=lambda message: mcs_contract.ReplyBatch([], []),
        mark_patient_read=lambda pid, stamp, **kw: marked.append((pid, stamp)),
        fetch_latest=lambda pid: {'message_id': 10})
    original = db.job_add_tx
    failed = []
    def fail_registration(*args, **kwargs):
        if args and args[0] == 'history':
            failed.append(args)
            raise RuntimeError('synthetic registration failure before commit')
        return original(*args, **kwargs)
    monkeypatch.setattr(db, 'job_add_tx', fail_registration)
    def capture():
        result = {'errors': [], 'messages': 0, 'new_messages': 0, 'incomplete': [], 'marked_read': []}
        collection_stages.stage_unread(adapter, db, result, time.monotonic() + 60,
                                      db.begin_run(None), mark_read=True)
        return result
    first = capture()
    assert len(failed) == 2  # discovery and unread SAVE each roll back independently
    assert first['errors'] == ['project 1: db_write_failed', 'project 1: db_write_failed']
    assert marked == []
    for table in ('patients', 'messages', 'fetch_jobs', 'notify_outbox', 'read_marks'):
        assert db.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    monkeypatch.setattr(db, 'job_add_tx', original)
    retried = capture()
    assert retried['errors'] == []
    cohort = initial_import_state.load(db.db, 1)
    assert cohort is not None and cohort['boundary_ts'] == boundary
    assert initial_import_state.message_ids(db.db, 1) == [10]
    assert marked == [(1, boundary)]
    assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='history'").fetchone()[0] == 1
    assert _events(db) == []
    assert capture()['errors'] == []
    assert initial_import_state.load(db.db, 1)['cohort_id'] == cohort['cohort_id']
    assert db.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    history = db.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' ORDER BY event_id").fetchall()
    assert history == []
    assert not db.db.execute('SELECT 1 FROM notification_renders').fetchone()


@pytest.mark.parametrize('has_cohort', [False, True])
def test_failure_for_existing_patient_preserves_identity_cohort_and_notification_history(db, has_cohort):
    db.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=has_cohort,
                           observation_ts=int(time.time()))
    cohort = initial_import_state.load(db.db, 1)
    before = db.db.execute('SELECT patient_name,created_at,initial_import FROM patients WHERE project_id=1').fetchone()
    jobs = [tuple(row) for row in db.db.execute('SELECT * FROM fetch_jobs')]
    intents = [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox')]
    db.patient_fetch_failed(1, 'synthetic_failure')
    assert tuple(db.db.execute('SELECT patient_name,created_at,initial_import FROM patients WHERE project_id=1').fetchone()) == tuple(before)
    assert tuple(db.db.execute('SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1').fetchone()) == ('incomplete', 'synthetic_failure')
    assert [tuple(row) for row in db.db.execute('SELECT * FROM fetch_jobs')] == jobs
    assert [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox')] == intents
    db.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                           observation_ts=int(time.time()))
    assert initial_import_state.load(db.db, 1) == cohort


@pytest.mark.parametrize("config,validate,transport", [
    (SLACK, validate_slack, "slack"), (CFG, validate_discord, "discord"),
    (LINEWORKS, validate_lineworks, "lineworks"),
])
@pytest.mark.parametrize("pre_stored", [False, True])
def test_read_new_room_aggregates_initial_history_and_later_posts_reach_chat_spec(
        db, config, validate, transport, pre_stored):
    assert _capture(db, [_unread_patient(1)])["errors"] == []
    assert db.job_pending("history", 1) is not None
    assert db.db.execute("SELECT fetch_state FROM patients").fetchone()[0] == "pending"
    db.notify_all_replies = True
    recent, old = _posted(10, 600), _posted(20, 3 * 86400)
    recent.replies = [_posted(11, 300, parent=10)]
    old.replies = [_posted(21, 2 * 86400, parent=20)]
    if pre_stored:
        db.save_messages([recent, old], project_id=1)
        assert _events(db) == []
    adapter = _Pages([[recent, old]])
    result = {"errors": []}
    for _ in range(2):
        job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 60,
                                 notify_max_age_s=86400)
    assert result["errors"] == []
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
    assert _events(db) == []
    assert initial_import_state.progress(db.db, 1)["saved_count"] == 4
    history = db.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' ORDER BY event_id").fetchall()
    assert history == []
    assert not db.db.execute("SELECT 1 FROM notification_renders").fetchone()
    _capture(db, [_unread_patient(1)])
    assert db.job_state("history", 1) == "done"
    assert _events(db) == []
    assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='patient_history'").fetchone()[0] == len(history)
    # A later reply to a history root remains an individual live arrival.
    live = _posted(30, -1, parent=10)
    db.save_thread_replies([live], 1, notify={"source": "unread"}, notify_max_age_s=86400)
    [event] = _events(db)
    assert json.loads(event["payload"])["message_ids"] == [30]
    notify_cards.dispatch_intent(db, dict(event), config, now=time.time() + 300)
    renders = db.db.execute("SELECT spec_json FROM notification_renders").fetchall()
    assert renders
    for row in renders:
        spec = json.loads(row[0])
        assert validate(spec) is spec
        assert spec["delivery"].get("transport", "discord") == transport


def test_patient_and_initial_walk_roll_back_together(db, monkeypatch):
    original = db.job_add_tx
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic disk failure")
    monkeypatch.setattr(db, "job_add_tx", fail)
    assert _capture(db, [_unread_patient(1)])["errors"] == ["project 1: db_write_failed"]
    assert db.db.execute("SELECT count(*) FROM patients").fetchone()[0] == 0
    monkeypatch.setattr(db, "job_add_tx", original)
    assert _capture(db, [_unread_patient(1)])["errors"] == []
    assert db.job_pending("history", 1) is not None


def test_existing_read_room_is_not_reserved_again(db):
    db.upsert_patient_info(_unread_patient(1))
    _capture(db, [_unread_patient(1)])
    assert db.job_state("history", 1) is None


def test_reappearing_room_restarts_old_nonnotifying_history(db):
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    db.job_add("history", 1, payload={"since": 0, "page": 2, "trickle": True})
    _capture(db, [_unread_patient(1)])
    payload = db.job_payload("history", 1)
    assert payload["notify"] is True and payload["trickle"] is False
    assert payload["page"] == 1 and payload["notify_source"] == "discovery"
    assert not db.is_archived(1)


def test_daily_inventory_never_starts_a_competing_quiet_import(db):
    job_ops.seed_discovery(db)
    job_ops.run_discovery(SimpleNamespace(list_projects=lambda: [_unread_patient(1)]),
                          db, {"errors": []}, time.monotonic() + 120)
    assert db.job_payload("history", 1)["notify_source"] == "discovery"
    assert db.job_payload("history", 1)["trickle"] is False
    assert job_ops.seed_trickle(db) == 0


def test_initial_walk_resumes_without_duplicate_notification(db):
    _capture(db, [_unread_patient(1)])
    message = _posted(10, 60)
    adapter = _Pages([[message]]*4)
    for _ in range(4):
        job_ops.run_history_jobs(adapter, db, {"errors": []}, time.monotonic() + 60,
                                 notify_max_age_s=86400)
    assert [call[1] for call in adapter.calls] == [1,3,1,3]
    assert db.job_state("history", 1) == "done"
    assert _events(db) == []
    assert initial_import_state.progress(db.db, 1)["saved_count"] == 1


@pytest.mark.parametrize("age_limit", [None, 3600])
def test_initial_walk_aggregates_all_ages_without_hiding_later_pinned_pages(db, age_limit):
    _capture(db, [_unread_patient(1)])
    old = _posted(10,30*86400)
    adapter = _Pages([[old],[old],[old],[_posted(20,7200),_posted(30,300)]])
    for _ in range(4):
        job_ops.run_history_jobs(adapter, db, {"errors": []}, time.monotonic() + 60,
                                 notify_max_age_s=age_limit)
        assert _events(db) == []
    assert initial_import_state.message_ids(db.db, 1) == [10, 20, 30]
    assert initial_import_state.progress(db.db, 1)["saved_count"] == 3
    assert [call[1] for call in adapter.calls] == [1,3,1,3] and db.job_state("history", 1) == "done"
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 3


@pytest.mark.parametrize("source", [None, False, 1, "unread"])
def test_initial_notification_source_is_validated(db, source):
    db.ensure_patient(1)
    db.job_add("history_head", 1, payload={"since": 0, "notify": True,
                                          "notify_source": source})
    result = {"errors": []}
    job_ops.run_history_jobs(SimpleNamespace(), db, result, time.monotonic() + 60)
    assert result["errors"] == ["import job: invalid_payload"]
    assert db.job_state("history_head", 1) == "failed"


def test_latest_unread_across_pages_gets_fetched_with_one_registration_commit(db, monkeypatch):
    from mcs_adapter import MCSAdapter

    listed = []
    for pid in range(1, 103):
        activity = 90 if pid == 102 else 60
        listed.append({"id": pid, "is_unread": pid in (1, 102), "type": "medical",
                       "last_message": {"created_at": datetime.fromtimestamp(activity, timezone.utc).isoformat()}})
    calls = []

    class Adapter(MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            assert path == "/projects"
            page = params["page"]
            return {"projects": listed[(page-1)*100:page*100],
                    "paginate": {"timestamp": 100, "has_next": page == 1}}

        def fetch_unread_messages(self, pid, stamp):
            calls.append((pid, stamp))
            return mcs_contract.MessageBatch([], 1, True)

    # Count only commits that created history jobs. A per-project commit would
    # exhaust this tick before its first GET, even for a patient on page one.
    registered, commits = [], []

    def count_registration_commits(sql):
        if sql.lstrip().upper().startswith("INSERT INTO FETCH_JOBS"):
            registered.append(True)
        elif sql == "COMMIT" and registered:
            commits.append(len(registered))
            registered.clear()

    db.db.set_trace_callback(count_registration_commits)
    monkeypatch.setattr(collection_stages, "time", SimpleNamespace(monotonic=lambda: len(commits)))
    result = {"errors": [], "messages": 0, "new_messages": 0, "incomplete": []}
    collection_stages.stage_unread(Adapter(), db, result, 2, db.begin_run(None))
    assert calls == [(102, 100), (1, 100)]
    assert result["errors"] == []
    assert commits == [102]
    assert db.db.execute("SELECT count(*) FROM patients WHERE json_extract(initial_import, '$.boundary_ts')=100"
                         ).fetchone()[0] == 102
    assert db.history_jobs_due()[0]["project_id"] == 102


@pytest.mark.parametrize(("same_second", "listed_mid"), [(False, None), (True, 600), (True, None)])
def test_new_activity_in_read_room_gets_a_head_slot_during_initial_import(db, same_second, listed_mid):
    now = int(time.time())
    patients = [_unread_patient(pid) for pid in range(1, 7)]
    for patient in patients:
        db.upsert_patient_info(patient, is_archived=False, notify_new=True, observation_ts=now-120)
    active = patients[-1]
    active.last_activity = now
    if same_second:
        active.last_message_id = listed_mid
        db.save_messages([_message(mid=599, project_id=6, posted_at=datetime.fromtimestamp(
            now, timezone.utc).isoformat())], project_id=6)
    pending = db.job_payload("history", active.project_id)
    _capture(db, patients, latest=lambda pid: {"message_id": 600})
    visited = []

    def history(pid, since, *, max_pages, start_page):
        visited.append((pid, since, start_page))
        post = _message(mid=600, project_id=pid, posted_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
        posts = [post] if pid == active.project_id else []
        return mcs_contract.MessageBatch(posts, pages=1, reached=True, page_observations=[
            history_walk.observation(pid, since, 1, 10, False, [p.message_id for p in posts])])

    result = {"errors": []}
    job_ops.run_history_jobs(SimpleNamespace(fetch_history=history), db, result, time.monotonic()+60, max_jobs=2)
    assert visited == [(6, 0, 1), (1, 0, 1)]
    assert result["errors"] == []
    assert db.job_payload("history", active.project_id) == pending
    assert db.history_floor(active.project_id) != -1
    assert _events(db) and json.loads(_events(db)[0]["payload"])["message_ids"] == [600]
    _capture(db, patients, latest=lambda pid: {"message_id": 600})
    assert db.job_state("history_head", active.project_id, 600) == "done"


def test_unseen_reply_keeps_its_page_when_another_observation_arrives(db):
    from mcs_adapter import MCSAdapter

    now = int(time.time())
    patient = _unread_patient(1)
    db.upsert_patient_info(patient, is_archived=False, notify_new=True, observation_ts=now-120)
    # Both the independent full walk and a pending coverage repair keep their original cursor.
    history = db.job_pending("history", 1)
    db.job_defer(history["job_id"], 3600)
    db.job_add("history_head", 1, payload={"since": 0, "page": 17}, next_try=time.time()+3600)
    original = [db.job_payload(kind, 1) for kind in ("history", "history_head")]
    pages = []

    def post(mid, stamp):
        return {"id": mid, "project_id": 1, "comment": "synthetic",
                "created_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}

    class Wire(MCSAdapter):
        def _get(self, path, params=None, **kw):
            if path == "/projects/1/messages/33/messages":
                return {"messages": [post(1000, now-10)], "paginate": {"has_next": False}}
            assert path == "/projects/1/messages"
            page = params["page"]
            pages.append(page)
            row = post(page, now-3600)
            if page == 1 and patient.last_message_id == 1001:
                row = post(1001, now-9)
            elif page == 3:
                row = dict(post(33, now-3600), count={"thread_messages": 1})
            return {"messages": [row], "paginate": {"has_next": page < 40}}

    def drain():
        result = {"errors": []}
        job_ops.run_history_jobs(Wire(), db, result, time.monotonic()+60, max_jobs=1)
        assert result["errors"] == []

    patient.last_activity, patient.last_message_id = now-10, 1000
    _capture(db, [patient])
    drain()
    assert db.job_payload("history_head", 1, 1000)["page"] == 2
    patient.last_activity, patient.last_message_id = now-9, 1001
    _capture(db, [patient])
    drain()
    assert db.has_message(1001) and db.job_state("history_head", 1, 1001) == "done"
    drain()
    drain()
    assert db.db.execute("SELECT parent_id FROM messages WHERE message_id=1000").fetchone()[0] == 33
    assert db.job_state("history_head", 1, 1000) == "done"
    assert pages == [1, 1, 2, 3]
    assert [db.job_payload(kind, 1) for kind in ("history", "history_head")] == original
    assert db.coverage_ts(1) == 0 and db.history_floor(1) != -1


def test_unavailable_time_only_hint_keeps_known_activity_and_required_history(db):
    now = int(time.time())
    patients = [_unread_patient(pid) for pid in (1, 2, 3)]
    for p in patients:
        db.upsert_patient_info(p, is_archived=False, notify_new=True, observation_ts=now-30)
        p.last_activity = now
    patients[-1].last_message_id = 300
    probes = []

    def expired(pid):
        probes.append(pid)
        raise mcs_contract.SessionExpired()

    result = _capture(db, patients, latest=expired)
    assert probes == [1]
    assert result["errors"] == ["probe 1: session_expired"]
    assert db.job_state("history_head", 3, 300) == "pending"
    assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='history_head'").fetchone()[0] == 1
    assert all(db.job_state("history", p.project_id) == "pending" for p in patients)


def test_time_only_probe_rotates_after_failure_without_extra_gets(db):
    now = int(time.time())
    patients = [_unread_patient(pid) for pid in (1, 2)]
    for p in patients:
        db.upsert_patient_info(p, is_archived=False, notify_new=True, observation_ts=now-60)
        p.last_activity = now - p.project_id
    calls = []

    def latest(pid):
        calls.append(pid)
        if pid == 1:
            raise mcs_contract.MCSError("http_error", status=503)
        return {"message_id": 200}

    assert _capture(db, patients, latest=latest)["errors"] == ["probe 1: http_error"]
    assert calls == [1]
    assert _capture(db, patients, latest=latest)["errors"] == []
    assert calls == [1, 2]
    assert db.job_state("history_head", 2, 200) == "pending"
    assert all(db.job_state("history", p.project_id) == "pending" for p in patients)
