"""Failures that used to leave health 'ok': an invalid semantic block,
signal detector errors, a lost failure alert, and card / semantic /
extraction work that stopped moving. Temp ledger and stubs only."""
import time
from types import SimpleNamespace

import pytest

import mcs_signals
import run_check
from ingest_testkit import _ledger

OLD = time.time() - 7 * 3600


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
def test_card_delivery_stall(tmp_path, cfg, transport, age, stalled):
    db = _ledger(tmp_path)
    _render(db, transport, time.time() - age)
    _render(db, transport, OLD - 86400, state="delivered")
    reasons = run_check._backlog_stalls(db, cfg, time.time())
    assert ("card_delivery_stalled" in reasons) is stalled


def test_semantic_stall_needs_old_due_job_and_no_progress(tmp_path):
    db = _ledger(tmp_path)
    on = {"semantic": {"mode": "shadow", "project_ids": []}}
    _sem_job(db, 1, "pending", OLD)
    assert run_check._backlog_stalls(db, {}, time.time()) == []   # off
    assert run_check._backlog_stalls(db, on, time.time()) == [
        "semantic_backlog_stalled"]
    _sem_job(db, 2, "done", time.time() - 60)                     # moving
    assert run_check._backlog_stalls(db, on, time.time()) == []


def test_extract_stall_needs_unstarted_old_message_and_no_progress(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 1, time.time())
    assert run_check._backlog_stalls(db, {}, time.time()) == []   # fresh
    _store(db, 2, OLD)
    assert run_check._backlog_stalls(db, {}, time.time()) == [
        "extract_backlog_stalled"]
    canonical = {"semantic": {"fact_source": "canonical"}}
    # an errored config is reported as a stage error, not a stall
    assert run_check._backlog_stalls(db, canonical, time.time()) == []
    _artifact(db, 1, time.time() - 60)                            # moving
    assert run_check._backlog_stalls(db, {}, time.time()) == []


def test_stall_degrades_overall_health(tmp_path):
    db = _ledger(tmp_path)
    _render(db, "slack", time.time() - 3600)
    health = run_check._health(db, {"errors": [], "notify": {}}, "ok",
                               cfg=SLACK)
    assert health["overall"] == "degraded"
    assert "card_delivery_stalled" in health["state_reasons"]


def test_invalid_semantic_block_is_reported(tmp_path):
    db = _ledger(tmp_path)
    result = {"errors": ["config: semantic_unknown_field"]}
    assert run_check._semantic_enabled(db, {"semantic": {"x": 1}},
                                       result) is False
    assert result["errors"] == ["config: semantic_unknown_field"]
    result = {"errors": []}
    run_check._semantic_enabled(db, {"semantic": "typo"}, result)
    assert result["errors"] == ["config: semantic_not_object"]


def test_signal_detector_errors_reach_run_errors(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {
        "open": 0, "errors": ["handoff:ValueError"]})
    result = {"errors": []}
    run_check.stage_derive(db, result, time.monotonic() + 600, {})
    assert "signals: handoff:ValueError" in result["errors"]


def test_failed_alert_enqueue_is_visible():
    def boom(*a, **k):
        raise OSError("disk")
    ledger = SimpleNamespace(finish_run=lambda *a: None, outbox_add=boom)
    result = {"errors": []}
    run_check._fail_run(ledger, SimpleNamespace(no_notify=True), result, 1,
                        "failed", "crash:X", time.monotonic() + 10, "run")
    assert result["errors"] == ["crash:X", "alert_enqueue_failed: OSError"]
