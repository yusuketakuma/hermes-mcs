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
        def scan(*args, deadline, **kwargs):
            seen["scan"] = deadline
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
        monkeypatch.setattr(rollup, "scan_dirty_projects", scan)
        monkeypatch.setattr(rollup, "rebuild_many", rebuild)
        monkeypatch.setattr(mcs_signals, "evaluate", signals)
        result = {"errors": []}
        run_check.stage_derive(ledger, result, 480, {}, llm_budget_cap=0)
        assert seen == dict.fromkeys(("rules", "drugs", "scan", "rebuild", "signals"), 296)
        assert result["rollup_scan"]["state"] == "unknown"
        assert result["rollup_scan"]["unknown_reasons"] == {"deadline": 1}
        assert "rollup: scan_unknown" in result["errors"]
        assert run_check.RUN_DEADLINE_S == 480
    finally:
        ledger.close()
