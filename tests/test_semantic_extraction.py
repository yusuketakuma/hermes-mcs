"""Durable extraction and source-to-facts coverage contracts."""
import json
import time

import pytest

import semantic_audit
import semantic_extraction as extraction
from semantic_runtime import RuntimeGuardError
from test_mcs_semantic import BODY, _FakeJev, _ledger, _llm, _seeded, _cfg


def _member(body):
    return {"project_id": 1, "message_id": 7, "revision": "rev-1",
            "body_original": body}


def test_chunk_failure_persists_prefix_and_replay_reuses_it(tmp_path):
    body = BODY + "後半重要イベント"
    cut = body.index("後半重要イベント")

    def chunker(source, _size):
        return [source[:cut], source[cut:]]

    calls = []

    def fail_on_second(prompt):
        calls.append(prompt)
        if len(calls) == 2:
            raise RuntimeError("synthetic model failure")
        parsed = json.loads(_llm(prompt))
        parsed["facts"][0]["statement"] = "前半の" + "詳細" * 120
        return json.dumps(parsed, ensure_ascii=False)

    db = _ledger(tmp_path)
    try:
        first = extraction.extract_facts_resumable(
            fail_on_second, _member(body), ledger=db, project_id=1,
            source_fingerprint="bundle-fp", chunker=chunker, chunk_size=8)
        assert not first["complete"]
        assert first["failure_reason"] == "model"
        assert first["completed_chunks"] == [0]
        assert first["pending_chunks"] == [1]
        assert len(first["facts"][0]["statement"]) > 200
        assert len(db.artifacts(extraction.KIND_CHUNK, project_id=1,
                                message_id=7)) == 1
        evidence = first["facts"][0]["_evidence"]
        assert body[evidence["start_codepoint"]:evidence["end_codepoint"]] \
            == evidence["quote"]

        replay_calls = []

        def replay(prompt):
            replay_calls.append(prompt)
            parsed = json.loads(_llm(prompt))
            parsed["facts"][0]["statement"] = "後半の重要イベント"
            parsed["facts"][0]["evidence_quote"] = "後半重要イベント"
            return json.dumps(parsed, ensure_ascii=False)

        second = extraction.extract_facts_resumable(
            replay, _member(body), ledger=db, project_id=1,
            source_fingerprint="bundle-fp", chunker=chunker, chunk_size=8)
        assert second["complete"]
        assert second["failure_reason"] is None
        assert second["reused_chunks"] == [0]
        assert replay_calls and len(replay_calls) == 1
        assert len(second["facts"]) == 2
        second_evidence = second["facts"][1]["_evidence"]
        assert body[second_evidence["start_codepoint"]:
                    second_evidence["end_codepoint"]] == second_evidence["quote"]
        assert len(db.artifacts(extraction.KIND_CHUNK, project_id=1,
                                message_id=7)) == 2

        def invalid_item(prompt):
            parsed = json.loads(_llm(prompt))
            parsed["facts"].append({"statement": ""})
            return json.dumps(parsed, ensure_ascii=False)

        invalid = extraction.extract_facts_resumable(
            invalid_item, {**_member(body), "message_id": 8}, project_id=1,
            source_fingerprint="invalid-fp", chunker=chunker, chunk_size=8)
        assert not invalid["complete"]
        assert invalid["failure_reason"] == "model"
        assert invalid["dropped_in_failed_chunk"] == 1
        assert invalid["completed_chunks"] == []
        assert not db.artifacts(extraction.KIND_CHUNK, project_id=1,
                                message_id=8)

        def guarded(_prompt):
            raise RuntimeGuardError("stale")

        with pytest.raises(RuntimeGuardError):
            extraction.extract_facts_resumable(
                guarded, _member(body), chunker=chunker, chunk_size=8)
    finally:
        db.close()


def test_source_fact_coverage_reports_findings_and_unknown_technical_state(tmp_path):
    source = "重要イベントAと重要イベントBの記載"
    facts = [{"fact_id": "f1", "statement": "重要イベントA"}]

    class CaptureJev(_FakeJev):
        def __init__(self, choice):
            super().__init__(choice_map={"source_fact_coverage": choice})
            self.state = None
            self.questions = None

        def evaluate(self, state, questions, deadline):
            self.state = state
            self.questions = questions
            return super().evaluate(state, questions, deadline)

    missing = CaptureJev("missing")
    result = semantic_audit.evaluate_source_fact_coverage(
        missing, source, facts, time.monotonic() + 5, target_id="m7")
    assert result["evaluated"] and result["status"] != "PASS"
    assert result["findings"] == [{"code": "source_fact_coverage_missing"}]
    assert missing.state["target"]["text"] == source
    assert missing.state["context"][0]["text"] == facts[0]["statement"]
    assert "cover every important event" in missing.questions[
        "source_fact_coverage"]["instructions"]

    ambiguous = CaptureJev("ambiguous")
    result = semantic_audit.evaluate_source_fact_coverage(
        ambiguous, source, facts, time.monotonic() + 5)
    assert result["status"] != "PASS"
    assert result["findings"] == [{"code": "source_fact_coverage_ambiguous"}]

    complete = CaptureJev("complete")
    result = semantic_audit.evaluate_source_fact_coverage(
        complete, source, facts, time.monotonic() + 5)
    assert result["status"] == "PASS" and result["findings"] == []

    strict = CaptureJev("complete")
    result = semantic_audit.evaluate_source_fact_coverage(
        strict, source, facts, time.monotonic() + 5, match_threshold=0.95)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["findings"] == [{"code": "source_fact_coverage_low_confidence"}]

    class GuardJev:
        def evaluate(self, _state, _questions, _deadline):
            raise RuntimeGuardError("off")

    with pytest.raises(RuntimeGuardError):
        semantic_audit.evaluate_source_fact_coverage(
            GuardJev(), source, facts, time.monotonic() + 5)

    unknown = semantic_audit.evaluate_source_fact_coverage(
        None, source, facts, time.monotonic() + 5)
    assert not unknown["evaluated"]
    assert unknown["status"] == "INCOMPLETE"
    assert unknown["failure_reason"] == "resource"
    assert unknown["findings"] == [{"code": "source_fact_coverage_unevaluated"}]

    import semantic
    db = _seeded(tmp_path)
    try:
        semantic.run_due(db, _cfg("enforce"), {"errors": []},
                         time.monotonic() + 300, jev_client=CaptureJev("missing"),
                         llm_fn=_llm)
        audits = db.artifacts("semantic_audit", message_id=1)
        assert audits and json.loads(audits[-1]["meta"])["audit_status"] == "NEEDS_REVIEW"
        assert not db.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()
