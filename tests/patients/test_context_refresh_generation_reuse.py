"""An extraction generation change reuses context scans whose input items and summaries are unchanged."""
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
from test_patient_context_refresh import CFG, _raw_proof, finish, runtime, seed, step

__all__ = ["runtime"]


def _bump(monkeypatch, module, version):
    monkeypatch.setattr(module, version, getattr(module, version) + 1)
    if module is extract_llm:
        # EXTRACT_VERSION is shared across the split extract_llm modules; a real
        # generation bump moves all of them together.
        for sub in (extract_llm_context, extract_llm_errors, extract_llm_prepare,
                    extract_llm_progress, extract_llm_proofs, extract_llm_provider,
                    extract_llm_qc, extract_llm_resident, extract_llm_selection,
                    extract_llm_store):
            monkeypatch.setattr(sub, "EXTRACT_VERSION", extract_llm.EXTRACT_VERSION)


def _scan_rows(led):
    return led.db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=1 ORDER BY artifact_id",
                          (context_refresh.SCAN,)).fetchall()


@pytest.mark.parametrize("module,version", [(extract, "RULE_VERSION"), (extract_llm, "EXTRACT_VERSION")])
def test_generation_change_keeps_scan_when_the_scan_input_is_reproduced(runtime, monkeypatch, module, version):
    led, clock, calls = runtime
    seed(led, body="見出しのない昔の支援記録を原文のまま残す。")
    finish(runtime)
    before = led.db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=1",
                            (context_refresh.SCAN,)).fetchone()
    old_scan, old_meta = json.loads(before[0]), json.loads(before[1])
    assert old_scan["complete"] and old_scan["audited"]
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
    scans = _scan_rows(led)
    calls.clear()
    _bump(monkeypatch, module, version)
    current = context_refresh._snapshot(led.db, 1, CFG)
    # Generations are no longer part of the scan contract.
    assert old_meta["contract"] == current["contract"] == context_refresh._contract()
    assert old_meta["original_source_sig"] == context_plan.original_source_signature(current["sources"][0])
    if module is extract_llm:
        # The old row's material proof no longer holds, so its summary is not
        # read and the patient's chat source is excluded from scanning
        # (extract_llm.pending_pred and the material proof in
        # _eligible_source_keys): no degraded scan runs and no SCAN row is added.
        step(runtime)
        assert not any("source" in call for call in calls)
        assert _scan_rows(led) == scans
        # Re-extraction reproduces the same summary and context items.
        _raw_proof(led, 1, 1)
        current = context_refresh._snapshot(led.db, 1, CFG)
    assert context_plan.source_signature(current["sources"][0]) == old_meta["source_sig"]
    assert context_refresh._current_scans(led.db, 1, current["sources"], current["route"])
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    finish(runtime)
    assert not any("source" in call for call in calls)
    assert _scan_rows(led) == scans
    assert context_refresh.context_view_input(led.db, 1, cfg=CFG) is not None
