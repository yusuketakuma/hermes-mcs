"""Synthetic input-generation and audit completeness contracts (AT-003/035)."""
import semantic_audit
import semantic_extraction
import semantic_llm
import semantic_store
import copy
import time

from semantic_testkit import _FakeJev, _llm, _seeded


def test_fingerprint_changes_with_every_interpretation_input(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db, 1, 1)
        members = bundle["members"]
        baseline = semantic_store.bundle_fingerprint(members)
        for field, value in (
            ("posted_at", "2026-09-21T00:00:00+09:00"),
            ("sender", {"type": "family", "profession": ""}),
            ("body_state", "snippet"),
            ("parent_id", 99),
            ("body_original", "訂正された原文"),
        ):
            changed = copy.deepcopy(members)
            changed[0][field] = value
            assert semantic_store.bundle_fingerprint(changed) != baseline, field
        assert semantic_store.bundle_fingerprint(list(reversed(members))) == baseline
        for column, value in (("sender_name", "別名"), ("sender_id", 42),
                              ("organization", "別組織")):
            before = semantic_store.thread_bundle(db, 1, 1)["source_fingerprint"]
            with db.db:
                db.db.execute(f"UPDATE messages SET {column}=? WHERE message_id=1", (value,))
            assert semantic_store.thread_bundle(db, 1, 1)["source_fingerprint"] != before
        state = semantic_store.jev_state(semantic_store.thread_bundle(db, 1, 1), 1)
        assert "source_metadata" not in state["target"]
    finally:
        db.close()


def test_missing_reply_cannot_receive_complete_audit(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE messages SET reply_count=2 WHERE message_id=1")
        bundle = semantic_store.thread_bundle(db, 1, 1, [1])
        assert bundle["content_quality"] == "partial"
        facts, complete, _ = semantic_extraction.extract_facts(_llm, bundle["members"][0])
        assert complete
        summary = semantic_llm.summarize(_llm, bundle, 1, facts, {})
        findings = semantic_audit.audit_code(bundle, facts, summary)
        assert any(f["code"] == "input_incomplete" for f in findings)
        assert semantic_audit.audit_status_for(findings, [], True, False) == "PENDING"
    finally:
        db.close()


def test_low_confidence_support_is_not_a_pass(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db, 1, 1, [1])
        facts, _, _ = semantic_extraction.extract_facts(_llm, bundle["members"][0])
        summary = semantic_llm.summarize(_llm, bundle, 1, facts, {})
        summary["_facts"] = facts

        class Uncertain(_FakeJev):
            def evaluate(self, state, questions, deadline):
                result = super().evaluate(state, questions, deadline)
                for answer in result["answers"].values():
                    answer["confidence"] = 0.01
                return result

        findings, complete = semantic_audit.audit_claims(
            Uncertain(), bundle, summary, time.monotonic() + 30)
        assert complete
        assert semantic_audit.audit_status_for([], findings, complete, False) == "NEEDS_REVIEW"
    finally:
        db.close()


def test_evidence_span_cannot_extend_past_original(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic_store.thread_bundle(db, 1, 1, [1])
        member = bundle["members"][0]
        facts, _, _ = semantic_extraction.extract_facts(_llm, member)
        evidence = facts[0]["_evidence"]
        evidence.update(start_codepoint=0,
                        end_codepoint=len(member["body_original"]) + 1,
                        quote=member["body_original"])
        summary = semantic_llm.summarize(_llm, bundle, 1, facts, {})
        findings = semantic_audit.audit_code(bundle, facts, summary)
        assert any(f["code"] == "evidence_span_mismatch" for f in findings)
    finally:
        db.close()


def test_impossible_calendar_date_stays_unresolved():
    assert semantic_llm._iso_date("2026-02-31") is None
    assert semantic_llm._iso_date("2024-02-29") == "2024-02-29"
    assert semantic_llm._iso_date("次回1月") is None
