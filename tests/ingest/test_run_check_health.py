"""Collection health evidence, source coverage and safe failure reporting."""
import collection_stages
import health_policy
import safe_io
import health_report
import json
import os
import sys
import time
import pytest
import extract_llm
import notify_flush
import run_check
from ingest_testkit import _ledger, _message
from run_check_stage_testkit import _lagging_patient, _pinned_history_adapter
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard


def test_health_counts_held_sends_and_age_from_creation(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    db.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES('extract_llm',1,'{}','{')")
    db.db.commit()
    db.job_add("extract_qc", 1, 1)
    eid = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    db.outbox_hold(eid)
    other = db.outbox_add("new_messages", 1, {"message_ids": [2]})
    now = time.time()
    db.db.execute("UPDATE notify_outbox SET created_at=?,next_try=? WHERE event_id=?",
                  (now - 1000, now + 500, other))
    db.db.execute("UPDATE fetch_jobs SET created_at=?,next_try=? WHERE kind='extract_qc'",
                  (now - 1000, now + 500))
    db.db.commit()
    health = health_report.build_health(db.db, {"notify": {}, "errors": [], "coverage_gaps": [{}]}, "partial")
    # coverage lag alone is information; the held send still degrades
    assert health["overall"] == "degraded" and health["collection"] == "ok"
    assert health["coverage_lagging"] == {"count": 1, "max_lag_s": 0}
    assert health["notify"]["held"] == 1
    assert health["notify"]["oldest_age_s"] >= 1000
    assert health["extract_qc_jobs"]["pending"] == 1
    assert health["extract_qc_jobs"]["oldest_age_s"] >= 1000
    coverage = health["extract_v2_coverage"]
    assert coverage["eligible"] == 1 and coverage["current"] == 0
    assert coverage["poison_gated"] == 1 and coverage["ratio"] == 0
    assert coverage["extract_version"] == extract_llm.EXTRACT_VERSION
    db.close()


@pytest.mark.parametrize(("progress", "reason"), [
    ('{"hold_reason":"send_outcome_unknown"}', "send_outcome_unknown"),
    ('{"hold_reason":"restore_text_unverified"}', "restore_text_unverified"),
    ('{"hold_reason":"payload_or_progress_invalid"}', "payload_or_progress_invalid"),
    (None, "not_recorded"),
    ("not-json", "not_recorded"),
    ("[]", "not_recorded"),
    ('{"hold_reason":"SYNTHETIC_PRIVATE_REASON"}', "unknown"),
    ('{"hold_reason":{"body":"SYNTHETIC_PRIVATE_REASON"}}', "unknown"),
])
def test_health_groups_only_safe_held_reason_codes(tmp_path, monkeypatch, progress, reason):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check.time, "time", lambda: 1000.0)
    try:
        for _ in range(2):
            eid = db.outbox_add("run_failed", None, {})
            db.outbox_hold(eid)
            db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                          (progress, eid))
        # A reason on a pending event must not become held evidence.
        pending = db.outbox_add("run_failed", None, {})
        db.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?",
                      (progress, pending))
        db.db.commit()
        health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
        assert health["overall"] == "degraded"
        assert health["notify"]["held"] == 2
        assert health["notify"]["held_reasons"] == {reason: 2}
        assert health["notify"]["pending"] == 1
        assert "SYNTHETIC_PRIVATE_REASON" not in json.dumps(health)
    finally:
        db.close()


@pytest.mark.parametrize(("cfg", "expected_held", "expected_retired"), [
    ({"notify": {"interactive": "slack"}}, 3, 1),
    ({"notify": {"interactive": "discord"}}, 3, 1),
    ({"notify": {"interactive": False}}, 4, 0),
    (None, 4, 0),
])
def test_health_retired_holds_preserve_live_reason_evidence(
        tmp_path, monkeypatch, cfg, expected_held, expected_retired):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check, "_config", lambda: pytest.fail("health must use supplied config"))
    try:
        for route, transport in (("interactive", "discord"),
                                 ("interactive", "slack"),
                                 ("interactive", None), ("text", "discord")):
            eid = db.outbox_add("new_messages", None, {})
            db.outbox_hold(eid, "send_outcome_unknown")
            db.db.execute("UPDATE notify_outbox SET route=? WHERE event_id=?", (route, eid))
            if transport:
                db.db.execute(
                    "INSERT INTO notification_intent_batches("
                    "event_id,frozen_payload,payload_hash,route_epoch,transport,sealed_at) "
                    "VALUES(?,'{}','synthetic',1,?,1000)", (eid, transport))
        db.db.commit()
        health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok", cfg=cfg)
        assert health["notify"]["held"] == expected_held
        assert health["notify"]["retired_held"] == expected_retired
        assert health["notify"]["held_reasons"] == {"send_outcome_unknown": expected_held}
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE state='failed'").fetchone()[0] == 4
        assert health["overall"] == "degraded"
    finally:
        db.close()


def test_health_retired_only_hold_no_longer_degrades_live_delivery(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    try:
        eid = db.outbox_add("new_messages", None, {})
        db.outbox_hold(eid, "send_outcome_unknown")
        db.db.execute("UPDATE notify_outbox SET route='interactive' WHERE event_id=?", (eid,))
        db.db.execute(
            "INSERT INTO notification_intent_batches("
            "event_id,frozen_payload,payload_hash,route_epoch,transport,sealed_at) "
            "VALUES(?,'{}','synthetic',1,'discord',1000)", (eid,))
        db.db.commit()
        health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok",
                                   cfg={"notify": {"interactive": "slack"}})
        assert health["notify"]["held"] == 0
        assert health["notify"]["held_reasons"] == {}
        assert health["notify"]["retired_held"] == 1
        assert health["notify"]["state"] == "ok"
        assert health["overall"] == "ok"
        assert "notification_held" not in health["state_reasons"]
        assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()[0] == "failed"
    finally:
        db.close()


@pytest.mark.parametrize(("previous", "last_ok"), [
    ({}, None),
    ({"overall": "ok", "at": 100.0}, 100.0),
    ({"overall": "degraded", "last_ok_at": 100.0}, 100.0),
    ({"overall": "failed", "at": 100.0}, None),
    ({"last_ok_at": None}, None),
    ({"last_ok_at": True}, None),
    ({"last_ok_at": "100"}, None),
    ({"last_ok_at": -1}, None),
    ({"last_ok_at": float("nan")}, None),
    ({"last_ok_at": float("inf")}, None),
])
def test_health_preserves_known_success_without_fabricating_it(
        tmp_path, monkeypatch, previous, last_ok):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: previous)
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check.time, "time", lambda: 1000.0)
    try:
        health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "failed")
        assert health["overall"] == "failed"
        assert health["last_ok_at"] == last_ok
        assert health["state_reasons"] == ["run_failed"]
    finally:
        db.close()


def test_health_explains_failures_without_changing_overall(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {"last_ok_at": 100.0})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check.time, "time", lambda: 1000.0)
    try:
        db.outbox_hold(db.outbox_add("new_messages", 1, {"message_ids": [1]}))
        result = {"errors": ["synthetic stage error"], "incomplete": [1],
                  "notify": {"failed": 1, "skipped": 1, "parked": 1},
                  "backup_skipped": "disk_low"}
        health = health_report.build_health(db.db, result, "partial")
        assert health["overall"] == "degraded"
        assert health["last_ok_at"] == 100.0
        assert health["state_reasons"] == [
            "stage_errors", "collection_incomplete", "notification_failed",
            "notification_deferred", "notification_parked", "notification_held",
            "disk_low"]
        assert health["notify"]["held"] == 1
        healthy = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
        assert healthy["overall"] == "degraded"
        db.db.execute("UPDATE notify_outbox SET state='sent'")
        db.db.commit()
        healthy = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
        assert healthy["overall"] == "ok"
        assert healthy["last_ok_at"] == 1000.0
        assert healthy["state_reasons"] == []
    finally:
        db.close()


def test_health_code_changed_does_not_degrade(tmp_path, monkeypatch):
    """A mid-tick mcs/ tree change is a deploy guard, not a fault: the
    run row still records it, but health stays ok and only reports the
    informational reason."""
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {"last_ok_at": 100.0})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check.time, "time", lambda: 1000.0)
    try:
        health = health_report.build_health(db.db, {"errors": ["code_changed"],
                                        "notify": {}}, "partial")
        assert health["overall"] == "ok"
        assert health["last_ok_at"] == 1000.0
        assert health["state_reasons"] == ["code_changed"]
        real = health_report.build_health(
            db.db, {"errors": ["code_changed", "synthetic stage error"],
                 "notify": {}}, "partial")
        assert real["overall"] == "degraded"
        assert real["state_reasons"][:2] == ["stage_errors", "code_changed"]
    finally:
        db.close()


@pytest.mark.parametrize("content,project,extra", [
    ("{}", 2, {}), ("{}", 1, {"error": "failed"}),
    ("[]", 1, {}), ("invalid", 1, {}),
])
def test_health_coverage_requires_usable_scoped_extraction(tmp_path, content, project, extra):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message()])
        source_hash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        if project != 1:
            # Plant pre-guard corruption to verify the reader's independent scope gate.
            with db.db:
                for suffix in ("ins", "upd"):
                    db.db.execute(f"DROP TRIGGER g1_artifacts_msg_{suffix}")
        db.artifact_add("extract_llm", content, project_id=project, message_id=1,
                        meta={"hash": source_hash, "extract_version": extract_llm.EXTRACT_VERSION,
                              **extra})
        health = health_report.build_health(db.db, {"errors": []}, "ok")
        assert health["extract_v2_coverage"]["current"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("hours", [float("inf"), 10**400, 1e308],
                         ids=["infinite", "huge_integer", "conversion_overflow"])
def test_notify_age_rejects_nonfinite_seconds(hours):
    result = {"errors": []}
    assert run_check._notify_max_age_s({"notify_max_age_h": hours}, result) is None
    assert result["errors"] == ["config: notify_max_age_h_invalid"]


def test_begin_run_failure_closes_writer_and_reports_safe_error(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    for name, value in {"HOME": tmp_path, "DB": data / "ledger.db",
                        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
                        "HEALTH_FILE": data / "health.json"}.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value))

    def fail_begin(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_CANARY")

    monkeypatch.setattr(run_check.Ledger, "begin_run", fail_begin)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check.main() == 1
    output = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE_CANARY" not in output
    assert "crash: RuntimeError" in output
    descriptor = run_check.acquire_run_lock(run_check.LOCKFILE)
    assert descriptor is not None
    os.close(descriptor)


def test_health_write_binds_run_and_survives_failure(tmp_path, monkeypatch):
    """A health.json write failure must not roll back committed ingest
    work nor mark the stored run failed — it raises a separate
    machine-readable 'health_write_failed' incident instead (GAP-1)."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    result = {"errors": [], "notify": {}}
    monkeypatch.setattr(health_report, 'HEALTH_FILE',
                        str(tmp_path / "health.json"))

    # happy path first — fresh present evidence bound to its source run
    run_check._write_health(db, result, "ok", run_id=7)
    h = json.loads((tmp_path / "health.json").read_text())
    assert h["overall"] == "ok" and h["run_id"] == 7 and h["at"]

    # now break the atomic publish: stored messages stay, run row stays
    # 'ok', and exactly one incident lands in the durable outbox
    def boom(path, text):
        raise OSError("synthetic full disk")
    monkeypatch.setattr(safe_io, 'atomic_publish_text', boom)
    run_id = db.begin_run(None, "tick")
    db.finish_run(run_id, "ok", "")
    run_check._write_health(db, result, "ok", run_id=run_id)
    assert any(e.startswith("health_write_failed")
               for e in result["errors"])
    kinds = [r[0] for r in db.db.execute(
        "SELECT kind FROM notify_outbox WHERE kind='health_write_failed'")]
    assert kinds == ["health_write_failed"]
    assert db.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.db.execute("SELECT status FROM runs WHERE run_id=?",
                         (run_id,)).fetchone()[0] == "ok"
    db.close()


def test_deeply_nested_previous_health_does_not_block_fresh_publication(tmp_path, monkeypatch):
    import health_watch

    path = tmp_path / "health.json"
    path.write_bytes(b'{"nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}')
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(path))
    monkeypatch.setattr(run_check.time, "time", lambda: 2000.0)
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message()])
        result = {"errors": [], "notify": {}}
        run_check._write_health(db, result, "ok", run_id=7, cfg={})
        assert result["errors"] == []
        fresh = json.loads(path.read_text())
        assert fresh["at"] == fresh["unread_at"] == 2000
        assert fresh["overall"] == "ok" and fresh["run_id"] == 7
        assert health_watch.classify_health(str(path), 2001, 1800)["status"] == "ok"
        assert db.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert db.db.execute(
            "SELECT COUNT(*) FROM notify_outbox WHERE kind='health_write_failed'"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_backup_health_database_failure_remains_machine_readable(monkeypatch):
    import sqlite3
    import backup_evidence
    import backup_config
    monkeypatch.setattr(backup_config, "validate_backup_config", lambda setting: [])
    monkeypatch.setattr(backup_evidence, "load_policy", lambda path: (object(), True))

    def fail(*args):
        raise sqlite3.DatabaseError("synthetic database failure")

    monkeypatch.setattr(backup_evidence, "status", fail)
    assert health_report.backup_health(
        {"backup": {"enabled": True, "policy": "synthetic-unused"}}, 1000) == {
            "state": "failed", "reasons": ["backup_io_or_policy_failed"]}


def test_health_write_survives_a_watchdog_deadline_mid_observation(tmp_path, monkeypatch):
    """Once the watchdog's hard deadline is reached, every health observation
    must stop at it — health.json is still published with honest unknowns,
    on a live connection, instead of the process dying mid-write (the 30s
    pool may not claim more than the watchdog leaves)."""
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'HEALTH_FILE',
                        str(tmp_path / "health.json"))
    monkeypatch.setattr(health_report, 'attachment_free_mb',
                        lambda path=None: 10_000)
    # the deadline already passed: no SQL read may claim a fresh 30s window
    monkeypatch.setattr(run_check, "_hard_deadline",
                        lambda: time.monotonic() - 1)
    result = {"errors": [], "notify": {}}
    run_check._write_health(db, result, "ok", run_id=7)
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["notify"]["pending"] is None
    assert health["notify"]["pending_by_reason"]["reason"] == "observation_limit"
    assert health["notify"]["state"] == "incomplete"
    assert health["overall"] == "degraded"
    assert "notification_pending_unavailable" in health["state_reasons"]
    # the tick's connection survived: no leaked savepoint or progress handler
    assert db.db.execute("SELECT 1").fetchone()[0] == 1
    db.close()


def test_jobs_only_health_keeps_last_unread_time(tmp_path, monkeypatch):
    """A --jobs-only deep run refreshes `at` but not `unread_at`, so the
    health watcher still sees a stopped unread check as stale."""
    import health_watch
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'HEALTH_FILE',
                        str(tmp_path / "health.json"))
    run_check._write_health(db, {"errors": [], "notify": {}}, "ok")
    first = json.loads((tmp_path / "health.json").read_text())
    assert first["unread_at"] == first["at"]
    run_check._write_health(
        db, {"errors": [], "notify": {}, "jobs_only": True}, "ok")
    deep = json.loads((tmp_path / "health.json").read_text())
    assert deep["unread_at"] == first["unread_at"]
    later = first["unread_at"] + 7200
    deep["at"] = later - 60
    (tmp_path / "health.json").write_text(json.dumps(deep))
    report = health_watch.classify_health(
        str(tmp_path / "health.json"), later, 1800)
    assert report["status"] == "stale"
    db.close()


def test_unread_at_ignores_huge_integer_in_previous_health(tmp_path,
                                                           monkeypatch):
    """An oversized integer literal in the previous health.json must
    read as no value, not raise OverflowError out of health writing."""
    (tmp_path / "health.json").write_text('{"unread_at": 1' + "0" * 400 + "}")
    monkeypatch.setattr(health_report, 'HEALTH_FILE',
                        str(tmp_path / "health.json"))
    assert health_report.unread_at({"jobs_only": True}, "ok", 1.0) is None


def test_coverage_lag_is_information_not_an_error(tmp_path, monkeypatch):
    db = _lagging_patient(tmp_path)
    result = {"errors": [], "backfilled": 0, "incomplete": [], "notify": {}}
    collection_stages.stage_backfill(_pinned_history_adapter(), db, result,
                             time.monotonic() + 300, 1)
    assert result["coverage_gaps"] and result["coverage_gaps"][0]["lag_s"] > 0
    assert result["errors"] == []
    assert not result.get("coverage_stalled")
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / "h.json"))
    health = health_report.build_health(db.db, result, "ok")
    assert health["collection"] == "ok" and health["overall"] == "ok"
    assert health["coverage_lagging"]["count"] == 1
    assert health["coverage_lagging"]["max_lag_s"] > 0
    assert health["coverage_stalled"] == []
    db.close()


@pytest.mark.parametrize("reason", ["failed", "not_advancing"])
def test_stalled_history_head_escalates_collection(tmp_path, monkeypatch,
                                                   reason):
    db = _lagging_patient(tmp_path)
    adapter = _pinned_history_adapter()
    collection_stages.stage_backfill(adapter, db, {"errors": [], "backfilled": 0},
                             time.monotonic() + 300, 1)
    if reason == "failed":
        db.db.execute("UPDATE fetch_jobs SET state='failed' "
                      "WHERE kind='history_head'")
    else:
        db.db.execute("UPDATE fetch_jobs SET updated_at=? "
                      "WHERE kind='history_head'",
                      (time.time() - health_policy.COVERAGE_STALL_S - 60,))
    db.db.commit()
    result = {"errors": [], "backfilled": 0, "incomplete": [], "notify": {}}
    collection_stages.stage_backfill(adapter, db, result, time.monotonic() + 300, 1)
    assert result["coverage_stalled"][0]["pid"] == 1
    assert result["coverage_stalled"][0]["reason"] == reason
    assert result["errors"] == [f"backfill 1: coverage_stalled_{reason}"]
    # a failed head is still revived so the walk keeps retrying
    assert db.job_state("history_head", 1) == "pending"
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / "h.json"))
    health = health_report.build_health(db.db, result, "partial")
    assert health["collection"] == "incomplete"
    assert health["overall"] == "degraded"
    assert health["coverage_stalled"] == [1]
    db.close()


def test_jobs_only_health_carries_last_collection_verdict(tmp_path,
                                                         monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'HEALTH_FILE',
                        str(tmp_path / "health.json"))
    run_check._write_health(db, {"errors": ["project 7: x"], "notify": {},
                                 "incomplete": [7]}, "partial")
    run_check._write_health(
        db, {"errors": [], "notify": {}, "jobs_only": True}, "ok")
    deep = json.loads((tmp_path / "health.json").read_text())
    assert deep["collection"] == "incomplete"
    assert deep["incomplete_projects"] == [7]
    assert deep["overall"] == "degraded"
    # the next full tick decides afresh
    run_check._write_health(db, {"errors": [], "notify": {}}, "ok")
    tick = json.loads((tmp_path / "health.json").read_text())
    assert tick["collection"] == "ok" and tick["overall"] == "ok"
    db.close()


def test_health_reports_disk_state(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / "h.json"))
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "512")
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 1000.0)
    health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
    assert health["disk_free_mb"] == 1000 and health["disk_low"] is True
    assert health["overall"] == "ok"      # disk has its own alert path
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 1e6)
    assert not health_report.build_health(db.db, {"errors": [], "notify": {}},
                                 "ok")["disk_low"]
    assert health_report.build_health(db.db, {"errors": [], "notify": {},
                                  "backup_skipped": "disk_low"},
                             "ok")["disk_low"] is True
    db.close()


def test_backup_skip_is_recorded(monkeypatch):
    import maintenance
    monkeypatch.setattr(maintenance, "daily_backup",
                        lambda *a: "skipped_disk_low")
    monkeypatch.setattr(maintenance, "rotate_log", lambda: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    result = {"errors": []}
    run_check._housekeeping(result)
    assert result == {"errors": [], "backup_skipped": "disk_low"}


def test_health_attention_splits_failures_unstarted_and_unsettled(tmp_path):
    """'degraded' never said WHERE — attention separates a fresh
    failure, due-but-never-attempted backlog (the QC starvation
    signature), and old unsettled deliveries."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    now = time.time()
    db.job_add("extract_qc", 1, 1)
    db.job_add("semantic", 1, 1)
    db.db.execute(
        "UPDATE fetch_jobs SET state='failed',updated_at=? "
        "WHERE kind='semantic'", (now - 60,))
    # the starvation signature is never-touched (updated_at==created_at)
    db.db.execute(
        "UPDATE fetch_jobs SET created_at=?,updated_at=?,next_try=? "
        "WHERE kind='extract_qc'", (now - 1000, now - 1000, now - 10))
    db.db.commit()
    att = health_report.build_health(db.db, {"notify": {}, "errors": []}, "ok")["attention"]
    assert att["failed_jobs_24h"] == {"semantic": 1}
    assert att["failed_jobs_latest_at"] == now - 60
    qc = att["pending_unstarted"]["extract_qc"]
    assert qc["count"] == 1 and qc["oldest_age_s"] >= 1000
    assert att["delivery_unsettled"] == {"attempts": 0, "oldest_age_s": 0}
    # a not-yet-due unstarted row is scheduled, not stuck
    db.db.execute(
        "UPDATE fetch_jobs SET next_try=? WHERE kind='extract_qc'",
        (now + 500,))
    db.db.commit()
    # and a rotation job (reconcile shape: attempts=0 but served —
    # updated_at moved past created_at) is not a starvation signal
    db.job_add("reconcile", 1, 2)
    db.db.execute(
        "UPDATE fetch_jobs SET created_at=? WHERE kind='reconcile'",
        (now - 1000,))
    # nor is an expired extract claim lease (a lease, not work)
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,state,attempts,"
        "next_try,created_at,updated_at) VALUES('extract_claim',1,3,"
        "'pending',0,?,?,?)", (now - 100, now - 1000, now - 1000))
    db.db.commit()
    att = health_report.build_health(db.db, {"notify": {}, "errors": []}, "ok")["attention"]
    assert att["pending_unstarted"] == {}
    db.close()


def test_card_route_holds_record_reason_for_health(tmp_path, monkeypatch):
    import notify_cards
    import notify_scope
    db = _ledger(tmp_path)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(notify_scope, "interactive_enabled", lambda cfg: True)

    def boom(*_a, **_k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(notify_cards, "dispatch_intent", boom)
    try:
        eid = db.outbox_add("new_messages", None, {})
        db.db.execute("UPDATE notify_outbox SET route='interactive',attempts=4,"
                      "progress='not-json' WHERE event_id=?", (eid,))
        db.db.commit()
        ev = db.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                           (eid,)).fetchone()
        res = {"failed": 0}
        assert notify_flush._dispatch_interactive(db, ev, {}, res) is True
        monkeypatch.setattr(notify_cards, "dispatch_intent",
                            lambda *_a, **_k: {"error": "delivery_scope_missing"})
        stuck = db.outbox_add("new_messages", None, {})
        db.db.execute("UPDATE notify_outbox SET route='interactive',attempts=4"
                      " WHERE event_id=?", (stuck,))
        db.db.commit()
        assert notify_flush._dispatch_interactive(db, db.db.execute(
            "SELECT * FROM notify_outbox WHERE event_id=?", (stuck,)).fetchone(),
            {}, res) is True
        receipt = db.outbox_add("run_failed", None, {})
        db.db.execute("UPDATE notify_outbox SET progress='{\"next\":1}' "
                      "WHERE event_id=?", (receipt,))
        db.outbox_hold(receipt, "payload_invalid")
        rows = {r["event_id"]: json.loads(r["progress"]) for r in db.db.execute(
            "SELECT event_id,progress FROM notify_outbox")}
        # the existing receipt survives; an unparseable one is preserved
        assert rows[eid] == {"invalid_progress": "not-json",
                             "hold_reason": "internal_failure"}
        assert rows[receipt] == {"next": 1, "hold_reason": "payload_invalid"}
        assert rows[stuck] == {"hold_reason": "dispatch_failed"}
        health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
        assert health["notify"]["held_reasons"] == {
            "internal_failure": 1, "payload_invalid": 1, "dispatch_failed": 1}
    finally:
        db.close()
