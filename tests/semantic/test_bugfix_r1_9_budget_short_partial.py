"""A budget stop on a later target's first summarize keeps earlier results."""

import json
import time

import semantic
import semantic_runtime as runtime
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded_two


def test_budget_short_on_second_target_commits_first(tmp_path, monkeypatch):
    state = {"sum": 0, "aud": 0}

    def llm(prompt):
        if "要約器" in prompt:
            state["sum"] += 1
            if state["sum"] == 2:      # target 2's first summarize
                raise runtime.RuntimeBudgetShort("llm_next_call")
        return _llm(prompt)

    real = semantic.audit_claims

    def audit(*a):
        state["aud"] += 1
        return real(*a)

    monkeypatch.setattr(semantic, "audit_claims", audit)
    db = _seeded_two(tmp_path)
    cfg = _cfg("shadow", loop_mode="off")
    jev = _FakeJev()
    r1 = semantic.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                          jev_client=jev, llm_fn=llm)
    assert r1["job_metrics"][0]["status"] == "deferred_short"
    rows = db.artifacts("semantic_summary", message_id=1)
    assert rows, "target 1's audited summary was discarded"
    status1 = json.loads(rows[-1]["meta"] or "{}").get("audit_status")
    assert status1 in ("PASS", "NEEDS_REVIEW")
    audits_run1 = state["aud"]

    db.db.execute("update fetch_jobs set next_try=0")
    db.db.commit()
    semantic.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                     jev_client=jev, llm_fn=llm)
    # only target 2 is audited on the resumed pass
    assert state["aud"] == audits_run1 + 1
    assert db.artifacts("semantic_summary", message_id=2)
