"""A role-extraction generation change makes old context scans unusable and reruns them."""
import patient_context_plan as context_plan
import json

import pytest

import extract
import extract_llm
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_selection
import extract_llm_store
import patient_context_refresh as context_refresh
from test_patient_context_refresh import CFG, _raw_proof, finish, runtime, seed

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
    old_version = getattr(module, version)
    monkeypatch.setattr(module, version, old_version + 1)
    if module is extract_llm:
        # EXTRACT_VERSION is shared across the split extract_llm modules; a real
        # generation bump moves all of them together.
        for sub in (extract_llm_context, extract_llm_errors, extract_llm_prepare,
                    extract_llm_progress, extract_llm_proofs, extract_llm_provider,
                    extract_llm_qc, extract_llm_resident, extract_llm_selection,
                    extract_llm_store):
            monkeypatch.setattr(sub, "EXTRACT_VERSION", extract_llm.EXTRACT_VERSION)
    current = context_refresh._snapshot(led.db, 1, CFG)
    assert old_meta["contract"] != current["contract"]
    # The original records are unchanged; extraction-derived signature fields
    # legitimately move with the new generation's proof validity.
    assert old_meta["original_source_sig"] == context_plan.original_source_signature(current["sources"][0])
    assert context_refresh._current_scans(led.db, 1, current["sources"], current["contract"]) == {}
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is None
    pending = context_refresh._pending(led.db, 1, CFG, now=clock.wall)[2]
    assert pending["A"] and pending["B"]
    if module is extract_llm:
        _raw_proof(led, 1, 1)
    # The new extraction contract consumes a new context generation.
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    finish(runtime)
    after = context_refresh._latest(led.db, context_refresh.SCAN, 1, 1)
    scan, meta = after
    assert scan["complete"] and scan["audited"]
    assert meta["contract"] == context_refresh._contract(CFG)
    assert sum("source" in call for call in calls) == 1
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
