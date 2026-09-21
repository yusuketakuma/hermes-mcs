"""Synthetic input-generation and audit completeness contracts (AT-003/035)."""
import copy
import time

import semantic
from test_mcs_semantic import _FakeJev, _llm, _seeded


def test_fingerprint_changes_with_every_interpretation_input(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
        members = bundle["members"]
        baseline = semantic.bundle_fingerprint(members)
        for field, value in (
            ("posted_at", "2026-09-21T00:00:00+09:00"),
            ("sender", {"type": "family", "profession": ""}),
            ("body_state", "snippet"),
            ("parent_id", 99),
            ("body_original", "訂正された原文"),
        ):
            changed = copy.deepcopy(members)
            changed[0][field] = value
            assert semantic.bundle_fingerprint(changed) != baseline, field
        assert semantic.bundle_fingerprint(list(reversed(members))) == baseline
        for column, value in (("sender_name", "別名"), ("sender_id", 42),
                              ("organization", "別組織")):
            before = semantic.thread_bundle(db, 1, 1)["source_fingerprint"]
            with db.db:
                db.db.execute(f"UPDATE messages SET {column}=? WHERE message_id=1", (value,))
            assert semantic.thread_bundle(db, 1, 1)["source_fingerprint"] != before
        state = semantic.jev_state(semantic.thread_bundle(db, 1, 1), 1)
        assert "source_metadata" not in state["target"]
    finally:
        db.close()


def test_missing_reply_cannot_receive_complete_audit(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE messages SET reply_count=2 WHERE message_id=1")
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        assert bundle["content_quality"] == "partial"
        facts, complete, _ = semantic.extract_facts(_llm, bundle["members"][0])
        assert complete
        summary = semantic.summarize(_llm, bundle, 1, facts, {})
        findings = semantic.audit_code(bundle, facts, summary)
        assert any(f["code"] == "input_incomplete" for f in findings)
        assert semantic.audit_status_for(findings, [], True, False) == "PENDING"
    finally:
        db.close()


def test_low_confidence_support_is_not_a_pass(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        facts, _, _ = semantic.extract_facts(_llm, bundle["members"][0])
        summary = semantic.summarize(_llm, bundle, 1, facts, {})
        summary["_facts"] = facts

        class Uncertain(_FakeJev):
            def evaluate(self, state, questions, deadline):
                result = super().evaluate(state, questions, deadline)
                for answer in result["answers"].values():
                    answer["confidence"] = 0.01
                return result

        findings, complete = semantic.audit_claims(
            Uncertain(), bundle, summary, time.monotonic() + 30)
        assert complete
        assert semantic.audit_status_for([], findings, complete, False) == "NEEDS_REVIEW"
    finally:
        db.close()


def test_evidence_span_cannot_extend_past_original(tmp_path):
    db = _seeded(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1, [1])
        member = bundle["members"][0]
        facts, _, _ = semantic.extract_facts(_llm, member)
        evidence = facts[0]["_evidence"]
        evidence.update(start_codepoint=0,
                        end_codepoint=len(member["body_original"]) + 1,
                        quote=member["body_original"])
        summary = semantic.summarize(_llm, bundle, 1, facts, {})
        findings = semantic.audit_code(bundle, facts, summary)
        assert any(f["code"] == "evidence_span_mismatch" for f in findings)
    finally:
        db.close()


def test_impossible_calendar_date_stays_unresolved():
    assert semantic._iso_date("2026-02-31") is None
    assert semantic._iso_date("2024-02-29") == "2024-02-29"
    assert semantic._iso_date("次回1月") is None
