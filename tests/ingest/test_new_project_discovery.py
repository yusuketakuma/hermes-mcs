"""New read rooms enter the normal durable capture and notification path."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import collection_stages
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


def _capture(db, patients):
    adapter = SimpleNamespace(list_unread=lambda: mcs_contract.UnreadSnapshot(
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


def test_failed_first_registration_retry_starts_exactly_one_initial_history_and_progress_card(db, monkeypatch):
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
    assert history and {mid for event in history for mid in json.loads(event['payload'])['message_ids']} == {10}
    notify_cards.dispatch_intent(db, dict(history[-1]), SLACK, now=time.time() + 300)
    assert db.db.execute("SELECT count(*) FROM notification_cards WHERE kind='patient_history'").fetchone()[0] == 1
    specs = db.db.execute('SELECT spec_json FROM notification_renders').fetchall()
    assert specs
    for spec in specs:
        assert validate_slack(json.loads(spec[0]))


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
    assert {mid for event in history for mid in json.loads(event["payload"])["message_ids"]} == {10, 11, 20, 21}
    notify_cards.dispatch_intent(db, dict(history[-1]), config, now=time.time() + 300)
    renders = db.db.execute("SELECT spec_json FROM notification_renders").fetchall()
    assert renders
    for row in renders:
        spec = json.loads(row[0])
        assert validate(spec) is spec
        assert spec["delivery"].get("transport", "discord") == transport
    _capture(db, [_unread_patient(1)])
    assert db.job_state("history", 1) == "done"
    assert _events(db) == []
    assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='patient_history'").fetchone()[0] == len(history)
    # A later reply to a history root remains an individual live arrival.
    live = _posted(30, -1, parent=10)
    db.save_thread_replies([live], 1, notify={"source": "unread"}, notify_max_age_s=86400)
    [event] = _events(db)
    assert json.loads(event["payload"])["message_ids"] == [30]


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
