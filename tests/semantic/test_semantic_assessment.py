"""Event-level medication detail contracts."""
import time

import pytest

import semantic_assessment as assessment
import semantic_jev as jev
from semantic_runtime import RuntimeGuardError
from test_mcs_semantic import _ledger


SCFG = {"model": jev.JEV_MODEL, "match_threshold": 0.7,
        "nomatch_threshold": 0.3, "calibration_version": "test-v1"}


def _bundle_and_facts():
    body = ("アムロジピンを開始します。"
            "ワルファリンは中止します。")
    members = [{
        "project_id": 1, "message_id": 10, "parent_id": None,
        "revision": "rev-10", "posted_at": "2026-09-20T00:00:00+09:00",
        "sender": {"id": 1, "type": "user"}, "role": "target",
        "body_original": body,
    }, {
        "project_id": 1, "message_id": 11, "parent_id": 10,
        "revision": "rev-11", "posted_at": "2026-09-20T00:01:00+09:00",
        "sender": {"id": 2, "type": "clinician"}, "role": "context",
        "body_original": "用量の確認をお願いします。",
    }]

    def fact(fact_id, statement, quote, drug, status, polarity):
        start = body.index(quote)
        return {
            "fact_id": fact_id, "kind": "medication_event",
            "statement": statement, "drug_ref": drug,
            "status": status, "polarity": polarity,
            "time_text": "本日",
            "_evidence": {
                "evidence_id": f"ev-{fact_id}", "message_id": 10,
                "revision_id": "rev-10", "start_codepoint": start,
                "end_codepoint": start + len(quote), "quote": quote,
            },
        }

    facts = [
        fact("f-start", "アムロジピンを開始するイベント",
             "アムロジピンを開始", "amlodipine", "planned", "affirmed"),
        fact("f-stop", "ワルファリンを中止するイベント",
             "ワルファリンは中止", "warfarin", "cancelled", "negated"),
    ]
    return {
        "project_id": 1, "root_id": 10, "source_fingerprint": "source-v1",
        "members": members,
    }, facts, body


class _EventJev:
    def __init__(self, confidence=0.9, fail_dimension=None):
        self.confidence = confidence
        self.fail_dimension = fail_dimension
        self.calls = []

    def evaluate(self, state, questions, _deadline):
        dimension = next(iter(questions))
        self.calls.append((state, dimension))
        if dimension == self.fail_dimension:
            raise jev.JevError("transport", "synthetic", retryable=True)
        fact_id = state["target"]["fact_id"]
        choices = {
            "f-start": {"change_kind": "start", "status": "planned",
                        "polarity": "affirmed", "speaker_basis": "self_report"},
            "f-stop": {"change_kind": "stop", "status": "cancelled",
                       "polarity": "negated", "speaker_basis": "clinician_report"},
        }
        choice = choices[fact_id][dimension]
        confidence = (0.6 if fact_id == "f-stop"
                      and dimension == "polarity" else self.confidence)
        return {"answers": {dimension: {
            "choice": choice, "confidence": confidence}}}


def test_each_medication_event_has_own_detail_target_and_review_finding(tmp_path):
    db = _ledger(tmp_path)
    bundle, facts, source = _bundle_and_facts()
    client = _EventJev()
    try:
        result = assessment.evaluate_medication_events(
            db, bundle, 10, facts, client, SCFG, time.monotonic() + 30)
        assert result["complete"]
        assert result["details"]["f-start"]["status"] == "planned"
        assert result["details"]["f-start"]["polarity"] == "affirmed"
        assert result["details"]["f-stop"]["status"] == "cancelled"
        assert result["details"]["f-stop"]["polarity"] == "negated"
        assert {finding["fact_id"] for finding in result["findings"]} \
            == {"f-stop"}
        assert result["findings"][0]["code"] == \
            "medication_detail_low_confidence"
        assert len(client.calls) == len(jev.MED_DETAIL_QUESTIONS) * 2
        for state, _dimension in client.calls:
            assert state["target"]["fact_id"] in {"f-start", "f-stop"}
            assert state["target"]["drug"] in {"amlodipine", "warfarin"}
            assert state["target"]["time_text"] == "本日"
            assert state["target"]["evidence"]["text"] in source
            assert any(item["text"] == source
                       for item in state["context"])
            assert any(item["role"] == "evidence_quote"
                       and item["text"] == state["target"]["evidence"]["text"]
                       for item in state["context"])
    finally:
        db.close()


def test_failed_dimension_resumes_without_repeating_completed_detail(tmp_path):
    db = _ledger(tmp_path)
    bundle, facts, _source = _bundle_and_facts()
    one_fact = facts[:1]
    first_client = _EventJev(fail_dimension="status")
    try:
        first = assessment.evaluate_medication_events(
            db, bundle, 10, one_fact, first_client, SCFG,
            time.monotonic() + 30)
        assert not first["complete"]
        assert first["failure_reason"] == "technical"
        assert first["details"]["f-start"] == {"change_kind": "start"}
        assert [dimension for _state, dimension in first_client.calls] \
            == ["change_kind", "status"]
        assert len(db.artifacts(assessment.KIND_ASSESS,
                                project_id=1, message_id=10)) == 1

        second_client = _EventJev()
        second = assessment.evaluate_medication_events(
            db, bundle, 10, one_fact, second_client, SCFG,
            time.monotonic() + 30)
        assert second["complete"]
        assert [dimension for _state, dimension in second_client.calls] \
            == ["status", "polarity", "speaker_basis"]
        assert len(db.artifacts(assessment.KIND_ASSESS,
                                project_id=1, message_id=10)) \
            == len(jev.MED_DETAIL_QUESTIONS)

        class GuardClient:
            def evaluate(self, _state, _questions, _deadline):
                raise RuntimeGuardError("stale")

        guarded_bundle = {**bundle, "source_fingerprint": "source-v2"}
        with pytest.raises(RuntimeGuardError):
            assessment.evaluate_medication_events(
                db, guarded_bundle, 10, one_fact, GuardClient(), SCFG,
                time.monotonic() + 30)
    finally:
        db.close()


def test_confident_detail_disagreement_blocks_audit_without_rewriting_fact(tmp_path):
    import semantic
    db = _ledger(tmp_path)
    bundle, facts, _ = _bundle_and_facts()
    facts[0]["status"] = "execution_reported"
    try:
        result = assessment.evaluate_medication_events(
            db, bundle, 10, facts[:1], _EventJev(), SCFG, time.monotonic() + 30)
        assert result["complete"]
        assert facts[0]["status"] == "execution_reported"
        assert any(f["code"] == "medication_detail_mismatch"
                   and f["dimension"] == "status" for f in result["findings"])
        assert semantic.audit_status_for(result["findings"], [], True, False) == "NEEDS_REVIEW"
    finally:
        db.close()


def test_invalid_confidence_is_unassessed_without_persisting_detail(tmp_path):
    db = _ledger(tmp_path)
    bundle, facts, _ = _bundle_and_facts()
    try:
        result = assessment.evaluate_medication_events(
            db, bundle, 10, facts, _EventJev(confidence=10**1000),
            SCFG, time.monotonic() + 30)
        assert not result['complete'] and result['failure_reason'] == 'technical'
        assert db.artifacts(assessment.KIND_ASSESS) == []
    finally:
        db.close()
