"""A role-extraction generation change makes old nightly scans unusable and reruns them."""
import json

import pytest

import extract
import extract_llm
import patient_context_refresh as context_refresh
from test_patient_context_refresh import CFG, finish, runtime, seed

__all__ = ["runtime"]


@pytest.mark.parametrize("module,version", [(extract, "RULE_VERSION"), (extract_llm, "EXTRACT_VERSION")])
def test_generation_change_rejects_completed_scan_and_rescans(runtime, monkeypatch, module, version):
    led, clock, calls = runtime
    seed(led, body="見出しのない昔の支援記録を原文のまま残す。")
    finish(runtime)
    before = led.db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=1",
                            (context_refresh.SCAN,)).fetchone()
    old_scan, old_meta = json.loads(before[0]), json.loads(before[1])
    assert old_scan["complete"] and old_scan["audited"]
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
    calls.clear()
    monkeypatch.setattr(module, version, getattr(module, version) + 1)
    current = context_refresh._snapshot(led.db, 1, CFG)
    assert old_meta["contract"] != current["contract"]
    assert old_meta["source_sig"] == context_refresh._source_sig(current["sources"][0])
    assert context_refresh._current_scans(led.db, 1, current["sources"], current["contract"]) == {}
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    pending = context_refresh._pending(led.db, 1, CFG, now=clock.wall)[2]
    assert pending["A"] and pending["B"]
    finish(runtime)
    after = led.db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=1",
                           (context_refresh.SCAN,)).fetchone()
    scan, meta = json.loads(after[0]), json.loads(after[1])
    assert scan["complete"] and scan["audited"]
    assert meta["contract"] == context_refresh._contract(CFG)
    assert sum("source" in call for call in calls) == 1
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
