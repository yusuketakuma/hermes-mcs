"""New read rooms enter the normal durable capture and notification path."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import collection_stages
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


@pytest.mark.parametrize("config,validate,transport", [
    (SLACK, validate_slack, "slack"), (CFG, validate_discord, "discord"),
    (LINEWORKS, validate_lineworks, "lineworks"),
])
@pytest.mark.parametrize("pre_stored", [False, True])
def test_read_new_room_is_registered_and_recent_posts_reach_chat_spec(
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
    adapter = SimpleNamespace(fetch_history=lambda *a, **kw: mcs_contract.MessageBatch(
        [recent, old], pages=1, reached=True))
    result = {"errors": []}
    job_ops.run_history_jobs(adapter, db, result, time.monotonic() + 60,
                             notify_max_age_s=86400)
    assert result["errors"] == []
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
    events = _events(db)
    assert len(events) == 1
    assert set(json.loads(events[0]["payload"])["message_ids"]) == {10, 11}
    assert json.loads(events[0]["payload"])["source"] == "discovery"
    notify_cards.dispatch_intent(db, dict(events[0]), config, now=time.time() + 300)
    renders = db.db.execute("SELECT spec_json FROM notification_renders").fetchall()
    assert renders
    for row in renders:
        spec = json.loads(row[0])
        assert validate(spec) is spec
        assert spec["delivery"].get("transport", "discord") == transport
    _capture(db, [_unread_patient(1)])
    assert db.job_state("history", 1) == "done"
    assert len(_events(db)) == 1


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
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs["start_page"])
        return mcs_contract.MessageBatch([message], pages=3, reached=len(calls) > 1)
    adapter = SimpleNamespace(fetch_history=fetch)
    for _ in range(2):
        job_ops.run_history_jobs(adapter, db, {"errors": []}, time.monotonic() + 60,
                                 notify_max_age_s=86400)
    assert calls == [1, 3]
    assert db.job_state("history", 1) == "done"
    assert len(_events(db)) == 1


@pytest.mark.parametrize("age_limit,expected", [(None, {20, 30}), (3600, {30})])
def test_initial_walk_is_age_bounded_without_hiding_later_pinned_pages(db, age_limit, expected):
    _capture(db, [_unread_patient(1)])
    pages = iter([[_posted(10, 30 * 86400)], [_posted(20, 7200), _posted(30, 300)]])
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs["start_page"])
        return mcs_contract.MessageBatch(next(pages), pages=3, reached=len(calls) > 1)
    adapter = SimpleNamespace(fetch_history=fetch)
    job_ops.run_history_jobs(adapter, db, {"errors": []}, time.monotonic() + 60,
                             notify_max_age_s=age_limit)
    assert _events(db) == []
    job_ops.run_history_jobs(adapter, db, {"errors": []}, time.monotonic() + 60,
                             notify_max_age_s=age_limit)
    assert set(json.loads(_events(db)[0]["payload"])["message_ids"]) == expected
    assert calls == [1, 3] and db.job_state("history", 1) == "done"
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
