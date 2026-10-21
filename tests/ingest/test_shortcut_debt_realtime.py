"""Realtime claims include fragmented batch and single-fallback budget floors."""
import time
from contextlib import closing

import pytest

import extract_llm
import run_check
from ingest_testkit import _ledger


@pytest.mark.parametrize("batch_k,budget,expected", [
    (0, 90, 1), (0, 600, 8), (1, 600, 8),
    (2, 600, 3), (4, 600, 3), (4, 190, 1), (4, 90, 1),
])
def test_realtime_limit_includes_batch_fallback(tmp_path, monkeypatch, batch_k, budget, expected):
    monkeypatch.setattr(extract_llm, "_BATCH_K", batch_k)
    monkeypatch.setattr(extract_llm, "pinned_slot_busy", lambda deadline: False)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda ledger, cfg: None)
    seen = []
    monkeypatch.setattr(extract_llm, "run_pending", lambda ledger, **kw: (
        seen.append(kw) or {"done": 0, "failed": 0, "pids": []}))
    with closing(_ledger(tmp_path)) as ledger:
        result = {"errors": [], "realtime_ids": [1]}
        run_check.stage_derive(ledger, result, time.monotonic() + 1000, {}, llm_budget_cap=budget)
    assert len(seen) == 1
    assert seen[0]["limit"] == expected
    assert seen[0]["budget_s"] == budget
    assert seen[0]["batch_k"] == batch_k
    assert seen[0]["admitted_ids"] == {1}
    assert seen[0]["oldest_first"] is False
