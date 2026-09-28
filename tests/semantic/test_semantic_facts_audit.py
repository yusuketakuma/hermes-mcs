"""Post-generation bidirectional fact audit tests (T9).

fact -> evidence support is judged per verified fact; source -> facts
coverage reuses the shared coverage Choice.  Unevaluated work is
INCOMPLETE, never a silent PASS.
"""
import semantic_audit as audit
import pytest

from semantic_runtime import RuntimeBudget, RuntimeOff, RuntimeStale


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


def test_open_obligations_block_pass_even_when_facts_supported():
    """U06-F01/F05: every remaining fact supported plus a clean coverage
    Choice is still NEEDS_REVIEW while the doc carries open
    obligations — never PASS over an incomplete canonical doc."""
    doc = _doc([_fact("fact_a")], [EV])
    doc["coverage"].update(open_obligation_ids=["obl_x"],
                           status="incomplete")
    result = audit.audit_facts_v2(_Jev(), doc, "アムロジピンを継続。",
                                  deadline=10**9)
    assert result["evaluated"]
    assert result["status"] == "NEEDS_REVIEW"
    assert {"code": "canonical_coverage_incomplete"} in result["findings"]


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


@pytest.mark.parametrize('confidence', [None, True, -0.1, 1.1,
                                        float('nan'), float('inf'), 10**1000],
                         ids=['missing', 'boolean', 'negative', 'over_one',
                              'nan', 'infinity', 'huge_integer'])
def test_invalid_fact_confidence_never_passes(confidence):
    class Invalid(_Jev):
        def evaluate(self, state, questions, deadline):
            result = super().evaluate(state, questions, deadline)
            if 'fact_a' in result['answers']:
                result['answers']['fact_a']['confidence'] = confidence
            return result
    result = audit.audit_facts_v2(Invalid(), _doc([_fact('fact_a')], [EV]),
                                  'synthetic', deadline=10**9)
    assert not result['evaluated'] and result['status'] == 'INCOMPLETE'


@pytest.mark.parametrize('signal', [RuntimeBudget, RuntimeOff, RuntimeStale])
def test_fact_audit_preserves_worker_control_signals(signal):
    class Interrupted(_Jev):
        def evaluate(self, state, questions, deadline):
            raise signal('synthetic boundary')
    with pytest.raises(signal):
        audit.audit_facts_v2(Interrupted(), _doc([_fact('fact_a')], [EV]),
                             'synthetic', deadline=10**9)


@pytest.mark.parametrize('threshold', [True, float('nan'), 10**1000],
                         ids=['boolean', 'nan', 'huge_integer'])
def test_invalid_threshold_is_rejected_before_evaluation(threshold):
    calls = []
    client = _Jev()
    client.evaluate = lambda *args: calls.append(args)
    coverage = audit.evaluate_source_fact_coverage(
        client, 'synthetic', [], deadline=10**9, match_threshold=threshold)
    facts = audit.audit_facts_v2(client, _doc([_fact('fact_a')], [EV]),
                                'synthetic', deadline=10**9,
                                match_threshold=threshold)
    assert not coverage['evaluated'] and not facts['evaluated']
    assert calls == []
