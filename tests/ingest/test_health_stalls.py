"""Failures that used to leave health 'ok': an invalid semantic block,
signal detector errors, a lost failure alert, and card / semantic /
extraction work that stopped moving. Temp ledger and stubs only."""
import health_report
import sqlite3
import time
from types import SimpleNamespace

import pytest

import mcs_signals
import run_check
from common_testkit import ledger_fixture

OLD = time.time() - 7 * 3600



db = ledger_fixture(name="db")


def _store(db, mid, first_seen):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,posted_at,posted_at_ts,"
        "body_text,body_state,content_hash,first_seen) VALUES(?,?,?,?,?,?,?,?)",
        (mid, 1, "2026-10-06T09:00", 1, "本文", "full", f"{mid:064x}",
         first_seen))
    db.db.commit()


def _artifact(db, mid, at):
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,"
        "created_at) VALUES('extract_llm',1,?,'{}','{}',?)", (mid, at))
    db.db.commit()


def _render(db, transport, created_at, state="queued"):
    n = db.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0]
    db.db.execute(
        "INSERT INTO notification_renders(delivery_id,op,render_rev,"
        "route_epoch,transport,payload_hash,correlation,state,created_at,"
        "updated_at) VALUES(?,'notice',1,1,?,'h',?,?,?,?)",
        (f"d{n}", transport, f"c{n}", state, created_at, created_at))
    db.db.commit()
    return f"d{n}"


def _part(db, delivery_id, updated_at, state="delivered"):
    db.db.execute(
        "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,"
        "state,updated_at) VALUES(?,?,?,0,?,?)",
        (delivery_id, f"part-{delivery_id}", "card", state, updated_at))
    db.db.commit()


def _traced(db):
    statements = []
    db.db.set_trace_callback(statements.append)
    return statements


def _sem_job(db, mid, state, updated_at):
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,"
        "next_try,created_at,updated_at) VALUES('semantic',1,?,'{}',?,?,?,?)",
        (mid, state, updated_at, updated_at, updated_at))
    db.db.commit()


SLACK = {"notify": {"interactive": "slack"}}


@pytest.mark.parametrize(("cfg", "transport", "age", "stalled"), [
    (SLACK, "slack", 3600, True),
    (SLACK, "slack", 60, False),            # delivered within a tick or two
    (SLACK, "discord", 3600, False),        # retired transport
    ({}, "discord", 3600, False),           # interactive off
])
def test_card_delivery_stall(db, cfg, transport, age, stalled):
    _render(db, transport, time.time() - age)
    _render(db, transport, OLD - 86400, state="delivered")
    reasons = health_report.backlog_stalls(db.db, cfg, time.time())
    assert ("card_delivery_stalled" in reasons) is stalled


def test_semantic_stall_needs_old_due_job_and_no_progress(db):
    on = {"semantic": {"mode": "shadow", "project_ids": []}}
    _sem_job(db, 1, "pending", OLD)
    assert health_report.backlog_stalls(db.db, {}, time.time()) == []   # off
    assert health_report.backlog_stalls(db.db, on, time.time()) == [
        "semantic_backlog_stalled"]
    _sem_job(db, 2, "done", time.time() - 60)                     # moving
    assert health_report.backlog_stalls(db.db, on, time.time()) == []


def test_jev_block_is_named_instead_of_a_generic_stall(db):
    on = {"semantic": {"mode": "shadow", "project_ids": []}}
    _store(db, 1, OLD)
    _artifact(db, 1, time.time())          # extraction itself is moving
    _sem_job(db, 1, "pending", OLD)
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
        "VALUES('semantic_assess',1,1,'{}',?,?)",
        ('{"technical_status":"error","error_kind":"payment_required"}', time.time() - 60))
    db.db.commit()
    # the block is named — and a job nobody touched for 6h still means
    # the drainer itself stopped (a waiting drainer keeps touching it)
    assert health_report.backlog_stalls(db.db, on, time.time()) == [
        "semantic_jev_payment_required", "semantic_backlog_stalled"]
    db.db.execute("UPDATE fetch_jobs SET updated_at=?", (time.time() - 60,))
    db.db.commit()
    assert health_report.backlog_stalls(db.db, on, time.time()) == ["semantic_jev_payment_required"]
    # a later successful assessment clears the block and the stall rule returns
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
        "VALUES('semantic_assess',1,1,'{}','{\"technical_status\":\"ok\"}',?)", (time.time(),))
    db.db.execute("UPDATE fetch_jobs SET updated_at=?", (OLD,))
    db.db.commit()
    assert health_report.backlog_stalls(db.db, on, time.time()) == ["semantic_backlog_stalled"]


def test_stall_check_failure_stays_a_plain_reason_code(db, monkeypatch):
    """health_watch validates every reason as snake_case: a code carrying
    the exception type would void the whole list (and the flap guard)."""
    import health_watch

    def boom(*_args, **_kw):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(health_report, 'backlog_stalls', boom)
    result = {"errors": [], "notify": {}}
    health = health_report.build_health(db.db, result, "ok", cfg=SLACK)
    assert "stall_check_failed" in health["state_reasons"]
    assert all(health_watch._is_code(r) for r in health["state_reasons"])
    # an observation failure is not a run error: nothing may be appended to
    # result["errors"], so it cannot echo as a postprocess stage error
    assert result["errors"] == []
    assert "stage_errors" not in health["state_reasons"]
    assert health["overall"] == "degraded"
    check = health["stall_check"]
    assert check["status"] == "unknown"
    assert check["reason"] == "observation_unavailable"
    assert check["observation"]["stop"] == "read_unavailable"
    assert "sqlite_code" not in check
    assert "stall_check_failed" in health_watch._REASON_JA


def _sql_error(message, code):
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = code
    return exc


def test_stall_check_records_a_successful_observation(db):
    result = {"errors": [], "notify": {}}
    health = health_report.build_health(db.db, result, "ok", cfg=SLACK)
    check = health["stall_check"]
    assert check["status"] == "recorded"
    assert check["observed_at"] == health["at"]
    assert check["observation"]["stop"] == "complete"
    assert "stall_check_failed" not in health["state_reasons"]
    assert result["errors"] == []


def test_stall_check_without_config_reports_unknown_not_recorded(db):
    health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok")
    check = health["stall_check"]
    assert check["status"] == "unknown" and check["reason"] == "config_not_loaded"
    assert "stall_check_failed" not in health["state_reasons"]


def test_stall_check_budget_exhaustion_is_observation_limit(db):
    result = {"errors": [], "notify": {}}
    health = health_report.build_health(
        db.db, result, "ok", cfg=SLACK, deadline=time.monotonic() - 1)
    assert "stall_check_failed" in health["state_reasons"]
    check = health["stall_check"]
    assert check["status"] == "unknown" and check["reason"] == "observation_limit"
    assert check["observation"]["stop"] == "budget_exhausted"
    assert result["errors"] == []
    assert "stage_errors" not in health["state_reasons"]
    assert health["overall"] == "degraded"


@pytest.mark.parametrize(("exc", "reason", "stop", "sqlite_code"), [
    # SQLITE_INTERRUPT surfaces as a bounded observation limit
    (_sql_error("interrupted", sqlite3.SQLITE_INTERRUPT),
     "observation_limit", "read_unavailable", sqlite3.SQLITE_INTERRUPT),
    # BUSY is a storage failure, not a limit; the primary code is masked to & 255
    (_sql_error("database is locked", sqlite3.SQLITE_BUSY),
     "observation_unavailable", "storage_busy", sqlite3.SQLITE_BUSY),
    (_sql_error("database is locked", sqlite3.SQLITE_BUSY | (2 << 8)),
     "observation_unavailable", "storage_busy", sqlite3.SQLITE_BUSY),
    # a genuine SQL failure stays 'unavailable' and keeps its masked code
    (_sql_error("no such table: x", sqlite3.SQLITE_ERROR),
     "observation_unavailable", "read_unavailable", sqlite3.SQLITE_ERROR),
    # a non-integer sqlite_errorcode is never copied into the report
    (_sql_error("synthetic", "not-an-int"),
     "observation_unavailable", "read_unavailable", None),
])
def test_stall_check_failure_records_fixed_diagnosis(
        db, monkeypatch, exc, reason, stop, sqlite_code):
    def boom(*_args, **_kw):
        raise exc
    monkeypatch.setattr(health_report, 'backlog_stalls', boom)
    result = {"errors": [], "notify": {}}
    health = health_report.build_health(db.db, result, "ok", cfg=SLACK)
    assert "stall_check_failed" in health["state_reasons"]
    assert result["errors"] == []
    assert "stage_errors" not in health["state_reasons"]
    assert health["overall"] == "degraded"
    check = health["stall_check"]
    assert check["status"] == "unknown" and check["reason"] == reason
    assert check["observation"]["stop"] == stop
    if sqlite_code is None:
        assert "sqlite_code" not in check
    else:
        assert check["sqlite_code"] == sqlite_code


def test_real_derive_error_still_reports_stage_errors(db):
    """Control: a genuine postprocess failure in result["errors"] must keep
    producing stage_errors — only the stall observation echo was removed."""
    result = {"errors": ["extract_llm: RuntimeError"], "notify": {}}
    health = health_report.build_health(db.db, result, "ok", cfg=SLACK)
    assert "stage_errors" in health["state_reasons"]
    assert health["overall"] == "degraded"


def test_deferred_only_errors_do_not_report_stage_errors(db):
    result = {"errors": ["deadline_deferred: rollup_scan"], "notify": {}}
    health = health_report.build_health(db.db, result, "ok", cfg=SLACK)
    assert "stage_errors" not in health["state_reasons"]


def test_extract_stall_needs_unstarted_old_message_and_no_progress(db):
    _store(db, 1, time.time())
    assert health_report.backlog_stalls(db.db, {}, time.time()) == []   # fresh
    _store(db, 2, OLD)
    assert health_report.backlog_stalls(db.db, {}, time.time()) == [
        "extract_backlog_stalled"]
    canonical = {"semantic": {"fact_source": "canonical"}}
    # an errored config is reported as a stage error, not a stall
    assert health_report.backlog_stalls(db.db, canonical, time.time()) == []
    _artifact(db, 1, time.time() - 60)                            # moving
    assert health_report.backlog_stalls(db.db, {}, time.time()) == []


def test_stall_degrades_overall_health(db):
    _render(db, "slack", time.time() - 3600)
    health = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok",
                               cfg=SLACK)
    assert health["overall"] == "degraded"
    assert "card_delivery_stalled" in health["state_reasons"]


def test_invalid_semantic_block_is_reported(db):
    result = {"errors": ["config: semantic_unknown_field"]}
    assert run_check._semantic_enabled(db, {"semantic": {"x": 1}},
                                       result) is False
    assert result["errors"] == ["config: semantic_unknown_field"]
    result = {"errors": []}
    run_check._semantic_enabled(db, {"semantic": "typo"}, result)
    assert result["errors"] == ["config: semantic_not_object"]


def test_signal_detector_errors_reach_run_errors(db, monkeypatch):
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {
        "open": 0, "errors": ["handoff:ValueError"]})
    result = {"errors": []}
    run_check.stage_derive(db, result, time.monotonic() + 600, {})
    assert "signals: handoff:ValueError" in result["errors"]


def test_signal_deadline_deferral_reaches_health_before_run_deadline(db, monkeypatch):
    import drug_map
    import extract
    import rollup
    import snapshots

    clock = [20]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
    monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": []})
    monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
    monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", lambda *a, **k:
                        rollup.RollupScan((), (), False, ()))
    monkeypatch.setattr(rollup, "rebuild_many", lambda *a, **k: 0)

    def finished(*args):
        clock[0] = 150  # derived-work deadline; the run still has 50 seconds
        return []

    def unrun(*args):
        pytest.fail("deadline-deferred detector was called")

    monkeypatch.setattr(mcs_signals, "DETECTORS", (
        ("request_overdue", finished), ("request_aging", unrun)))
    result = {"errors": []}
    cfg = {"signals": {"notify": False}}
    run_check._run_stage(result, "derive", 200, run_check.stage_derive,
                         db, result, 200, cfg, llm_budget_cap=0)

    assert result["signals"]["errors"] == []
    assert result["signals"]["detectors_deferred"] == ["request_aging"]
    assert result["errors"] == ["deadline_deferred:signals"]
    assert result["deferred_stages"] == ["signals"]
    assert result["run"]["overshoot_s"] == 0
    health = health_report.build_health(db.db, result, "partial", cfg=cfg)
    assert health["overall"] == "ok"
    assert health["run_status"] == "partial"
    assert "stage_errors" not in health["state_reasons"]
    assert health['collection_diagnostics']['errors']['categories']['deferred'] == 1
    assert "run_deadline_exceeded" not in health["state_reasons"]
    assert health["run"]["deferred_stages"] == ["signals"]

    run_check._run_stage(result, "deliver", 150, unrun)
    assert result["errors"] == ["deadline_deferred:signals,deliver"]
    assert result["deferred_stages"] == ["signals", "deliver"]


def test_failed_alert_enqueue_is_visible():
    def boom(*a, **k):
        raise OSError("disk")
    ledger = SimpleNamespace(
        finish_run=lambda *a: None, outbox_add=boom,
        db=SimpleNamespace(in_transaction=False,
            execute=lambda *a: SimpleNamespace(
                fetchone=lambda: {"t": None})))
    result = {"errors": []}
    run_check._fail_run(ledger, SimpleNamespace(no_notify=True), result, 1,
                        "failed", "crash:X", time.monotonic() + 10, "run")
    assert result["errors"] == ["crash:X", "alert_enqueue_failed: OSError"]


def test_large_backlog_with_recent_delivery_is_not_stalled(db):
    now = time.time()
    for _ in range(300):
        _render(db, 'slack', now - 7200)
    _render(db, 'slack', now - 60, state='delivered')
    assert 'card_delivery_stalled' not in health_report.backlog_stalls(db.db, SLACK, now)
    assert 'card_delivery_stalled' in health_report.backlog_stalls(db.db, SLACK, now + 1900)


def test_recent_render_progress_skips_the_parts_join(db):
    """An old queued render with fresh whole-render progress proves the
    transport is moving; the per-part MAX read must not run."""
    now = time.time()
    _render(db, "slack", now - 7200)
    _render(db, "slack", now - 60, state="delivered")
    traced = _traced(db)
    try:
        reasons = health_report.backlog_stalls(db.db, SLACK, now)
    finally:
        db.db.set_trace_callback(None)
    assert "card_delivery_stalled" not in reasons
    assert not any("notification_render_parts" in sql for sql in traced)


def test_fresh_queued_render_needs_no_progress_reads(db):
    """Without an old queued render neither progress query is issued."""
    now = time.time()
    _render(db, "slack", now - 60)
    traced = _traced(db)
    try:
        reasons = health_report.backlog_stalls(db.db, SLACK, now)
    finally:
        db.db.set_trace_callback(None)
    assert "card_delivery_stalled" not in reasons
    reads = [sql for sql in traced if "notification_renders" in sql]
    assert reads and all("MIN(created_at)" in sql for sql in reads)


def test_old_part_delivery_still_rescues_the_stall(db):
    """Stale whole-render progress falls back to the parts join, whose
    recent row still proves the transport is moving."""
    now = time.time()
    _render(db, "slack", now - 7200)
    delivered = _render(db, "slack", now - 7200, state="delivered")
    _part(db, delivered, now - 60)
    reasons = health_report.backlog_stalls(db.db, SLACK, now)
    assert "card_delivery_stalled" not in reasons


def test_other_transport_progress_does_not_rescue(db):
    now = time.time()
    _render(db, "slack", now - 7200)
    _render(db, "discord", now - 60, state="delivered")
    assert "card_delivery_stalled" in health_report.backlog_stalls(db.db, SLACK, now)


def test_future_progress_is_no_rescue(db):
    now = time.time()
    _render(db, "slack", now - 7200)
    _render(db, "slack", now + 7200, state="delivered")
    delivered = _render(db, "slack", now - 7200, state="delivered")
    _part(db, delivered, now + 7200)
    assert "card_delivery_stalled" in health_report.backlog_stalls(db.db, SLACK, now)


def test_restore_hold_check_runs_without_queued_renders(db):
    # Attempt-scoped holds carry no card_id; the check must still run
    # with an empty render queue.
    db.db.execute(
        "INSERT INTO notification_restore_holds(attempt_id,reason,"
        "held_at) VALUES('a0','synthetic',?)", (time.time(),))
    db.db.commit()
    assert "restore_delivery_held" in health_report.backlog_stalls(db.db, SLACK, time.time())


@pytest.mark.parametrize(("needle", "phase"), [
    ("MIN(created_at) FROM notification_renders", "card_queue_oldest"),
    ("MAX(updated_at) FROM notification_renders", "card_render_progress"),
    ("notification_render_parts p", "card_part_progress"),
    ("notification_restore_holds", "card_restore_holds"),
])
def test_stall_observation_phase_marks_the_failed_section(db, needle, phase):
    """A SQL failure leaves a fixed phase name in the stall observation —
    no ids, bodies or exception text — and the read still fails."""
    now = time.time()
    _render(db, "slack", now - 7200)
    inner = db.db

    class FailOn:
        def __getattr__(self, name):
            return getattr(inner, name)

        def execute(self, sql, parameters=()):
            if isinstance(sql, str) and needle in sql:
                raise sqlite3.OperationalError("synthetic stall")
            return inner.execute(sql, parameters)

    proxy = FailOn()
    observed = {}
    with pytest.raises(sqlite3.OperationalError):
        health_report.bounded_health_read(
            proxy, lambda _check: health_report.backlog_stalls(
                proxy, SLACK, now, observation=observed),
            seconds=10.0, observation=observed)
    assert observed["phase"] == phase
    assert "synthetic stall" not in str(observed)


def test_stall_check_carries_the_failed_phase(db):
    """End-to-end: a parts-join failure inside build_health leaves its
    fixed phase in stall_check["observation"]."""
    now = time.time()
    _render(db, "slack", now - 7200)
    inner = db.db

    class FailOn:
        def __getattr__(self, name):
            return getattr(inner, name)

        def execute(self, sql, parameters=()):
            if (isinstance(sql, str)
                    and "JOIN notification_renders r ON r.delivery_id=p.delivery_id" in sql):
                raise sqlite3.OperationalError("synthetic stall")
            return inner.execute(sql, parameters)

    health = health_report.build_health(FailOn(), {"errors": [], "notify": {}}, "ok",
                                        cfg=SLACK)
    check = health["stall_check"]
    assert check["status"] == "unknown" and check["reason"] == "observation_unavailable"
    assert check["observation"]["phase"] == "card_part_progress"
    assert "synthetic stall" not in str(check)


def test_observation_kwarg_records_last_phase_on_success(db):
    observed = {}
    health_report.backlog_stalls(db.db, {}, time.time(), observation=observed)
    assert observed["phase"] == "extract_backlog"
