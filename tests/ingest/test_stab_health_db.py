"""Health and housekeeping for long-running installs: a semantic backlog
whose drainer only defers, a dead local LLM, cached coverage counts,
backup deadline skips and the change-gated snapshot. Temp ledger,
temp health file and stubs only."""
import json
import time

import pytest

import maintenance
import run_check
from ingest_testkit import _ledger, _message

ON = {"semantic": {"mode": "shadow", "project_ids": []}}
H = 3600


@pytest.fixture
def health_file(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr(run_check, "HEALTH_FILE", str(path))

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
    assert run_check._backlog_stalls(db, ON, now, marks=marks) == []
    assert marks["semantic_due_since"] == now          # episode starts now
    health_file(stall_marks={"semantic_due_since": now - 7 * H})
    assert run_check._backlog_stalls(db, ON, now) == ["semantic_backlog_stalled"]
    # a job finished inside the window: the backlog moves
    _sem_job(db, 2, next_try=now, updated_at=now - H, state="done")
    assert run_check._backlog_stalls(db, ON, now) == []
    db.close()


def test_due_episode_ends_when_no_job_is_due(tmp_path, health_file):
    db = _ledger(tmp_path)
    now = time.time()
    health_file(stall_marks={"semantic_due_since": now - 7 * H})
    _sem_job(db, 1, next_try=now + 6 * H, updated_at=now)   # scheduled later
    marks = {}
    assert run_check._backlog_stalls(db, ON, now, marks=marks) == []
    assert marks["semantic_due_since"] is None
    db.close()


def test_llm_endpoint_down_needs_failing_breaker_and_due_work(
        tmp_path, health_file, monkeypatch):
    import mcs_util
    # the breaker fallback applies only without the LLM health record
    monkeypatch.delattr(mcs_util, "llm_down_for_s", raising=False)
    db = _ledger(tmp_path)
    now = time.time()
    circuit = tmp_path / "llm_circuit.json"
    _sem_job(db, 1, next_try=now, updated_at=now)
    health_file(stall_marks={"llm_down_since": now - 7 * H})
    marks = {}
    assert "llm_endpoint_down" not in run_check._backlog_stalls(db, ON, now, marks=marks)
    assert marks["llm_down_since"] is None              # breaker healthy
    circuit.write_text(json.dumps({"failures": 1, "streak": 0}))
    assert "llm_endpoint_down" in run_check._backlog_stalls(db, ON, now)
    health_file(stall_marks={"llm_down_since": now - H})
    assert "llm_endpoint_down" not in run_check._backlog_stalls(db, ON, now)
    db.close()


@pytest.mark.parametrize(("down_for", "due", "alerted"), [
    (3600, "semantic", True), (3599, "semantic", False),
    (None, "semantic", False), (7200, None, False),
    # the default semantic-off install: pending legacy extraction is
    # eligible messages, not fetch_jobs
    (7200, "legacy", True),
    # extract_qc runs on Jev, not the local model
    (7200, "extract_qc", False),
])
def test_local_llm_down_uses_the_llm_health_record(
        tmp_path, health_file, monkeypatch, down_for, due, alerted):
    import mcs_util
    db = _ledger(tmp_path)
    now = time.time()
    monkeypatch.setattr(mcs_util, "llm_down_for_s",
                        lambda _ledger, now=None: down_for, raising=False)
    cfg = {}
    if due == "semantic":
        cfg = ON
        _sem_job(db, 1, next_try=now, updated_at=now)
    elif due == "extract_qc":
        _sem_job(db, 1, next_try=now, updated_at=now, kind="extract_qc")
    elif due == "legacy":
        db.save_messages([_message(1, body="synthetic note")], project_id=1)
    reasons = run_check._backlog_stalls(db, cfg, now)
    assert ("local_llm_down" in reasons) is alerted
    assert "llm_endpoint_down" not in reasons   # fallback only without the record
    db.close()


def test_health_carries_stall_marks_and_caches_coverage(tmp_path, health_file, monkeypatch):
    import extract_llm
    db = _ledger(tmp_path)
    now = time.time()
    source = run_check._ledger_key(db)
    health_file(stall_marks={"semantic_due_since": now - H},
                extract_v2_coverage={
                    "extract_version": extract_llm.EXTRACT_VERSION,
                    "current": 5, "eligible": 9, "poison_gated": 0,
                    "ratio": 0.5556, "counted_at": now - 60, "source": source})
    _sem_job(db, 1, next_try=now, updated_at=now)
    health = run_check._health(db, {"errors": [], "notify": {}}, "ok", cfg=ON)
    assert health["stall_marks"]["semantic_due_since"] == pytest.approx(now - H)
    assert health["extract_v2_coverage"]["eligible"] == 9      # cached
    # another ledger, an old count or a new extract version is recounted
    monkeypatch.setattr(run_check, "COVERAGE_COUNT_S", 30)
    health = run_check._health(db, {"errors": [], "notify": {}}, "ok", cfg=ON)
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
    health = run_check._health(db, dict(result, notify={}), "ok", cfg={})
    assert "backup_deferred_deadline" in health["state_reasons"]
    assert health["disk_low"] is False and health["overall"] == "degraded"
    # a same-day backup exists: one late tick is not an incident
    monkeypatch.setattr(maintenance, "newest_backup_age_s", lambda: 3 * H)
    health = run_check._health(db, dict(result, notify={}), "ok", cfg={})
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
def test_finish_run_publishes_by_change_and_clears_dirty_only_on_publish(
        tmp_path, monkeypatch, dirty, state, cleared, errors):
    import notify_cards
    import notify_cmds
    db = _ledger(tmp_path)
    run_id = db.begin_run(None)
    calls = {}
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    monkeypatch.setattr(notify_cards, "publish_flags", lambda *a, **k: None)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda _l: dirty)
    monkeypatch.setattr(notify_cards, "clear_snapshot_dirty",
                        lambda _l: calls.setdefault("cleared", True))

    def refresh(_path, *, force):
        calls["force"] = force
        return state
    monkeypatch.setattr(maintenance, "refresh_snapshot", refresh)
    result = {"errors": [], "incomplete": [], "notify": {}}
    status = run_check._finish_run(db, {}, result, run_id, time.monotonic() + 60)
    assert calls["force"] is dirty
    assert calls.get("cleared", False) is cleared
    assert result["errors"] == errors
    assert status == ("partial" if errors else "ok")
    db.close()
