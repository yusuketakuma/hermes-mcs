"""Invented sealed-content completeness regression, fixed 7e43547f only."""
import copy
import json

import extract_llm
import extract_quality
import extract_validation
import patient_context_refresh
from extract_testkit import _ledger, _message
from test_completeness_quality import OBSERVATIONS, PREFIX


def test_sealed_normal_final_rejects_content_only_vital_loss(tmp_path, monkeypatch):
    body = PREFIX + "本人の血圧120/80です。" + "完全合成の通常経過を記録します。" * 24
    assert 300 <= len(body) < extract_llm._CHUNK_SIZE
    document = {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}
    hints = extract_llm._rule_hints({"body_text": body, "posted_at": "2026-09-19"})
    validated = extract_validation.validate(document, body)
    assert not validated.get("_items_dropped") and not validated.get("_evidence_dropped")
    assert not extract_quality.is_thin(validated, body)
    assert not extract_quality.completeness_issues(validated, hints, body)
    calls = []
    def local_stub(*args, **kwargs):
        calls.append(kwargs.get("kind", "first"))
        extract_llm._CALL_NOTES.provider_response = "local"
        return copy.deepcopy(document)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm, "_llm_call", local_stub)
    db = _ledger(tmp_path)
    try:
        db.ensure_patient(1)
        db.save_messages([_message(mid=1, body=body)], project_id=1)
        result = extract_llm.run_pending(db, limit=1, budget_s=300)
        assert result["done"] == 1, result
        final, = db.artifacts(extract_llm.KIND, message_id=1)
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        meta = json.loads(final["meta"])
        content = json.loads(final["content"])
        assert "__mcs_internal_proof_v1" not in content
        assert content["__mcs_leaf_claims_v1"] == meta["integrity"]["completeness"]
        assert meta["provider_provenance"]["complete"] is True
        assert meta["provider_provenance"]["providers"] == ["local"]
        assert meta["provider_provenance"]["result_hash"] == extract_llm._provider_result_hash(final["content"])
        assert not db.artifacts("extract_llm_chunk", message_id=1)
        before = extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)
        before_ready, _ = patient_context_refresh._raw_extraction_current(db.db, source, {})
        assert before["state"] == "complete", before
        assert before_ready is True
        call_count = len(calls)
        # Only the stored clinical vitals key is removed. All source, seal,
        # final metadata and its original provider result hash stay unchanged.
        removed = content.pop("vitals")
        assert removed == {"sbp": 120.0, "dbp": 80.0}
        damaged = json.dumps(content, ensure_ascii=False)
        remaining = extract_validation.validate(content, body)
        assert remaining is not None and not remaining.get("_items_dropped") and not remaining.get("_evidence_dropped")
        assert not extract_quality.is_thin(remaining, body)
        gaps = extract_quality.completeness_issues(remaining, hints, body)
        assert gaps and extract_quality.facts(remaining) < extract_quality.facts(validated)
        assert extract_llm._provider_result_hash(damaged) != meta["provider_provenance"]["result_hash"]
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (damaged, final["artifact_id"]))
        assert db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (final["artifact_id"],)).fetchone()[0] == final["meta"]
        after = extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)
        after_ready, _ = patient_context_refresh._raw_extraction_current(db.db, source, {})
        assert len(calls) == call_count
        print(json.dumps({"body_chars": len(body), "before": before["state"], "before_ready": before_ready,
                          "after": after["state"], "after_ready": after_ready, "missing_hint_count": len(gaps),
                          "meta_unchanged": True, "provider_hash_matches": False,
                          "additional_model_calls": len(calls) - call_count}, sort_keys=True))
        assert (after["state"], after_ready) == ("attention", False), (after, after_ready)
    finally:
        db.close()
