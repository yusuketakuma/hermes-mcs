"""Derivation must stop before a later stage commits an open transaction."""
import extract_llm_resident

import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

import collection_stages
import drug_map
import extract
import extract_llm
import health_report
import job_ops
import mcs_signals
import notify_cmds
import notify_digest
import notify_flush
import notify_spool
import notify_urgent
import rollup
import run_check
import stamp_write
from mcs_contract import MCSError, SessionExpired
from extract_testkit import _ledger, _message


def test_derive_does_not_take_a_callers_open_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        before = store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0]
        pending = 0 if before else 1
        store.db.execute("BEGIN")
        store.db.execute("UPDATE messages SET is_unread=? WHERE message_id=1", (pending,))
        calls = []
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: calls.append("extract"))
        result = {"errors": [], "realtime_ids": [1]}
        with pytest.raises(run_check.TickTransactionOpen):
            run_check.stage_derive(store, result, time.monotonic() + 600, {})
        assert result["errors"] == ["tick: transaction_open"] and calls == []
        assert store.db.in_transaction
        assert store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == pending
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == before
        store.db.rollback()
    finally:
        store.close()


def test_derive_stops_after_a_failed_stage_leaves_a_transaction_open(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        before = store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0]
        pending = 0 if before else 1
        calls = []

        def failed_stage(ledger, **kwargs):
            ledger.db.execute("UPDATE messages SET is_unread=? WHERE message_id=1", (pending,))
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(extract, "run_pending", failed_stage)
        monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **k: calls.append("llm"))
        monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
        result = {"errors": [], "realtime_ids": [1]}
        with pytest.raises(run_check.TickTransactionOpen):
            run_check.stage_derive(store, result, time.monotonic() + 600, {})
        assert result["errors"] == ["extract: RuntimeError", "tick: transaction_open"]
        assert calls == [] and store.db.in_transaction
        store.db.rollback()
        assert store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == before
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["derive_error", "jobs_code_changed", "fetch_deadline", "fetch_error"])
def test_tick_closes_open_transaction_without_delivery_or_run_commit(tmp_path, monkeypatch, capsys, mode):
    data = tmp_path / "data"
    data.mkdir()
    for name, value in {"HOME": tmp_path, "DB": data / "ledger.db",
                        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock"}.items():
        monkeypatch.setattr(collection_stages if name == "ATTACH_DIR" else run_check, name, str(value))
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    monkeypatch.setattr(run_check, "MCSAdapter", lambda **kw: SimpleNamespace(set_deadline=lambda deadline: None))
    monkeypatch.setattr(notify_spool, "restore_awaiting_consent", lambda *a: None)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *a: False)
    monkeypatch.setattr(run_check, "_code_changed", lambda *a: mode == "jobs_code_changed")
    clock = [time.monotonic()]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    calls = []
    monkeypatch.setattr(run_check, "_deliver", lambda *a: calls.append("delivery"))
    monkeypatch.setattr(run_check, "_fail_run", lambda *a: calls.append("finish"))
    monkeypatch.setattr(run_check, "_write_health", lambda *a, **k: calls.append("health"))

    def open_transaction(ledger):
        ledger.db.execute("UPDATE runs SET error='合成未確定' WHERE run_id=(SELECT MAX(run_id) FROM runs)")

    def failed_stage(ledger, **kwargs):
        open_transaction(ledger)
        raise RuntimeError("synthetic failure")

    def fetch(*args):
        if mode in ("fetch_deadline", "fetch_error"):
            open_transaction(args[1])
        if mode == "fetch_deadline":
            clock[0] += run_check.RUN_DEADLINE_S + 1
        if mode == "fetch_error":
            raise MCSError("network_error", retryable=True)

    def jobs(*args):
        if mode == "jobs_code_changed":
            open_transaction(args[1])

    monkeypatch.setattr(run_check, "_stage_fetch", fetch)
    monkeypatch.setattr(run_check, "_run_jobs", jobs)
    monkeypatch.setattr(extract, "run_pending", failed_stage)
    assert run_check._main() == 1
    assert calls == []
    assert "tick: transaction_open" in capsys.readouterr().out
    with sqlite3.connect(data / "ledger.db") as reader:
        assert reader.execute("SELECT error FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] != "合成未確定"


def test_derive_reports_reconcile_failure_without_echoing_its_contents(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {
            "done": 0, "pids": [], "reconcile_error": "PRIVATE_CANARY"})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(rollup, "scan_dirty_projects", lambda ledger, **kw: rollup.RollupScan((), (), False))
        monkeypatch.setattr(rollup, "rebuild_many", lambda *a, **k: 0)
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})
        result = {"errors": []}
        run_check.stage_derive(store, result, time.monotonic() + 600, {"synthetic": True}, llm_budget_cap=0)
        assert result["errors"] == ["intervention_reconcile_failed"]
        assert "PRIVATE_CANARY" not in str(result)
    finally:
        store.close()


def test_derive_reports_unknown_without_rebuilding_unknown_candidates(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": [2]})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(rollup, "scan_dirty_projects", lambda ledger, **kw: rollup.RollupScan((1,), (2,), True))
        rebuilt = []
        monkeypatch.setattr(rollup, "rebuild_many", lambda _db, pids, **kw: rebuilt.extend(pids) or len(pids))
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})
        result = {"errors": []}
        run_check.stage_derive(store, result, time.monotonic() + 600, {}, llm_budget_cap=0)
        assert rebuilt == [1]
        assert result["rollup_scan"] == {"state": "unknown", "unknown_observed": 1,
                                         "unknown_reasons": {}, "rechecked": 0}
        assert "rollup: scan_unknown" in result["errors"]
    finally:
        store.close()


def test_derive_reports_source_changed_without_disclosing_patient_id(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        monkeypatch.setattr(extract, 'run_pending', lambda *a, **k: {'done': 0, 'pids': []})
        monkeypatch.setattr(drug_map, 'configured', lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, 'derive', lambda *a, **k: {'pids': [], 'status': 'complete'})
        monkeypatch.setattr(rollup, 'scan_dirty_projects', lambda ledger, **kw: rollup.RollupScan((17, 18, 19), (), False))

        def rebuild(_ledger, pids, *, deadline, source_changed, source_unknown,
                    source_unknown_reasons, cfg):
            assert set(pids) == {17, 18, 19}
            source_changed.append(17)
            source_unknown.append(18)
            return 1

        monkeypatch.setattr(rollup, 'rebuild_many', rebuild)
        monkeypatch.setattr(mcs_signals, 'evaluate', lambda *a, **k: {'errors': []})
        result = {'errors': []}
        run_check.stage_derive(store, result, time.monotonic() + 600, {}, llm_budget_cap=0)
        assert result['rollups'] == 1
        assert result['rollup_source_changed'] == 1
        assert result['rollup_source_unknown'] == 1
        assert 'rollup: source_changed' in result['errors']
        assert 'rollup: source_unknown' in result['errors']
        assert '17' not in str(result['errors']) and '18' not in str(result['errors'])
    finally:
        store.close()


def test_delivery_stops_before_later_sends_when_digest_leaves_a_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []

        def failed_digest(ledger, cfg):
            ledger.db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(notify_digest, "maybe_enqueue", failed_digest)
        monkeypatch.setattr(notify_urgent, "maybe_enqueue", lambda *a: calls.append("urgent"))
        monkeypatch.setattr(notify_flush, "flush", lambda *a, **k: calls.append("send"))
        result = {"errors": []}
        with pytest.raises(run_check.TickTransactionOpen):
            run_check._deliver(store, SimpleNamespace(no_notify=False), {}, result, time.monotonic() + 600)
        assert calls == [] and store.db.in_transaction
        assert result["errors"] == ["daily_digest: RuntimeError", "tick: transaction_open"]
        store.db.rollback()
    finally:
        store.close()


def test_jobs_stop_before_stamp_when_commands_leave_a_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []
        monkeypatch.setattr(job_ops, "drain_commands", lambda *a: None)
        monkeypatch.setattr(notify_spool, "ensure_dirs", lambda *a: None)
        monkeypatch.setattr(notify_spool, "restore_pending", lambda *a: None)

        def failed_commands(ledger, *args, **kwargs):
            ledger.db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(notify_cmds, "drain_int_commands", failed_commands)
        monkeypatch.setattr(stamp_write, "enabled", lambda *a: calls.append("stamp"))
        monkeypatch.setattr(job_ops, "seed_discovery", lambda *a: calls.append("discovery"))
        result = {"errors": []}
        with pytest.raises(run_check.TickTransactionOpen):
            run_check._run_jobs(None, store, SimpleNamespace(), {}, result, time.monotonic() + 600, False, None)
        assert calls == [] and store.db.in_transaction
        assert result["errors"] == ["cmd_int: RuntimeError", "tick: transaction_open"]
        store.db.rollback()
    finally:
        store.close()


def test_session_expiry_does_not_retry_with_an_open_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []

        def expired():
            store.db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise SessionExpired("session_expired")

        monkeypatch.setattr(collection_stages, "attempt_relogin", lambda *a, **k: calls.append("login"))
        with pytest.raises(SessionExpired):
            collection_stages.with_relogin(None, store, {"errors": []}, "synthetic", expired)
        assert calls == [] and store.db.in_transaction
        store.db.rollback()
    finally:
        store.close()


def test_finish_stops_before_run_commit_when_tail_leaves_a_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []

        def failed_tail(ledger, *args, **kwargs):
            ledger.db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(notify_cmds, "drain_int_commands", failed_tail)
        monkeypatch.setattr(notify_spool, "publish_flags", lambda *a: calls.append("publish"))
        monkeypatch.setattr(store, "finish_run", lambda *a: calls.append("finish"))
        result = {"errors": [], "incomplete": []}
        with pytest.raises(run_check.TickTransactionOpen):
            run_check._finish_run(store, {}, result, 1, time.monotonic() + 600)
        assert calls == [] and store.db.in_transaction
        assert result["errors"] == ["cmd_int_tail: RuntimeError", "tick: transaction_open"]
        store.db.rollback()
    finally:
        store.close()


def test_failed_run_does_not_enqueue_or_send_after_recording_leaves_transaction(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []

        def failed_record(*args):
            store.db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(store, "finish_run", failed_record)
        monkeypatch.setattr(collection_stages, "queue_run_alert", lambda *a: calls.append("alert"))
        monkeypatch.setattr(notify_flush, "flush", lambda *a, **k: calls.append("send"))
        result = {"errors": []}
        run_check._fail_run(store, SimpleNamespace(no_notify=False), result, 1, "failed",
                            "synthetic", time.monotonic() + 600, alert="run_failed")
        assert calls == [] and store.db.in_transaction
        assert result["errors"] == ["synthetic", "run_record_failed: RuntimeError"]
        store.db.rollback()
    finally:
        store.close()


def test_health_failure_does_not_commit_a_transaction_left_by_builder(tmp_path, monkeypatch):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        calls = []

        def failed_builder(db, *args, **kwargs):
            db.execute("UPDATE messages SET is_unread=0 WHERE message_id=1")
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(health_report, "build_health", failed_builder)
        monkeypatch.setattr(store, "outbox_add", lambda *a, **k: calls.append("outbox"))
        result = {"errors": []}
        run_check._write_health(store, result, "failed")
        assert calls == [] and store.db.in_transaction
        assert result["errors"] == ["health_write_failed: RuntimeError"]
        store.db.rollback()
    finally:
        store.close()
