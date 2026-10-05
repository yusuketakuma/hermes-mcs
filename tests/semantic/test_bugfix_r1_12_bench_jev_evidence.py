"""The bench --jev audit must see the same evidence context as the drain."""
import semantic_bench as bench

QUOTE = "合成薬を投与"


class _StubJev:
    requests_made = 0

    def __init__(self):
        self.contexts = []

    def evaluate(self, state, questions, deadline):
        self.contexts.append(state["context"])
        key = next(iter(questions))
        return {"answers": {key: {"choice": "supported", "confidence": 0.9}}}


def test_run_case_passes_fact_evidence_to_jev(monkeypatch):
    fact = {"fact_id": "f1", "statement": QUOTE, "evidence_refs": ["e1"],
            "validation_status": "verified",
            "_evidence": {"evidence_id": "e1", "message_id": 1, "quote": QUOTE,
                          "start_codepoint": 0, "end_codepoint": len(QUOTE)}}
    summary = {"claims": [{"claim_id": "c1", "section": "status", "text": QUOTE,
                           "claim_kind": "reported_fact", "fact_refs": [0],
                           "evidence_refs": ["e1"], "status": "execution_reported",
                           "polarity": "affirmed"}], "limitations": []}
    monkeypatch.setattr(bench, "extract_facts",
                        lambda *a, **k: ([fact], True, 0, None))
    monkeypatch.setattr(bench, "summarize", lambda *a, **k: (summary, None))
    monkeypatch.setattr(bench, "audit_code", lambda *a: [])
    member = {"message_id": 1, "project_id": 1, "parent_id": None,
              "posted_at": "2026-01-01", "sender": {"id": 1, "name": "合成", "type": "staff"},
              "body_original": QUOTE, "body_state": "full", "revision": "r",
              "attachments": [], "replies_complete": True, "bundle_role": "target"}
    case = {"case_id": "synthetic:x", "project_id": 1, "root_id": 1,
            "target_id": 1, "members": [member]}
    jev = _StubJev()
    bench._run_case(case, None, jev)
    assert jev.contexts
    quotes = [c["text"] for ctx in jev.contexts for c in ctx
              if c.get("role") == "evidence_quote"]
    assert quotes and all(q == QUOTE for q in quotes)
