"""Synthetic negative quality evidence must invalidate accepted replay completion."""
import json
import time

import pytest

import patient_context_refresh as refresh
import rollup
import semantic_drain as drain
import semantic_jev
from test_anthropic_context_refresh import CFG, cloud as cloud, drain as finish_cloud
from test_patient_context_refresh import runtime as runtime, seed, step

__all__ = ["cloud", "runtime"]


def _bookmark(ledger, cfg):
    for _ in range(12):
        result = drain.reprocess_step(ledger, cfg)
        if result.get("phase") in ("complete", "waiting"):
            return result
    pytest.fail("synthetic replay did not reach a completion bookmark")


def test_negative_quality_reopens_completed_bookmark(runtime, cloud, monkeypatch):
    ledger, clock, _ = runtime
    clock.wall = time.time()
    monkeypatch.setattr(drain, "time", refresh.time)
    monkeypatch.setattr(rollup, "time", refresh.time)
    monkeypatch.setitem(CFG["semantic"], "summary_mode", "assist")
    monkeypatch.setitem(CFG["semantic"], "daily_request_budget", 0)
    seed(ledger, body="完全合成の居住記録です。")
    finish_cloud(runtime)
    assert refresh.inference_coverage(ledger.db, 1, CFG)["complete"]
    assert ledger.artifacts("patient_context_source_quality_skipped_v1")
    rollup.rebuild(ledger, 1, cfg=CFG)
    # Post-level interpretation is independent of this patient-quality epoch.
    monkeypatch.setattr(drain, "_reprocess_target_state", lambda *_args: "current")
    before = _bookmark(ledger, CFG)
    assert before["complete"] and before["patient_counts"] == {"current": 1}
    protected_jobs = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")]

    monkeypatch.setitem(CFG["semantic"], "daily_request_budget", 3000)

    def negative(body, timeout):
        assert timeout > 0 and not ledger.db.in_transaction
        answers = {}
        for qid, question in body["questions"].items():
            choices = question["criteria"]
            choice = "not_supported" if "not_supported" in choices else "missing"
            answers[qid] = {"type": "choice", "choice": choice, "confidence": 1.0,
                            "probabilities": {key: float(key == choice) for key in choices}}
        return 200, {}, json.dumps({"model": semantic_jev.JEV_MODEL, "answers": answers,
                                    "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()

    # The existing cloud fixture binds a stub client; replace its POST result only.
    from test_anthropic_context_refresh import _SOURCE_REPAIR_JEV
    monkeypatch.setattr(semantic_jev, "JevClient",
                        lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=negative, **kwargs))
    for _ in range(12):
        step(runtime, cfg=CFG)
        if ledger.artifacts("patient_context_source_quality_fail_v1"):
            break
    else:
        pytest.fail("synthetic quality recheck did not retain its negative answer")
    assert not refresh.inference_coverage(ledger.db, 1, CFG)["complete"]
    after = _bookmark(ledger, CFG)
    assert not after["complete"]
    assert after["analysis_epoch"] > before["analysis_epoch"]
    assert after["proof_generation"] == before["proof_generation"]
    assert after["seed_cursor"] == before["seed_cursor"]
    assert protected_jobs == [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")]
