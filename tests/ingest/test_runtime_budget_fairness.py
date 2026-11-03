"""Bounded stages leave time for clinical work and fresh operational evidence."""
import json

import pytest

import drug_map
import extract
import extract_llm
import health_report
import health_watch
import mcs_signals
import notify_recovery
import notify_scope
import rollup
import run_check
import snapshots
from ingest_testkit import _ledger


def test_slow_rules_and_llm_leave_medication_rollup_and_signal_slices(tmp_path, monkeypatch):
    clock = [0.0]
    seen = []
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda _: 0)
    monkeypatch.setattr(drug_map, "configured", lambda _: (None, None))
    monkeypatch.setattr(extract_llm, "_MIN_CALL_S", 1)
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", lambda _: False)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)

    def rules(ledger, *, deadline):
        assert clock[0] < deadline < 270
        seen.append("rules")
        clock[0] = deadline
        return {"done": 1, "pids": []}

    def medications(ledger, dictionary, *, deadline):
        assert clock[0] < deadline < 270
        seen.append("medications")
        clock[0] = deadline
        return {"done": 0, "pids": [], "status": "partial"}

    def llm(ledger, **kwargs):
        assert seen == ["rules"]
        seen.append("llm")
        clock[0] += kwargs["budget_s"]
        assert clock[0] < 270
        return {"done": 1, "pids": []}

    def scan(*a, **kwargs):
        assert clock[0] < kwargs["deadline"] < kwargs["commit_deadline"] < 270
        return rollup.RollupScan((), (), False)

    def rebuild(*a, **kwargs):
        seen.append("rollup")
        clock[0] = kwargs["deadline"]
        return 0

    def signals(*a, **kwargs):
        assert clock[0] < kwargs["deadline"] == 270
        seen.append("signals")
        return {}

    monkeypatch.setattr(extract, "run_pending", rules)
    monkeypatch.setattr(drug_map, "derive", medications)
    monkeypatch.setattr(extract_llm, "run_pending", llm)
    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
    monkeypatch.setattr(rollup, "rebuild_many", rebuild)
    monkeypatch.setattr(mcs_signals, "evaluate", signals)
    ledger = _ledger(tmp_path)
    try:
        result = {"errors": [], "realtime_ids": [1]}
        run_check.stage_derive(ledger, result, 300, {})
        assert seen == ["rules", "llm", "medications", "rollup", "signals"]
        assert result["errors"] == ["drug_map: partial"]
        assert set(result["stage_elapsed_s"]) >= {"derive_rules", "derive_medications"}
    finally:
        ledger.close()


def test_notification_observations_precede_slow_global_health_reads(tmp_path, monkeypatch):
    clock = [0.0]
    seen = []
    monkeypatch.setattr(health_report.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(tmp_path / "missing.json"))

    def parts(db, now, cfg):
        seen.append("parts")
        return {"total": 0, "cards": 0, "by_state": {}, "by_plan": {},
                "oldest_age_s": None, "observed_at": now}

    def holds(db):
        seen.append("holds")
        return {"parts": {}}

    def slow_outbox(*a, **kw):
        assert seen == ["parts", "holds"]
        clock[0] = 31
        raise RuntimeError("observation_limit")

    monkeypatch.setattr(notify_scope, "incomplete_parts_health", parts)
    monkeypatch.setattr(notify_recovery, "part_hold_report", holds)
    monkeypatch.setattr(notify_scope, "pending_breakdown", slow_outbox)
    ledger = _ledger(tmp_path)
    try:
        report = health_report.build_health(ledger.db, {"errors": []}, "ok", cfg={})
        assert report["notify"]["incomplete_parts"]["status"] == "recorded"
        assert report["notify"]["part_holds"]["status"] == "recorded"
        assert report["notify"]["pending_by_reason"]["status"] == "unavailable"
    finally:
        ledger.close()


@pytest.mark.parametrize("evidence", ["stale", "missing"])
def test_fresh_disk_pressure_alerts_even_when_health_producer_is_stale(tmp_path, monkeypatch, evidence):
    data = tmp_path / "data"
    data.mkdir()
    if evidence == "stale":
        (data / "health.json").write_text(json.dumps({"overall": "ok", "at": 1,
                                                   "disk_low": False, "disk_free_mb": 9000}))
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda _: 100)
    monkeypatch.setattr(health_report, "attachment_disk_low", lambda free: free < 200)
    report = health_watch.evaluate(str(tmp_path), now=10000, cfg={})
    assert report["status"] == evidence
    assert report["disk_low"] and report["disk_alert"]
    assert report["disk_free_mb"] == 100
    assert report["disk_observed_at"] == 10000
    assert not health_watch.evaluate(str(tmp_path), now=10001, cfg={})["disk_alert"]


@pytest.mark.parametrize("remaining,expected", [(80, None), (90, 75), (149, 75), (180, 90)])
def test_complete_realtime_call_keeps_clinical_tail(tmp_path, monkeypatch, remaining, expected):
    clock = [0.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda _: 0)
    monkeypatch.setattr(extract, "run_pending", lambda *a, **kw: {"done": 0, "pids": []})
    monkeypatch.setattr(drug_map, "configured", lambda _: (None, None))
    monkeypatch.setattr(drug_map, "derive", lambda *a, **kw: {"done": 0, "pids": [], "status": "unavailable"})
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", lambda _: False)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", lambda *a, **kw: rollup.RollupScan((), (), False))
    monkeypatch.setattr(rollup, "rebuild_many", lambda *a, **kw: 0)
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **kw: {})
    budgets = []
    def infer(*a, **kw):
        budgets.append(kw["budget_s"])
        assert kw["budget_s"] < remaining
        return {"done": 0, "pids": []}
    monkeypatch.setattr(extract_llm, "run_pending", infer)
    ledger = _ledger(tmp_path)
    try:
        run_check.stage_derive(ledger, {"errors": [], "realtime_ids": [1]}, remaining + 30, {})
        assert budgets == ([] if expected is None else [expected])
    finally:
        ledger.close()


def test_live_disk_recovery_rearms_warning_despite_old_low_health(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps({"overall": "ok", "at": 1,
                                               "disk_low": True, "disk_free_mb": 100}))
    free = [100]
    monkeypatch.setattr(health_report, "attachment_free_mb", lambda _: free[0])
    monkeypatch.setattr(health_report, "attachment_disk_low", lambda value: value < 200)
    assert health_watch.evaluate(str(tmp_path), now=10000, cfg={})["disk_alert"]
    free[0] = 1000
    recovered = health_watch.evaluate(str(tmp_path), now=10001, cfg={})
    assert not recovered["disk_low"] and not recovered["disk_alert"]
    assert recovered["status"] == "stale"
    free[0] = 100
    assert health_watch.evaluate(str(tmp_path), now=10002, cfg={})["disk_alert"]
