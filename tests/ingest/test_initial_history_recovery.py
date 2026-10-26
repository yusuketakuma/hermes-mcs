"""Synthetic retries preserve cohort/source evidence and never certify an unsaved thread."""
import copy
from contextlib import closing
import json
import time

import pytest

import initial_import_state as initial
import job_ops
import mcs_requests
import notify_spool
from ingest_testkit import _ledger, _message, _unread_patient
from test_mcs_operations import _command


def _failed(store, *, payload=None, reason="network_error", floor=0):
    with store.db:
        store.job_add_tx("history", 1, payload=payload or {"since": 0, "page": 7, "pages": 3}, reset_pending=True)
        job = store.db.execute("SELECT job_id FROM fetch_jobs WHERE kind='history' AND project_id=1 AND message_id=0").fetchone()[0]
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=8,reason_code=? WHERE job_id=?", (reason, job))
        store.db.execute("UPDATE patients SET history_floor=? WHERE project_id=1", (floor,))
    return job


def _restart(store, tmp_path, monkeypatch, route):
    if route == "scan":
        return mcs_requests.apply_command(store, _command("ops.scan", days=14, pages=4))
    monkeypatch.setattr(notify_spool, "restore_awaiting_consent", lambda path: None)
    commands = tmp_path / "commands"
    commands.mkdir()
    path = commands / "retry.json"
    path.write_text(json.dumps({"cmd": "import", "project_id": 1, "days": 14, "pages": 4}))
    result = {"errors": []}
    job_ops.drain_commands(store, result, str(commands))
    return {"outcome": "rejected" if result["errors"] else "applied", "errors": result["errors"], "consumed": not path.exists()}


@pytest.mark.parametrize("route", ["scan", "import"])
@pytest.mark.parametrize("cohort", [False, True])
@pytest.mark.parametrize("floor", [0, -1])
def test_failed_since_zero_without_walk_restarts_without_creating_or_replacing_cohort(tmp_path, monkeypatch, route, cohort, floor):
    with closing(_ledger(tmp_path)) as store:
        if cohort:
            store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                                      observation_ts=int(time.time()))
        else:
            store.ensure_patient(1)
        store.save_messages([_message(mid=10, body="wholly synthetic prior source")], project_id=1)
        original = initial.load(store.db, 1)
        sources = [tuple(row) for row in store.db.execute("SELECT * FROM messages")]
        intents = [tuple(row) for row in store.db.execute("SELECT * FROM notify_outbox")]
        job = _failed(store, floor=floor)
        assert _restart(store, tmp_path, monkeypatch, route)["outcome"] == "applied"
        row = store.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?", (job,)).fetchone()
        payload = json.loads(row["payload"])
        assert (row["state"], row["attempts"], row["reason_code"]) == ("pending", 0, None)
        assert payload == {"since": 0, "page": 1, "pages": 4, "stalls": 0, "trickle": False}
        assert store.history_floor(1) == floor
        current = initial.load(store.db, 1)
        assert (current is None) == (original is None)
        if original:
            assert {k: current[k] for k in ("cohort_id", "boundary_ts", "boundary_source", "started_at")} == {
                k: original[k] for k in ("cohort_id", "boundary_ts", "boundary_source", "started_at")}
        assert [tuple(row) for row in store.db.execute("SELECT * FROM messages")] == sources
        assert [tuple(row) for row in store.db.execute("SELECT * FROM notify_outbox")] == intents
        assert not store.db.in_transaction


@pytest.mark.parametrize("route", ["scan", "import"])
def test_restart_requeues_failed_cohort_fetches_without_claiming_they_were_observed(tmp_path, monkeypatch, route):
    with closing(_ledger(tmp_path)) as store:
        store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                                  observation_ts=int(time.time()))
        parent = _message(mid=10, body="synthetic history", posted_at="2026-09-01T00:00:00+09:00")
        future = _message(mid=20, body="synthetic live", posted_at="2090-01-01T00:00:00+09:00")
        store.save_messages([parent, future], project_id=1)
        assert initial.message_ids(store.db, 1) == [10]
        jobs = [store.job_add("thread", 1, mid, parent_id=mid) for mid in (10, 20)]
        jobs.append(store.job_add("reply", 1, 11, parent_id=10))  # not fetched yet
        with store.db:
            store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=8,reason_code='network_error' WHERE kind IN ('thread','reply')")
        _failed(store)
        assert _restart(store, tmp_path, monkeypatch, route)["outcome"] == "applied"
        rows = {r["job_id"]: r for r in store.db.execute("SELECT * FROM fetch_jobs")}
        for job in (jobs[0], jobs[2]):
            assert (rows[job]["state"], rows[job]["attempts"], rows[job]["reason_code"]) == ("pending", 0, None)
        assert (rows[jobs[1]]["state"], rows[jobs[1]]["attempts"]) == ("failed", 8)
        assert initial.load(store.db, 1)["capture_phase"] == "capturing"
        assert not store.db.execute("SELECT 1 FROM messages WHERE message_id=11").fetchone()


def test_unknown_import_is_rejected_without_bypassing_real_registration(tmp_path, monkeypatch):
    with closing(_ledger(tmp_path)) as store:
        result = _restart(store, tmp_path, monkeypatch, "import")
        assert result["errors"] == ["cmd_invalid: project_not_found"]
        assert not store.db.execute("SELECT 1 FROM patients").fetchone()
        assert not store.db.execute("SELECT 1 FROM fetch_jobs").fetchone()
        assert not store.db.execute("SELECT 1 FROM notify_outbox").fetchone()
        store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                                  observation_ts=int(time.time()))
        assert initial.load(store.db, 1)["capture_phase"] == "capturing"
        assert store.job_payload("history", 1)["since"] == 0
        assert store.job_payload("history", 1)["notify"] is True
        assert store.db.execute("SELECT 1 FROM notify_outbox WHERE kind='patient_history'").fetchone()


@pytest.mark.parametrize("route", ["scan", "import"])
def test_retry_failure_rolls_back_history_cohort_and_receipt_or_command_consumption(tmp_path, monkeypatch, route):
    with closing(_ledger(tmp_path)) as store:
        store.upsert_patient_info(_unread_patient(1), is_archived=False, notify_new=True,
                                  observation_ts=int(time.time()))
        _failed(store)
        before = {table: [tuple(row) for row in store.db.execute("SELECT * FROM " + table)]
                  for table in ("patients", "fetch_jobs", "command_receipts", "notify_outbox")}
        original = initial.restart_history_tx
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic_restart_failure")
        monkeypatch.setattr(initial, "restart_history_tx", fail)
        with pytest.raises(RuntimeError, match="synthetic_restart_failure"):
            _restart(store, tmp_path, monkeypatch, route)
        assert {table: [tuple(row) for row in store.db.execute("SELECT * FROM " + table)] for table in before} == before
        if route == "import":
            assert (tmp_path / "commands/retry.json").is_file()


def _parent():
    parent = _message(mid=10, body="synthetic root", project_id=1)
    parent.reply_count = 2
    parent.replies = [_message(mid=11, parent_id=10, body="synthetic reply A", project_id=1),
                      _message(mid=12, parent_id=10, body="synthetic reply B", project_id=1)]
    return parent


@pytest.mark.parametrize("embedded", [False, True])
def test_fresh_full_thread_only_retires_failed_job_after_matching_bodies_are_durable(tmp_path, embedded):
    with closing(_ledger(tmp_path)) as store:
        store.ensure_patient(1)
        job = store.job_add("thread", 1, 10, parent_id=10)
        store.job_fail(job, reason_code="network_error")
        with store.db:
            store.db.execute("UPDATE fetch_jobs SET attempts=8 WHERE job_id=?", (job,))
        parent = _parent()
        fetched = copy.deepcopy(parent.replies)
        class Adapter:
            def fetch_thread(self, pid, mid, **kwargs):
                assert (pid, mid) == (1, 10)
                return copy.deepcopy(fetched)
        if not embedded:
            parent.replies = []
        def merge():
            return job_ops.merge_full_replies(Adapter(), [parent], 0, time.monotonic() + 60, {"errors": []}, ledger=store)
        assert merge().checkpoint_safe
        assert store.job_state("thread", 1, 10) == "failed"  # success is not a durable save
        store.save_messages([parent], project_id=1)
        if not embedded:
            parent.replies = []
        assert merge().checkpoint_safe
        assert store.job_state("thread", 1, 10) == "done"
        assert store.db.execute("SELECT attempts FROM fetch_jobs WHERE job_id=?", (job,)).fetchone()[0] == 8


@pytest.mark.parametrize("bad", ["foreign_project", "foreign_parent", "duplicate", "partial", "unknown",
                                  "deleted", "old_body", "snippet", "zero", "fetch_error"])
def test_failed_thread_cannot_be_cleared_by_mismatched_or_incomplete_proof(tmp_path, bad):
    with closing(_ledger(tmp_path)) as store:
        store.ensure_patient(1)
        stored = _parent()
        store.save_messages([stored], project_id=1)
        job = store.job_add("thread", 1, 10, parent_id=10)
        store.job_fail(job, reason_code="network_error")
        parent, replies = copy.deepcopy(stored), copy.deepcopy(stored.replies)
        parent.replies = []
        if bad == "foreign_project":
            replies[0].project_id = 2
        elif bad == "foreign_parent":
            replies[0].parent_id = 20
        elif bad == "duplicate":
            replies[1] = copy.deepcopy(replies[0])
        elif bad == "partial":
            replies.pop()
        elif bad in ("unknown", "deleted", "snippet"):
            replies[0].body_state = bad
        elif bad == "old_body":
            replies[0].body_html = "different synthetic body not durably saved"
        elif bad == "zero":
            parent.reply_count = 0
            replies = []
        class Adapter:
            def fetch_thread(self, pid, mid, **kwargs):
                if bad == "fetch_error":
                    from mcs_contract import MCSError
                    raise MCSError("network_error", retryable=True)
                return replies
        job_ops.merge_full_replies(Adapter(), [parent], 0, time.monotonic() + 60, {"errors": []}, ledger=store)
        assert store.job_state("thread", 1, 10) != "done"
        assert not store.db.in_transaction


def test_verified_thread_retirement_borrows_and_never_commits_the_callers_transaction(tmp_path):
    with closing(_ledger(tmp_path)) as store:
        store.ensure_patient(1)
        parent = _parent()
        store.save_messages([parent], project_id=1)
        job = store.job_add("thread", 1, 10, parent_id=10)
        store.job_fail(job, reason_code="network_error")
        with pytest.raises(RuntimeError, match="synthetic_owner_rollback"):
            with store.db:
                store.db.execute("UPDATE fetch_jobs SET job_id=job_id WHERE 0")
                job_ops.merge_full_replies(object(), [parent], 0, time.monotonic() + 60,
                                          {"errors": []}, ledger=store)
                assert store.db.in_transaction and store.job_state("thread", 1, 10) == "done"
                raise RuntimeError("synthetic_owner_rollback")
        assert store.job_state("thread", 1, 10) == "failed"
        assert not store.db.in_transaction
