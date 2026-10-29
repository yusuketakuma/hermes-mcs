"""Synthetic derivation reserves snapshot verification time without hiding unfinished work."""
import drug_map
import extract
import mcs_signals
import rollup
import run_check
import snapshots
from extract_testkit import _ledger


def test_derive_reserves_verification_and_existing_tail_for_every_stage(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    seen = {}
    try:
        def verification_budget(path):
            assert path == ledger.db.execute("PRAGMA database_list").fetchone()[2]
            assert path != run_check.DB
            return 154
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", verification_budget)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: 0)
        def rules(*args, deadline):
            seen["rules"] = deadline
            return {"done": 0, "pids": []}
        def drugs(*args, deadline):
            seen["drugs"] = deadline
            return {"pids": [], "status": "complete"}
        def scan(*args, deadline, commit_deadline, **kwargs):
            seen["scan"] = deadline
            seen["scan_commit"] = commit_deadline
            return rollup.RollupScan((), (1,), True, (("deadline", 1),))
        def rebuild(*args, deadline, **kwargs):
            seen["rebuild"] = deadline
            return 0
        def signals(*args, deadline):
            seen["signals"] = deadline
            return {"errors": []}
        monkeypatch.setattr(extract, "run_pending", rules)
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", drugs)
        monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
        monkeypatch.setattr(rollup, "rebuild_many", rebuild)
        monkeypatch.setattr(mcs_signals, "evaluate", signals)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 480, {}, llm_budget_cap=0)
        assert seen == {"rules": 296, "drugs": 296, "scan": 148,
                        "scan_commit": 296, "rebuild": 296, "signals": 296}
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert "rollup: scan_unknown" in result["errors"]
        assert run_check.RUN_DEADLINE_S == 480
    finally:
        ledger.close()


def test_derive_reserves_rebuild_time_and_excludes_unknown_touched(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    clock = [20]
    seen = {}
    try:
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": [1, 2, 3]})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})

        def scan(_ledger, *, deadline, commit_deadline, **kwargs):
            seen["scan_deadline"] = deadline
            seen["commit_deadline"] = commit_deadline
            clock[0] = deadline
            return rollup.RollupScan((1,), (2,), True, (("deadline", 1),))

        def rebuild(_ledger, project_ids, *, deadline, **kwargs):
            seen["rebuilt"] = set(project_ids)
            seen["rebuild_start"] = clock[0]
            seen["rebuild_deadline"] = deadline
            return len(project_ids)

        monkeypatch.setattr(rollup, "scan_fair_dirty_projects", scan)
        monkeypatch.setattr(rollup, "rebuild_many", rebuild)
        result = {"errors": [], "trickle_imports": [{"pid": 2}]}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)

        # 20 seconds for snapshot verification and 30 for the existing tail.
        assert seen["commit_deadline"] == seen["rebuild_deadline"] == 150
        assert seen["scan_deadline"] == seen["rebuild_start"] == 85
        assert seen["rebuild_start"] < seen["rebuild_deadline"]
        assert seen["rebuilt"] == {1, 3}
        assert result["rollups"] == 2
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert "rollup: scan_unknown" in result["errors"]
    finally:
        ledger.close()


def test_real_scan_timeout_preserves_time_to_rebuild_confirmed_dirty(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    clock = [20]
    scans, rebuilt = [], []
    try:
        ledger.ensure_patient(1)
        ledger.ensure_patient(2)
        monkeypatch.setattr(snapshots, "snapshot_verify_timeout_s", lambda path: 20)
        monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(extract, "run_pending", lambda *a, **k: {"done": 0, "pids": [2]})
        monkeypatch.setattr(drug_map, "configured", lambda cfg: (None, None))
        monkeypatch.setattr(drug_map, "derive", lambda *a, **k: {"pids": [], "status": "complete"})
        monkeypatch.setattr(mcs_signals, "evaluate", lambda *a, **k: {"errors": []})

        def dirty(_db, project_id, _now, *, deadline):
            scans.append(project_id)
            if project_id == 2:
                clock[0] = deadline
            return True

        def rebuild(current_ledger, project_id, *, cfg, deadline):
            rebuilt.append((project_id, clock[0], deadline))
            current_ledger.artifact_add(rollup.KIND, "synthetic", project_id=project_id)

        monkeypatch.setattr(rollup, "_dirty_project_snapshot", dirty)
        monkeypatch.setattr(rollup, "rebuild", rebuild)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 200, {}, llm_budget_cap=0)

        assert scans == [1, 2]
        assert rebuilt == [(1, 85, 150)]
        assert [row["project_id"] for row in ledger.artifacts(rollup.KIND)] == [1]
        assert result["rollups"] == 1
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert "rollup: scan_unknown" in result["errors"]
        assert not ledger.db.in_transaction
    finally:
        ledger.close()
