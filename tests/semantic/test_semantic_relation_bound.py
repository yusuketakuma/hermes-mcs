"""All-pairs relation reconciliation is bounded by the run deadline.

Normalised facts from every chunk are compared pairwise, so N facts that
share an entity yield N*(N-1)/2 relations.  Past the deadline the scan
stops, the skipped pairs are counted, and neither extraction nor repair
reports the cut set as a finished document."""
import json
import time
from types import SimpleNamespace

import pytest

import semantic_extraction as extraction
import semantic_facts as sf
import semantic_relations as sr

PRESENCE = dict({c: "none" for c in sf.MANDATORY_CATEGORIES},
                symptom="multiple")


def _body(n):
    return "".join(f"合成症状記録{i:02d}番あり。" for i in range(n))


def _items(n, status="reported"):
    return [{"statement": f"合成症状記録{i:02d}番", "kind": "symptom_state",
             "subject_role": "patient", "polarity": "affirmed",
             "workflow_status": status, "importance": "T2",
             "evidence_quote": f"合成症状記録{i:02d}番あり"}
            for i in range(n)]


def _member(n):
    return {"project_id": 1, "message_id": "m1", "revision": "r1",
            "body_original": _body(n), "body_state": "full",
            "posted_at": "2026-09-20",
            "sender": {"id": "s1", "type": "staff", "profession": "dr"}}


def _llm(n, status="reported"):
    return lambda _prompt: json.dumps(
        {"facts": _items(n, status), "category_presence": PRESENCE},
        ensure_ascii=False)


def _expired(monkeypatch):
    """Only the relation scan sees a clock past the deadline."""
    monkeypatch.setattr(sr, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + 10_000), raising=False)


@pytest.mark.parametrize("n", [16, 32, 64])
def test_relations_grow_quadratically_with_facts(n):
    result = extraction.extract_facts_v2(_llm(n), _member(n))
    assert len(result["doc"]["facts"]) == n
    assert len(result["doc"]["relations"]) == n * (n - 1) // 2
    assert result["extraction_complete"]
    assert result.get("relation_pairs_skipped", 0) == 0


@pytest.mark.parametrize("n", [16, 64])
def test_extraction_past_deadline_is_incomplete_and_counts_skips(
        monkeypatch, n):
    _expired(monkeypatch)
    result = extraction.extract_facts_v2(
        _llm(n), _member(n), time.monotonic() + 300)
    sf.validate_facts_doc(result["doc"])
    assert not result["extraction_complete"] and not result["complete"]
    assert result.get("relation_pairs_skipped") == n * (n - 1) // 2
    assert result["doc"]["relations"] == []
    assert result["failure_reason"] == "deadline"
    coverage = result["doc"]["coverage"]
    assert coverage["status"] == "incomplete"
    assert "relations_incomplete" in coverage["limitations"]


def test_scan_stops_at_the_first_clock_read_past_the_deadline(monkeypatch):
    facts = extraction.extract_facts_v2(_llm(16), _member(16))["doc"]["facts"]
    reads = iter([0.0, 99.0])
    monkeypatch.setattr(sr, "time", SimpleNamespace(
        monotonic=lambda: next(reads)), raising=False)
    out = sr.reconcile_facts([], facts, deadline=1.0)
    assert out.get("pairs_skipped") == 120 - 64
    assert len(out["relations"]) == 64
    assert out["fingerprint"] == sr.relation_set_fingerprint(out["relations"])


def test_repair_past_deadline_keeps_the_stored_doc(monkeypatch):
    n = 16
    stored = extraction.extract_facts_v2(_llm(n), _member(n))["doc"]
    before = json.dumps(stored, ensure_ascii=False, sort_keys=True)
    rejected = stored["facts"][0]["fact_id"]
    _expired(monkeypatch)
    out = extraction.repair_facts_v2(
        _llm(n, status="performed"), _member(n), stored,
        {rejected: "audit: synthetic"}, time.monotonic() + 300)
    assert not out["repaired"] and out["doc"] is stored
    assert out.get("relation_pairs_skipped", 0) > 0
    assert out["owner_chunk_ids"]
    assert json.dumps(stored, ensure_ascii=False, sort_keys=True) == before
