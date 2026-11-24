"""Derive reports only numeric rollup timings without changing its stage accounting."""
from contextlib import closing

import pytest

import drug_map
import extract
import mcs_signals
import rollup
import run_check
import snapshots
from extract_testkit import _ledger


@pytest.fixture
def timing_case(tmp_path, monkeypatch):
    clock = [20.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
    monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": []})
    monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
    monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
    monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})
    with closing(_ledger(tmp_path)) as ledger:
        yield ledger, clock


def test_derive_rollup_timing_has_remaining_budget_and_no_stage_double_count(timing_case, monkeypatch):
    ledger, clock = timing_case
    deadlines = []

    def scan(*args, deadline, commit_deadline, **kwargs):
        deadlines.extend([deadline, commit_deadline])
        clock[0] += 2.12345
        return rollup.RollupScan((1, 2), (3,), True, (("deadline", 1),))

    def rebuild(ledger, pid, **kwargs):
        clock[0] += pid + 0.12345
        if pid == 2:
            raise rollup.RollupSourceUnverified()

    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
    monkeypatch.setattr(rollup, "rebuild", rebuild)
    result = {"errors": []}
    run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)
    assert deadlines == pytest.approx([190 / 3, 320 / 3])
    assert result["rollup_timing"] == {
        "scan_s": 2.123, "rebuild_s": 3.247, "rebuild_budget_s": 84.543,
        "rebuilt": 1, "deferred": 1, "max_patient_s": 2.123,
    }
    assert result["rollup_scan"]["unknown_observed"] == 1
    assert result["rollups"] == 1 and result["rollup_source_unknown"] == 1
    assert result["errors"] == ["deadline_deferred:rollup_scan", "rollup: source_unknown"]
    assert set(result["stage_elapsed_s"]) == {
        "derive_rules", "derive_llm", "derive_medications", "derive_signals"}
    assert result["run"]["slowest_stage"] == "derive_rules"


@pytest.mark.parametrize("fail_scan", [False, True])
def test_derive_failure_retains_available_timing_and_existing_error(timing_case, monkeypatch, fail_scan):
    ledger, clock = timing_case

    def scan(*args, **kwargs):
        clock[0] += 3
        if fail_scan:
            raise ValueError("synthetic-private")
        return rollup.RollupScan((1, 2), (), False)

    def rebuild(*args, **kwargs):
        clock[0] += 4
        raise ValueError("synthetic-private")

    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
    monkeypatch.setattr(rollup, "rebuild", rebuild)
    result = {"errors": []}
    run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)
    expected = {"scan_s": 3.0} if fail_scan else {
        "scan_s": 3.0, "rebuild_budget_s": 83.667, "rebuild_s": 4.0,
        "rebuilt": 0, "deferred": 2, "max_patient_s": 4.0,
    }
    assert result["rollup_timing"] == expected
    assert result["errors"] == ["rollup: ValueError"]
    assert "rollups" not in result and "synthetic-private" not in str(result)


def test_scan_clock_failure_does_not_replace_original_failure(timing_case, monkeypatch):
    ledger, _clock = timing_case
    previous = run_check.time.monotonic

    def failed_observation():
        monkeypatch.setattr(run_check.time, "monotonic", previous)
        raise OSError("synthetic-private-clock")

    def scan(*args, **kwargs):
        monkeypatch.setattr(run_check.time, "monotonic", failed_observation)
        raise ValueError("synthetic-private-scan")

    monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
    result = {"errors": []}
    run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)
    assert result["errors"] == ["rollup: ValueError"]
    assert "rollup_timing" not in result
    assert "synthetic-private" not in str(result)
