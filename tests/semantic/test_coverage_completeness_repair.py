"""Fictional coverage-only omissions share the existing repair reservation and audit gates."""
import json
import time

import pytest

import semantic
import semantic_drain as drain
import semantic_facts as facts
from semantic_policy import KIND_FACT_REPAIR
from semantic_testkit import _canonical_cfg, _FakeJev, _ledger, _message, _patient


FIRST = "本人は朝の架空資料を希望します。"
SECOND = "本人は夕方の架空資料も希望します。"


def preference(quote):
    return {"kind": "preference", "statement": quote, "subject_role": "patient",
            "polarity": "affirmed", "epistemic": "reported", "workflow_status": "reported",
            "evidence_quote": quote}


class CoverageJev(_FakeJev):
    def __init__(self, terminal=None):
        super().__init__(choice_map={"has_" + category: "present" if category == "preference"
                                    else "absent" for category in facts.MANDATORY_CATEGORIES})
        self.terminal = terminal

    def evaluate(self, state, questions, deadline):
        if "source_fact_coverage" in questions:
            statements = [row["text"] for row in state["context"]]
            self.choice_map["source_fact_coverage"] = self.terminal or (
                "complete" if SECOND in statements else "missing")
        return super().evaluate(state, questions, deadline)


@pytest.mark.parametrize("terminal", [None, "missing", "ambiguous"])
def test_coverage_only_finding_can_dispatch_once_then_requires_a_fresh_pass(tmp_path, terminal):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body=FIRST + SECOND)])
    bundle = semantic.thread_bundle(db, 1, 1, [1])
    member = bundle["members"][0]
    scfg, errors = semantic.semantic_config(_canonical_cfg())
    assert not errors
    calls = []

    def model(prompt):
        calls.append(prompt)
        presence = dict.fromkeys(facts.MANDATORY_CATEGORIES, "none")
        presence["preference"] = "one"
        return json.dumps({"facts": [preference(FIRST)] + (
            [preference(SECOND)] if len(calls) > 1 else []), "category_presence": presence})

    client = CoverageJev(terminal)
    try:
        stage = drain._fact_stage(db, scfg, member, 1, 1, bundle["source_fingerprint"],
                                  semantic.policy_fingerprint(scfg), client, model,
                                  time.monotonic() + 600)
        assert len(calls) == (1 if terminal == "ambiguous" else 2)
        assert (stage["outcome"] is None) is (terminal is None)
        assert bool(db.artifacts(KIND_FACT_REPAIR)) is (terminal != "ambiguous")
        if terminal is None:
            assert {fact["statement"] for fact in stage["v2_doc"]["facts"]} >= {FIRST, SECOND}
            assert stage["v2_doc"]["coverage"]["status"] == "complete"
        else:
            assert stage["outcome"] == "hard_fail"
            assert not db.artifacts("canonical_projection")
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()


def test_coverage_repair_cannot_promote_a_fact_in_a_category_adjudicated_absent(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body=FIRST + SECOND)])
    bundle = semantic.thread_bundle(db, 1, 1, [1])
    scfg, _ = semantic.semantic_config(_canonical_cfg())
    calls = []

    def model(prompt):
        calls.append(prompt)
        presence = dict.fromkeys(facts.MANDATORY_CATEGORIES, "none")
        presence["preference"] = "one"
        extra = {**preference(SECOND), "kind": "other_observation"}
        return json.dumps({"facts": [preference(FIRST)] + ([extra] if len(calls) > 1 else []),
                           "category_presence": presence})

    try:
        result = drain._fact_stage(db, scfg, bundle["members"][0], 1, 1,
                                   bundle["source_fingerprint"], semantic.policy_fingerprint(scfg),
                                   CoverageJev(), model, time.monotonic() + 600)
        assert len(calls) == 2 and result["outcome"] == "hard_fail"
        doc = result["v2_doc"]
        assert doc["coverage"]["status"] == "incomplete"
        assert any(ob["category"] == "other_observation" and ob["status"] == "ambiguous"
                   for ob in doc["obligations"])
        assert not db.artifacts("canonical_projection") and not db.artifacts("semantic_facts_v4")
    finally:
        db.close()
