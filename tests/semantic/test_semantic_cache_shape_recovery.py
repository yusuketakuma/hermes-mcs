"""Current-generation JSON objects with broken document containers must be regenerated."""
import copy
import hashlib
import json
import time

import pytest

import semantic
import semantic_drain as drain
import semantic_extraction as extraction
from semantic_policy import KIND_FACTS_V2
from semantic_testkit import _ledger, _message, _PassJev, v2_doc, v2_fact


@pytest.mark.parametrize("field,bad", [
    ("valid", None),
    ("normalize", None),
    ("coverage", None), ("facts", 7), ("evidence", {}), ("source", []),
    ("evidence", [{"evidence_id": ["bad"]}]),
    ("facts", [v2_fact("f1", evidence_ids=None)]),
    ("facts", [v2_fact("f1", evidence_ids=[{"bad": "ref"}])]),
    ("facts", [v2_fact(["bad"])]),
    ("source.message_id", "999"),
    ("source.revision", "stale"),
    ("source.content_hash", "stale"),
    ("source.source_fingerprint", "stale"),
    ("source.body_codepoints", 999),
    ("coverage.open_obligation_ids", None),
    ("coverage.open_obligation_ids", ["missing"]),
    ("relations", [{"type": "CONTRADICTION", "left_fact_id": ["bad"]}]),
    ("relations", [{"relation_id": "r1", "type": "CONTRADICTION",
                    "left_fact_id": "missing1", "right_fact_id": "missing2",
                    "evidence_ids": []}]),
])
def test_malformed_current_cache_is_reextracted_without_losing_history(tmp_path, monkeypatch, field, bad):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="SYNTH")])
        revision = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        member = {"message_id": 1, "project_id": 1, "revision": revision,
                  "body_original": "SYNTH", "body_state": "full"}
        healthy = v2_doc([])
        healthy["source"].update(message_id="1", revision=revision,
                                 content_hash=hashlib.sha256(b"SYNTH").hexdigest(),
                                 body_codepoints=5, source_fingerprint="fp")
        if field == "valid":
            healthy["extension"] = {"synthetic": "preserve"}
        if field == "normalize":
            healthy["atoms"] = [{"atom_id": "a1", "kind": "clause", "start": 0,
                                 "end": 5, "text_hash": healthy["source"]["content_hash"]}]
            healthy["chunks"] = [{"chunk_id": "c1", "core_atom_ids": ["a1"]}]
            healthy["evidence"] = [{"evidence_id": "e1", "message_id": "1",
                                    "revision": revision, "atom_id": "a1",
                                    "start": 0, "end": 5, "quote": "SYNTH"}]
            healthy["facts"] = [v2_fact("f1", statement=7, evidence_ids=["e1"])]
        broken = copy.deepcopy(healthy)
        if "." in field:
            container, key = field.split(".")
            broken[container][key] = bad
        elif field not in ("valid", "normalize"):
            broken[field] = bad
        old = ledger.artifact_add(KIND_FACTS_V2, json.dumps(broken), project_id=1, message_id=1,
                                   meta={"fingerprint": "fp", "coverage_status": "complete"})
        calls = []

        def regenerate(*args, **kwargs):
            calls.append(1)
            return {"doc": healthy, "extraction_complete": True}

        monkeypatch.setattr(extraction, "extract_facts_v2", regenerate)
        monkeypatch.setattr(semantic, "llm_model", lambda *args: "SYNTH-model")
        result = drain._fact_stage(ledger, {"fact_source": "canonical", "match_threshold": 0.7},
                                  member, 1, 1, "fp", "policy", _PassJev(),
                                  lambda *args: pytest.fail("unexpected model call"), time.monotonic() + 30)
        assert result["outcome"] is None
        assert calls == ([] if field in ("valid", "normalize") else [1])
        rows = ledger.artifacts(KIND_FACTS_V2, message_id=1)
        assert rows[0]["artifact_id"] == old
        assert json.loads(rows[-1]["content"]) == healthy
        if field in ("valid", "normalize"):
            assert len(rows) == 1
        if field == "valid":
            assert result["v2_doc"] == healthy
        if field == "normalize":
            assert result["facts"][0]["statement"] == "7"
    finally:
        ledger.close()
