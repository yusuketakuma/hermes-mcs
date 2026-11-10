"""Health and housekeeping for long-running installs: a semantic backlog
whose drainer only defers, a dead local LLM, cached coverage counts,
backup deadline skips and the change-gated snapshot. Temp ledger,
temp health file and stubs only."""
import llm_availability
import health_report
import json
import time

import pytest

import maintenance
import run_check
from inference_version import CURRENT_VERSION, VERSION_KEY
from ingest_testkit import _ledger, _message

ON = {"semantic": {"mode": "shadow", "project_ids": []}}
H = 3600


@pytest.fixture
def health_file(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(path))

    def write(**block):
        path.write_text(json.dumps(block))
    return write


def _sem_job(db, mid, *, next_try, updated_at, state="pending", kind="semantic"):
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,"
        "next_try,created_at,updated_at) VALUES(?,1,?,'{}',?,?,?,?)",
        (kind, mid, state, next_try, updated_at - 60, updated_at))
    db.db.commit()


def test_deferring_drainer_no_longer_hides_a_semantic_stall(tmp_path, health_file):
    db = _ledger(tmp_path)
    now = time.time()
    # the drainer keeps deferring: updated_at fresh, next_try a minute out
    _sem_job(db, 1, next_try=now + 60, updated_at=now - 30)
    marks = {}
    assert health_report.backlog_stalls(db.db, ON, now, marks=marks) == []
    assert marks["semantic_due_since"] == now          # episode starts now
    health_file(stall_marks={"semantic_due_since": now - 7 * H})
    assert health_report.backlog_stalls(db.db, ON, now) == ["semantic_backlog_stalled"]
    # a job finished inside the window: the backlog moves
    _sem_job(db, 2, next_try=now, updated_at=now - H, state="done")
    assert health_report.backlog_stalls(db.db, ON, now) == []
    db.close()


def test_due_episode_ends_when_no_job_is_due(tmp_path, health_file):
    db = _ledger(tmp_path)
    now = time.time()
    health_file(stall_marks={"semantic_due_since": now - 7 * H})
    _sem_job(db, 1, next_try=now + 6 * H, updated_at=now)   # scheduled later
    marks = {}
    assert health_report.backlog_stalls(db.db, ON, now, marks=marks) == []
    assert marks["semantic_due_since"] is None
    db.close()


def _legacy_extract(db, mid, *, exhausted):
    import extract_llm
    digest = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?",
                           (mid,)).fetchone()[0]
    meta = {VERSION_KEY: CURRENT_VERSION, "hash": digest, "extract_version": extract_llm.EXTRACT_VERSION}
    content = {"summary": "s", "symptoms": [{"text": "a"}, {"text": "b"}]}
    if exhausted:
        meta.update(error=True, attempts=5, next_try=0)
        content = {"_error": True}
    db.artifact_add(extract_llm.KIND, json.dumps(content), project_id=1,
                    message_id=mid, meta=meta)


@pytest.mark.parametrize(("down_for", "due", "alerted"), [
    (3600, "semantic", True), (3599, "semantic", False),
    (None, "semantic", False), (7200, None, False),
    # the default semantic-off install: pending legacy extraction is
    # eligible messages, not fetch_jobs
    (7200, "legacy", True),
    # extract_qc runs on Jev, not the local model
    (7200, "extract_qc", False),
    # legacy extraction already settled, or out of attempts: nothing runnable
    (7200, "legacy_done", False), (7200, "legacy_exhausted", False),
])
def test_local_llm_down_uses_the_llm_health_record(
        tmp_path, health_file, monkeypatch, down_for, due, alerted):
    db = _ledger(tmp_path)
    now = time.time()
    monkeypatch.setattr(llm_availability, 'llm_down_for_s',
                        lambda _ledger, now=None: down_for, raising=False)
    cfg = {}
    if due == "semantic":
        cfg = ON
        _sem_job(db, 1, next_try=now, updated_at=now)
    elif due == "extract_qc":
        _sem_job(db, 1, next_try=now, updated_at=now, kind="extract_qc")
    elif due and due.startswith("legacy"):
        db.save_messages([_message(1, body="synthetic note")], project_id=1)
        if due != "legacy":
            _legacy_extract(db, 1, exhausted=due == "legacy_exhausted")
    reasons = health_report.backlog_stalls(db.db, cfg, now)
    assert ("local_llm_down" in reasons) is alerted
    assert "llm_endpoint_down" not in reasons   # the retired fallback reason is never emitted
    db.close()


def test_health_carries_stall_marks_and_caches_coverage(tmp_path, health_file, monkeypatch):
    import extract_llm
    db = _ledger(tmp_path)
    now = time.time()
    source = health_report.ledger_key(db.db)
    health_file(stall_marks={"semantic_due_since": now - H},
                extract_v2_coverage={
                    "extract_version": extract_llm.EXTRACT_VERSION,
                    "current": 5, "eligible": 9, "poison_gated": 0,
                    "ratio": 0.5556, "counted_at": now - 60, "source": source})
    _sem_job(db, 1, next_try=now, updated_at=now)
    health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok", cfg=ON)
    assert health["stall_marks"]["semantic_due_since"] == pytest.approx(now - H)
    assert health["extract_v2_coverage"]["eligible"] == 9      # cached
    # another ledger, an old count or a new extract version is recounted
    monkeypatch.setattr(health_report, 'COVERAGE_COUNT_S', 30)
    health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok", cfg=ON)
    assert health["extract_v2_coverage"]["eligible"] == 0
    assert health["extract_v2_coverage"]["source"] == source
    db.close()


def test_housekeeping_passes_the_watchdog_deadline_and_records_a_skip(
        tmp_path, health_file, monkeypatch):
    seen = {}

    def backup(db_path, **kw):
        seen.update(kw)
        return "skipped_deadline"
    monkeypatch.setattr(maintenance, "daily_backup", backup)
    monkeypatch.setattr(maintenance, "db_maintenance", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "rotate_log", lambda: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    monkeypatch.setattr(maintenance, "prune_leftovers", lambda: 0)
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", time.monotonic() + 100)
    result = {"errors": []}
    run_check._housekeeping(result)
    assert result == {"errors": [], "backup_skipped": "deadline"}
    assert seen["deadline"] == pytest.approx(
        run_check._WATCHDOG_EXIT_AT - run_check.HARD_DEADLINE_MARGIN_S)
    db = _ledger(tmp_path)
    monkeypatch.setattr(maintenance, "newest_backup_age_s", lambda: None)
    health = health_report.build_health(db.db, dict(result, notify={}), "ok", cfg={})
    assert "backup_deferred_deadline" in health["state_reasons"]
    assert health["disk_low"] is False and health["overall"] == "degraded"
    # a same-day backup exists: one late tick is not an incident
    monkeypatch.setattr(maintenance, "newest_backup_age_s", lambda: 3 * H)
    health = health_report.build_health(db.db, dict(result, notify={}), "ok", cfg={})
    assert "backup_deferred_deadline" not in health["state_reasons"]
    db.close()


def test_disarm_clears_the_hard_deadline(monkeypatch):
    run_check._arm_watchdog(time.monotonic() + 600, 60)
    try:
        assert run_check._hard_deadline() is not None
    finally:
        run_check._disarm_watchdog()
    assert run_check._hard_deadline() is None


@pytest.mark.parametrize(("dirty", "state", "cleared", "errors"), [
    (False, "unchanged", False, []),
    (True, "published", True, []),
    (False, None, False, ["snapshot: RuntimeError"]),
])
@pytest.mark.parametrize("hard", [None, 530.0])
def test_finish_run_publishes_by_change_and_clears_dirty_only_on_publish(
        tmp_path, monkeypatch, dirty, state, cleared, errors, hard):
    import notify_cards
    import notify_spool
    import notify_cmds
    db = _ledger(tmp_path)
    run_id = db.begin_run(None)
    calls = {}
    monkeypatch.setattr(run_check, "_hard_deadline", lambda: hard)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **k: None)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda _l: dirty)
    monkeypatch.setattr(notify_cards, "clear_snapshot_dirty",
                        lambda _l, _g: calls.setdefault("cleared", True))

    def refresh(_path, *, force, deadline=None, verification_context=None):
        calls["force"] = force
        calls["deadline"] = deadline
        return state
    monkeypatch.setattr(maintenance, "refresh_snapshot", refresh)
    result = {"errors": [], "incomplete": [], "notify": {}}
    status = run_check._finish_run(db, {}, result, run_id, time.monotonic() + 60)
    assert calls["force"] is dirty
    assert calls["deadline"] == (None if hard is None else 495.0)
    assert calls.get("cleared", False) is cleared
    assert result["errors"] == errors
    assert status == ("partial" if errors else "ok")
    db.close()


@pytest.mark.parametrize("failure", ["signature_deadline", "copy_timeout", "validator_timeout"])
def test_snapshot_failure_leaves_health_time_and_preserves_prior_publication(
        tmp_path, monkeypatch, failure):
    import notify_cards
    import notify_cmds
    import notify_spool
    import snapshots
    from pathlib import Path

    db = _ledger(tmp_path)
    try:
        source = db.db.execute("PRAGMA database_list").fetchone()[2]
        destination = tmp_path / "snapshots"
        published = snapshots.publish_snapshot(source, str(destination))
        assert published is not None
        old_snapshot = Path(published).read_bytes()
        monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(destination))
        marker = Path(maintenance._snapshot_sig_path())
        marker.write_text(json.dumps({"sig": "synthetic-old", "published_at": 100}))
        old_marker = marker.read_bytes()
        health_path = tmp_path / "health.json"
        health_path.write_text(json.dumps({"at": 100, "unread_at": 100,
                                          "semantic_jobs": {"pending": 999}}))
        monkeypatch.setattr(health_report, "HEALTH_FILE", str(health_path))
        monkeypatch.setattr(run_check, "DB", source)
        monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", 540.0)
        monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
        monkeypatch.setattr(notify_spool, "publish_flags", lambda *a, **k: None)
        monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda _: True)
        cleared = []
        monkeypatch.setattr(notify_cards, "clear_snapshot_dirty",
                            lambda *a: cleared.append(True))
        clock = [500.0 if failure == "signature_deadline" else 302.0]
        monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(maintenance, "monotonic", lambda: clock[0])
        monkeypatch.setattr(snapshots, "monotonic", lambda: clock[0])

        def prepare(_source, _tmp, deadline, _fd, _state):
            assert failure != "signature_deadline", "expired snapshot started a copy"
            assert deadline == 495.0
            if failure == "copy_timeout":
                clock[0] = deadline
                snapshots._fail("copy_timeout")
                return False
            clock[0] += 176.031
            return True

        def verify(_path, *, deadline, **kwargs):
            assert failure == "validator_timeout"
            assert deadline == 495.0
            remaining = deadline - clock[0]
            clock[0] = deadline
            snapshots._fail("validator_timeout", verify_s=remaining, budget_s=remaining)
            return False

        monkeypatch.setattr(snapshots, "_prepare_in_child", prepare)
        monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", verify)
        run_id = db.begin_run(None)
        result = {"errors": [], "incomplete": [], "notify": {}}
        status = run_check._finish_run(db, {}, result, run_id, 480.0)
        assert status == "partial"
        assert result["snapshot"] is False and result["snapshot_state"] is None
        assert result["snapshot_failure"]["reason"] == failure
        assert Path(published).read_bytes() == old_snapshot
        assert marker.read_bytes() == old_marker and not cleared
        assert run_check._hard_deadline() - clock[0] >= 30.0
        run_check._write_health(db, result, status, run_id=run_id, cfg={})
        health = json.loads(health_path.read_text())
        assert health["at"] > 100 and health["unread_at"] == health["at"]
        assert health["semantic_jobs"]["pending"] == 0
        assert health["notify"]["incomplete_parts"]["total"] == 0
        assert health["run_status"] == "partial"
        assert run_check._hard_deadline() == 530.0
    finally:
        db.close()
