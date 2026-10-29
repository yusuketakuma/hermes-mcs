"""The manual repair entry must retain the installed inference configuration."""
import rollup
from test_rollup_completion_witness import saved

__all__ = ["saved"]


def test_manual_repair_produces_a_current_rollup_with_nondefault_config(saved, monkeypatch, capsys):
    ledger, cfg, _, _ = saved
    ledger.db.execute("DELETE FROM artifacts WHERE kind=?", (rollup.KIND,))
    ledger.db.commit()
    monkeypatch.setattr(rollup.sys, "argv", ["rollup", "--project", "1"])
    monkeypatch.setattr(rollup, "acquire_run_lock", lambda: 987654)
    monkeypatch.setattr(rollup.os, "close", lambda fd: None)
    monkeypatch.setattr(rollup, "Ledger", lambda path: ledger)
    monkeypatch.setattr(ledger, "close", lambda: None)
    monkeypatch.setattr(rollup, "load_config", lambda: cfg)
    assert rollup.main() == 0
    assert '"rollups": 1' in capsys.readouterr().out
    assert rollup.completion_coverage(ledger.db, 1, cfg)["current"]
