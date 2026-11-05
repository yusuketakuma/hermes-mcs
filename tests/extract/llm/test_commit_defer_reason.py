"""One rejected write contributes one fixed defer reason, including legacy callers."""
import extract_llm_runner
import pytest

import extract_llm as worker
from test_anthropic_runtime import old, routed as routed


@pytest.mark.parametrize("reason,expected", [("completeness", "commit_completeness"), (None, "commit_rejected")])
def test_run_pending_counts_rejected_commit_exactly_once(routed, monkeypatch, reason, expected):
    ledger, _cfg, _calls, _reserves, _results = routed
    old(ledger)
    observed = []

    def rejected(_ledger, row, *args, **kwargs):
        observed.append(True)
        return worker._commit_rejected(row, reason) if reason is not None else "rejected"

    monkeypatch.setattr(worker, "_commit_extraction_atomic", rejected)
    monkeypatch.setattr(extract_llm_runner, '_commit_extraction_atomic', worker._commit_extraction_atomic)
    result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert observed == [True]
    assert result["done"] == 0 and result["deferred"] == 1
    assert result["defer_reasons"] == {expected: 1}
