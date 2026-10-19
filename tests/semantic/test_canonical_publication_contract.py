"""Synthetic source-binding, bounded retry and non-PASS canonical acceptance oracles."""
import copy
import hashlib
import json

import pytest

import semantic_audit
import semantic_facts
from semantic_testkit import v2_doc


def cached():
    member = {"message_id": 1, "revision": "r", "body_state": "full", "body_original": "合成の観測"}
    source = {"message_id": "1", "revision": "r", "content_hash": hashlib.sha256(member["body_original"].encode()).hexdigest(),
              "body_codepoints": len(member["body_original"]), "source_fingerprint": "fp", "content_quality": "full", "attachments_complete": True}
    doc = v2_doc([], source=source)
    return member, {"content": doc, "meta": {}}


@pytest.mark.parametrize("field,value", [("message_id", "2"), ("revision", "other"),
    ("content_hash", "f" * 64), ("body_codepoints", 1), ("source_fingerprint", "old"),
    ("content_quality", "partial"), ("attachments_complete", False)])
def test_every_original_source_binding_dimension_blocks_reuse(field, value):
    member, previous = cached()
    previous["content"]["source"][field] = value
    before = copy.deepcopy(previous)
    selected, spent, retry = semantic_facts.cached_facts_plan(previous, member, 1, "fp", "canonical")
    assert selected is None and spent is False and retry is False
    assert previous == before


@pytest.mark.parametrize("mode,meta,selected,spent,retry", [
    ("canonical", {}, True, False, False),
    ("canonical", {"coverage_status": "incomplete"}, True, False, True),
    ("canonical", {"coverage_status": "incomplete", "coverage_retry": True}, True, False, False),
    ("canonical", {"hint_rebuild": "skipped:incomplete", "coverage_retry": True}, False, True, False),
    ("shadow", {"coverage_status": "incomplete"}, True, False, False),
])
def test_cached_retry_budget_and_input_observation_remain_local(mode, meta, selected, spent, retry):
    member, previous = cached()
    previous["meta"] = meta
    previous["content"]["synthetic_extension"] = {"kept": True}
    before = json.dumps(previous, sort_keys=True)
    actual, used, resume = semantic_facts.cached_facts_plan(previous, member, 1, "fp", mode)
    assert (actual is not None, used, resume) == (selected, spent, retry)
    if actual is not None:
        assert actual["content"]["synthetic_extension"] == {"kept": True}
    assert json.dumps(previous, sort_keys=True) == before


@pytest.mark.parametrize("evaluated,status,coverage,expected", [
    (False, "PASS", "complete", "wait"),
    (True, "PASS", "complete", "pass"),
    (True, "PASS", "incomplete", "coverage_incomplete"),
    (True, "NEEDS_REVIEW", "complete", "review"),
])
def test_open_coverage_or_unevaluated_audit_never_becomes_pass(evaluated, status, coverage, expected):
    assert semantic_audit.canonical_audit_disposition({"evaluated": evaluated, "status": status},
        {"coverage": {"status": coverage}}) == expected


@pytest.mark.parametrize("evaluated,bound,expected", [(False, True, False), (True, False, False), (True, True, True)])
def test_cached_audit_needs_completed_current_document(evaluated, bound, expected):
    audit = {"meta": {"doc_hash": "current" if bound else "old"},
             "content": {"evaluated": evaluated, "status": "PASS"}}
    actual = semantic_audit.cached_fact_audit(audit, {"facts": [], "evidence": []}, "current")
    assert (actual is not None) is expected


@pytest.mark.parametrize("attempt,findings,allowed", [
    (0, [{"fact": "f1", "code": "fact_unverified"}], True),
    (1, [{"fact": "f1", "code": "fact_unverified"}], False),
    (0, [{"code": "source_fact_coverage_missing"}], True),
    (0, [], False),
])
def test_fact_repair_candidate_remains_first_attempt_and_targeted(attempt, findings, allowed):
    rejected, coverage, candidate = semantic_audit.fact_repair_plan(findings, attempt)
    assert candidate is allowed
    assert rejected == ({"f1": "fact_unverified"} if findings and findings[0].get("fact") else {})
    assert coverage is bool(findings and findings[0].get("code") == "source_fact_coverage_missing")


@pytest.mark.parametrize("kind,stage,expected", [
    ("not_sent", "anything", False), ("budget", "llm_attempt", False),
    ("budget", "llm_next_call", False), ("budget", "llm", False),
    ("stale", "llm_result", True), ("budget", "jev_result", True),
])
def test_repair_reservation_refund_requires_pre_dispatch_evidence(kind, stage, expected):
    import semantic_runtime as runtime
    cls = {"not_sent": runtime.LLMNotSent, "budget": runtime.RuntimeBudget, "stale": runtime.RuntimeStale}[kind]
    assert runtime.llm_request_was_dispatched(cls(stage)) is expected
