"""Post-generation bidirectional fact audit tests (T9).

fact -> evidence support is judged per verified fact; source -> facts
coverage reuses the shared coverage Choice.  Unevaluated work is
INCOMPLETE, never a silent PASS.
"""
import semantic_audit as audit


def _doc(facts, evidence=()):
    return {"version": "semantic-facts/v2",
            "source": {"message_id": "m1", "revision": "r1",
                       "content_hash": "h", "body_codepoints": 20,
                       "content_quality": "full",
                       "attachments_complete": True,
                       "source_fingerprint": "sf_x"},
            "atoms": [], "chunks": [], "obligations": [],
            "evidence": list(evidence), "facts": facts,
            "relations": [],
            "coverage": {"category_counts": {},
                         "open_obligation_ids": [], "limitations": [],
                         "status": "complete"}}


def _fact(fid, verified=True, evidence_ids=("ev_1",)):
    return {"fact_id": fid, "kind": "medication_event",
            "subject": "patient:1", "actor": "sender:s1",
            "statement": "アムロジピン継続", "polarity": "affirmed",
            "epistemic": "asserted", "workflow_status": "performed",
            "event_time": "unknown", "valid_time": "unknown",
            "evidence_ids": list(evidence_ids), "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "verified" if verified else "unverified",
            "action": "continue"}


EV = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
      "start": 0, "end": 9, "quote": "アムロジピン", "atom_id": "atom_1"}


class _Jev:
    """Scripted audit stub: fact verdicts keyed by fact_id, plus the
    shared coverage Choice answered from ``coverage_choice``."""

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


def test_audit_passes_when_all_supported():
    doc = _doc([_fact("fact_a")], [EV])
    result = audit.audit_facts_v2(_Jev(), doc, "アムロジピンを継続。",
                                  deadline=10**9)
    assert result["evaluated"] and result["status"] == "PASS"
    assert result["fact_verdicts"]["fact_a"]["choice"] == "supports"


def test_audit_flags_contradicted_and_ambiguous_facts():
    doc = _doc([_fact("fact_a"), _fact("fact_b")], [EV])
    jev = _Jev({"fact_a": "contradicts", "fact_b": "ambiguous"})
    result = audit.audit_facts_v2(jev, doc, "source", deadline=10**9)
    assert result["evaluated"] and result["status"] == "NEEDS_REVIEW"
    codes = {f["code"] for f in result["findings"]}
    assert "fact_contradicts" in codes and "fact_ambiguous" in codes


def test_unverified_fact_finding_without_jev_verdict():
    doc = _doc([_fact("fact_a", verified=False, evidence_ids=[])], [])
    result = audit.audit_facts_v2(_Jev(), doc, "src", deadline=10**9)
    assert any(f["code"] == "unverified_facts"
               for f in result["findings"])
    assert result["status"] == "NEEDS_REVIEW"


def test_no_jev_client_is_incomplete_not_pass():
    doc = _doc([_fact("fact_a")], [EV])
    result = audit.audit_facts_v2(None, doc, "src", deadline=10**9)
    assert result["status"] == "INCOMPLETE"
    assert not result["evaluated"]
    assert any(f["code"] == "fact_audit_unevaluated"
               for f in result["findings"])


def test_jev_failure_mid_audit_is_incomplete():
    doc = _doc([_fact("fact_a")], [EV])
    result = audit.audit_facts_v2(_Jev(fail=True), doc, "src",
                                  deadline=10**9)
    assert result["status"] == "INCOMPLETE"
    assert not result["evaluated"]


def test_coverage_missing_is_needs_review():
    doc = _doc([_fact("fact_a")], [EV])
    jev = _Jev(coverage="missing")
    result = audit.audit_facts_v2(jev, doc, "src", deadline=10**9)
    assert result["evaluated"]
    assert result["status"] == "NEEDS_REVIEW"
    assert any(f["code"] == "source_fact_coverage_missing"
               for f in result["findings"])


def test_audit_target_carries_structured_fields():
    seen = {}

    class Spy(_Jev):
        def evaluate(self, state, questions, deadline):
            if "source_fact_coverage" not in questions:
                seen["target"] = state["target"]["text"]
                seen["ctx_roles"] = [c["role"]
                                     for c in state["context"]]
            return super().evaluate(state, questions, deadline)

    doc = _doc([_fact("fact_a")], [EV])
    audit.audit_facts_v2(Spy(), doc, "アムロジピンを継続。",
                         deadline=10**9)
    assert "polarity:affirmed" in seen["target"]
    assert "action:continue" in seen["target"]
    assert "evidence_quote" in seen["ctx_roles"]
    assert "evidence_context" in seen["ctx_roles"]
