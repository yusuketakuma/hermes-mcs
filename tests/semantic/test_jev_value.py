"""Jev incremental-value evaluation tests (T14).

The report must split deterministic findings from Jev-only detections,
measure mandatory-fact recall, and gate on complete evaluation —
partial evidence never passes.
"""
import json

import pytest

import semantic_evaluation as seval
import semantic_facts as sf


NO_FACTS = {c: "none" for c in sf.MANDATORY_CATEGORIES}


def _llm(facts, presence=None):
    def call(_prompt):
        return json.dumps(
            {"facts": facts,
             "category_presence": dict(NO_FACTS, **(presence or {}))},
            ensure_ascii=False)
    return call


class _Jev:
    """Answers fact-support from a verdict map and coverage Choice."""

    def __init__(self, verdicts=None, coverage="complete", fail=False):
        self.verdicts = verdicts or {}
        self.coverage = coverage
        self.fail = fail
        self.last_error = None

    def evaluate(self, state, questions, deadline):
        if self.fail:
            raise RuntimeError("jev down")
        answers = {}
        for key in questions:
            if key == "source_fact_coverage":
                answers[key] = {"choice": self.coverage,
                                "confidence": 0.95}
            else:
                choice = self.verdicts.get(key, "supports")
                answers[key] = {"choice": choice, "confidence": 0.95}
        return {"answers": answers}


BODY = "アムロジピン5mgを継続します。"
CASES = [{"id": "c1", "body": BODY,
          "expect": {"facts": [
              {"statement": "アムロジピン5mg継続", "mandatory": True,
               "evidence_quote": "アムロジピン5mgを継続"}]}}]

GOOD_FACTS = [{"statement": "アムロジピン5mg継続",
               "kind": "medication_event", "action": "continue",
               "subject_role": "patient", "polarity": "affirmed",
               "workflow_status": "performed", "importance": "T1",
               "evidence_quote": "アムロジピン5mgを継続"}]


def test_jev_value_report_counts_incremental_findings():
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    jev = _Jev(coverage="missing")   # coverage-miss -> Jev-only finding
    report = seval.evaluate_jev_incremental(CASES, llm, jev)
    assert report["evaluated"] and report["gate"]["pass"]
    assert report["schema_version"] == "jev-value-v1"
    audit = report["audit"]
    assert audit["jev_incremental_findings"] >= 1
    assert audit["incremental_share"] == 1.0
    assert report["extraction"]["mandatory_recall"] == 1.0


def test_jev_value_contradicted_fact_counts_incremental():
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    # Reject every fact-support question -> one Jev-only finding/fact.
    jev = _Jev(verdicts={}, coverage="complete")
    original = jev.evaluate

    def contradicting(state, questions, deadline):
        out = original(state, questions, deadline)
        for key in questions:
            if key != "source_fact_coverage":
                out["answers"][key]["choice"] = "contradicts"
        return out
    jev.evaluate = contradicting
    report = seval.evaluate_jev_incremental(CASES, llm, jev)
    codes = [c for case in report["per_case"] for c in
             [case["jev_incremental_findings"]]]
    assert report["audit"]["jev_incremental_findings"] >= 1
    assert all(n >= 1 for n in codes)


def test_unevaluated_audit_blocks_gate():
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    report = seval.evaluate_jev_incremental(
        CASES, llm, _Jev(fail=True))
    assert not report["evaluated"]
    assert not report["gate"]["pass"]


def test_no_jev_client_is_not_evaluated():
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    report = seval.evaluate_jev_incremental(CASES, llm, None)
    assert not report["evaluated"]
    assert not report["gate"]["pass"]


def test_mandatory_recall_tracks_expected_quotes():
    # Model reports the wrong fact: the expected quote is never covered.
    llm = _llm([{"statement": "別の事実", "kind": "symptom_state",
                 "subject_role": "patient", "polarity": "affirmed",
                 "workflow_status": "reported", "importance": "T3",
                 "evidence_quote": "継続します"}],
               presence={"symptom_state": "one"})
    report = seval.evaluate_jev_incremental(CASES, llm, _Jev())
    assert report["extraction"]["mandatory_expected"] == 1
    assert report["extraction"]["mandatory_matched"] == 0
    assert report["extraction"]["mandatory_recall"] == 0.0


# ---------- shadow e2e driver (T16) ----------

def test_shadow_e2e_runs_all_stages():
    cases = [{"id": "e2e-1", "body": BODY,
              "expect": {"facts": []}}]
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    report = seval.run_shadow_e2e(cases, llm, _Jev())
    assert report["schema_version"] == "shadow-e2e-v1"
    case = report["per_case"][0]
    assert case["stages"]["extract"] == "ok"
    assert case["stages"]["audit"] == "PASS"
    assert "facts" in case["stages"]["render"]
    assert report["complete"] == 1


def test_shadow_e2e_repair_stage_visible():
    llm = _llm(GOOD_FACTS, presence={"medication": "one"})
    jev = _Jev()
    original = jev.evaluate
    calls = {"n": 0}

    def once_contradict(state, questions, deadline):
        calls["n"] += 1
        out = original(state, questions, deadline)
        if calls["n"] == 1:
            for key in questions:
                if key != "source_fact_coverage":
                    out["answers"][key]["choice"] = "contradicts"
        return out
    jev.evaluate = once_contradict
    report = seval.run_shadow_e2e([{"id": "r1", "body": BODY}], llm, jev)
    stages = report["per_case"][0]["stages"]
    assert stages["repair"] in ("repaired", "failed", "skipped")
    assert stages["audit"] is not None


def test_shadow_e2e_extraction_failure_recorded():
    def dead(_prompt):
        raise ConnectionError("down")
    report = seval.run_shadow_e2e([{"id": "x", "body": BODY}], dead, _Jev())
    stages = report["per_case"][0]["stages"]
    # A model outage surfaces as an incomplete extraction stage (the
    # contract converts model failure to non-terminal incomplete), and
    # the case can never report a pass.
    assert stages["extract"] in ("incomplete",) or \
        stages["extract"].startswith("error:")
    assert not report["per_case"][0]["passed"]


@pytest.mark.parametrize("failure", ["audit", "unevaluated", "relations"])
def test_shadow_e2e_later_message_cannot_hide_failed_stage(monkeypatch, failure):
    import semantic_audit
    import semantic_relations

    calls = []

    def audit(*args):
        first = not calls
        calls.append(1)
        return {"evaluated": not (first and failure == "unevaluated"),
                "status": "NEEDS_REVIEW" if first and failure != "relations" else "PASS",
                "findings": []}

    if failure == "relations":
        reconcile = semantic_relations.reconcile_facts
        def broken(active, new):
            if active:
                raise ValueError("synthetic reconciliation failure")
            return reconcile(active, new)
        monkeypatch.setattr(semantic_relations, "reconcile_facts", broken)
    monkeypatch.setattr(semantic_audit, "audit_facts_v2", audit)
    cases = [{"id": "thread", "messages": [
        {"message_id": i, "body": BODY} for i in (1, 2)]}]
    report = seval.run_shadow_e2e(
        cases, _llm(GOOD_FACTS, presence={"medication": "one"}), _Jev())
    assert len(calls) == 2
    assert report["complete"] == 0
    case = report["per_case"][0]
    assert not case["passed"]
    if failure != "relations":
        assert case["stages"]["audit"] == "NEEDS_REVIEW"


def test_shadow_e2e_honors_chunk_budget():
    prompts = []
    body = "本日は特記なし。\n" * 20
    def llm(prompt):
        prompts.append(prompt)
        return _llm([])(prompt)

    seval.run_shadow_e2e([{"id": "chunked", "body": body}], llm, _Jev(), chunk_size=30)
    assert len(prompts) > 1


def test_failed_extraction_keeps_expected_facts_in_recall_denominator(monkeypatch):
    import semantic_extraction

    def fail(*args, **kwargs):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(semantic_extraction, "extract_facts_v2", fail)
    report = seval.evaluate_jev_incremental(CASES, _llm([]), _Jev())
    assert report["extraction"]["mandatory_expected"] == 1
    assert report["extraction"]["mandatory_matched"] == 0
    assert report["extraction"]["mandatory_recall"] == 0.0
    assert not report["gate"]["pass"]


def test_unused_evidence_cannot_claim_extracted_fact_recall():
    doc = {"facts": [], "evidence": [{"evidence_id": "e1", "quote": BODY}]}
    assert seval._expected_matched(doc, [{"evidence_quote": BODY}]) == 0


def test_incomplete_render_cannot_pass_shadow_report(monkeypatch):
    import semantic_render

    original = semantic_render.mandatory_render

    def tiny_page(doc):
        return original(doc, page_budget=1)

    monkeypatch.setattr(semantic_render, "mandatory_render", tiny_page)
    report = seval.run_shadow_e2e(CASES,
        _llm(GOOD_FACTS, presence={"medication": "one"}), _Jev())
    assert report["complete"] == 0
    assert not report["per_case"][0]["passed"]
    assert "incomplete" in report["per_case"][0]["stages"]["render"]


def test_shadow_relations_use_repaired_facts_for_following_message(monkeypatch):
    import semantic_audit
    import semantic_extraction
    import semantic_relations
    from test_semantic_mandatory_render import _doc, _fact

    def extract(_llm, member, *args, **kwargs):
        return {"doc": _doc(facts=[_fact(f"message-{member['message_id']}")]),
                "extraction_complete": True}

    def audit(_client, doc, *args):
        rejected = [f for f in doc["facts"] if f["statement"] == "message-1"]
        return {"evaluated": True, "status": "NEEDS_REVIEW" if rejected else "PASS",
                "findings": [{"fact": f["fact_id"], "code": "fact_not_supported"}
                             for f in rejected]}

    def repair(*args, **kwargs):
        return {"repaired": True, "doc": _doc(facts=[_fact("repaired")])}

    histories = []

    def relations(active, new):
        histories.append([f["statement"] for f in active])
        return {"relations": []}

    monkeypatch.setattr(semantic_extraction, "extract_facts_v2", extract)
    monkeypatch.setattr(semantic_extraction, "repair_facts_v2", repair)
    monkeypatch.setattr(semantic_audit, "audit_facts_v2", audit)
    monkeypatch.setattr(semantic_relations, "reconcile_facts", relations)
    report = seval.run_shadow_e2e([{"id": "thread", "messages": [
        {"message_id": 1, "body": BODY}, {"message_id": 2, "body": BODY}]}], None, None)
    assert report["complete"] == 1
    assert histories[-1] == ["repaired"]
