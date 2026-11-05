"""Current raw responses require schema fields; normalized checkpoints stay compatible."""
import extract_llm_engine
import extract_llm_runner
import json

import pytest

import extract_llm as llm
import extract_schema
import extract_validation
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.mark.parametrize("first", [{}, {"summary": "合成の報告"}])
def test_missing_required_first_response_uses_the_existing_repair(monkeypatch, first):
    calls = []

    def model(*args, **kwargs):
        calls.append(kwargs.get("kind", "first"))
        return first if len(calls) == 1 else {"symptoms": []}

    monkeypatch.setattr(llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    meta = {}
    assert llm.llm_extract("本人に発熱はありません。", meta_out=meta) == {"symptoms": []}
    assert calls == ["first", "repair"]
    assert meta["repair_issues"]["structure"] == 1


def test_missing_required_repair_response_does_not_become_a_success(monkeypatch):
    responses = iter(({"symptoms": None}, {"summary": "合成の修復結果"}))
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k: next(responses))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    meta = {}
    assert llm.llm_extract("本人に発熱はありません。", meta_out=meta) is None
    assert meta["failure_reason"] == "validation"


def test_missing_required_repair_cannot_replace_a_valid_first_candidate(monkeypatch):
    responses = iter(({"symptoms": []}, {"vitals": {"hr": 80}}))
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k: next(responses))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    body = "本人の脈拍は80です。" + "合成の経過です。" * 50
    assert llm.llm_extract(body) == {"symptoms": []}


def test_explicit_empty_required_section_remains_valid(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k:
                        calls.append(1) or {"symptoms": []})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    assert llm.llm_extract("合成の報告です。") == {"symptoms": []}
    assert calls == [1]


def test_required_fields_follow_the_current_schema(monkeypatch):
    schema = {**extract_schema.SCHEMA, "schema": {
        **extract_schema.SCHEMA["schema"], "required": ["symptoms", "summary"]}}
    monkeypatch.setattr(extract_schema, "SCHEMA", schema)
    responses = iter(({"symptoms": []}, {"symptoms": [], "summary": "合成の報告"}))
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k: next(responses))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    assert llm.llm_extract("合成の報告です。") == {"symptoms": [], "summary": "合成の報告"}


@pytest.mark.parametrize("status", [None, "pending", "started"])
def test_normalized_checkpoint_does_not_require_raw_response_fields(monkeypatch, status):
    cached = {"summary": "合成の保存済み要約"}
    if status:
        cached["_quality_status"] = status
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k: pytest.fail("paid first call repeated"))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    assert llm.llm_extract("合成の報告です。", chunks_in={0: cached}) == {
        "summary": "合成の保存済み要約"}
    assert extract_validation.validate({}) == {}
    assert extract_validation.validate({"summary": "合成の部分payload"}) == {
        "summary": "合成の部分payload"}


def test_missing_required_batch_item_retries_only_that_message(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    calls = []

    def model(*args, **kwargs):
        calls.append(kwargs.get("schema"))
        if kwargs.get("schema") is extract_schema.SCHEMA_BATCH:
            return {"items": [{"i": 0}, {"i": 1, "symptoms": []}]}
        return {"symptoms": []}

    monkeypatch.setattr(llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', llm._llm_call)
    try:
        ledger.save_messages([_message(mid=mid, body=f"合成の報告{mid}です。") for mid in (1, 2)])
        result = llm.run_pending(ledger, budget_s=300, batch_k=2)
        assert result["done"] == 2
        assert calls == [extract_schema.SCHEMA_BATCH, None]
        assert all(json.loads(row["content"])["symptoms"] == []
                   for row in ledger.artifacts(llm.KIND))
    finally:
        ledger.close()


def test_version_ten_empty_result_stays_stored_and_is_pending_for_current_contract(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(body="合成の報告です。")])
        artifact_id = ledger.artifact_add(llm.KIND, "{}", project_id=1, message_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(ledger), "extract_version": 10})
        assert not llm._current(ledger, 1, _hash(ledger))
        assert ledger.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(ledger.db)).fetchone()
        assert ledger.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0] == "{}"
    finally:
        ledger.close()
