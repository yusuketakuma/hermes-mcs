"""Synthetic version-nine upgrade boundaries for source interpretation changes."""
import json
import time

import pytest

import extract_llm as llm
import inference_dependency_loss
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, LLM_EXTRACT_VERSION, VERSION_KEY
from semantic_projection import PROJECTION_VERSION


_BODY = "本人に息切れはありません。家族には軽い発熱があります。"
_PRIOR = {"summary": "合成の報告", "symptoms": [
    {"text": "息切れ", "negated": True, "status": "ongoing", "subject": "patient",
     "evidence": "本人に息切れはありません。"},
    {"text": "発熱", "negated": False, "status": "ongoing", "subject": "family",
     "evidence": "家族には軽い発熱があります。"},
]}


@pytest.fixture
def source(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_rerender_cards", lambda *args: None)
    ledger.save_messages([_message(body=_BODY)])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, 1, 1)
    prior = ledger.artifact_add("extract_llm", json.dumps(_PRIOR, ensure_ascii=False),
        project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(ledger),
        "source_binding": row["_source_binding"], "extract_version": 9})
    try:
        yield ledger, row, prior
    finally:
        ledger.close()


def _pending(ledger):
    return bool(ledger.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(ledger.db)).fetchone())


def test_same_generation_version_nine_requeues_and_ignores_old_retry_budget(source):
    ledger, row, prior = source
    assert llm.EXTRACT_VERSION == LLM_EXTRACT_VERSION
    assert _pending(ledger)
    # A terminal retry in the previous extraction contract must not exhaust the new contract.
    ledger.artifact_add("extract_llm", '{"_error":true}', project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": row["content_hash"],
              "extract_version": 9, "error": True, "attempts": 5, "next_try": time.time() + 3600})
    ledger.artifact_add("extract_qc", '{"qc":"done","items":[{"verdict":"NO_MATCH"}]}',
        project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": row["content_hash"],
        "extract_version": 9, "source_artifact_id": prior})
    now = time.time()
    selected = ledger.db.execute(llm._selection_sql(ledger.db, "DESC", "", None),
        (llm.KIND, LLM_EXTRACT_VERSION, None, 1, 0, now, llm.KIND, LLM_EXTRACT_VERSION, now, 10)).fetchall()
    assert len(selected) == 1 and selected[0]["message_id"] == 1
    assert selected[0]["attempts"] == 0 and selected[0]["qc_src"] is None


def test_version_nine_checkpoint_is_readable_only_by_the_old_contract(source, monkeypatch):
    ledger, row, _ = source
    assert llm.EXTRACT_VERSION == LLM_EXTRACT_VERSION
    with monkeypatch.context() as old:
        old.setattr(llm, "EXTRACT_VERSION", 9)
        assert llm._persist_chunks(ledger, row, {0: {"summary": "合成の途中経過"}})
        _splits, saved = llm._saved_chunk_state(ledger.db, row)
        assert saved[0]["summary"] == "合成の途中経過"
    _splits, saved = llm._saved_chunk_state(ledger.db, row)
    assert saved == {}
    old_rows = ledger.artifacts("extract_llm_chunk", message_id=1)
    assert len(old_rows) == 1 and json.loads(old_rows[0]["meta"])["ver"] == 9


def test_held_prior_is_rechecked_under_new_target_without_losing_evidence(source, monkeypatch):
    ledger, row, prior = source
    assert llm.EXTRACT_VERSION == LLM_EXTRACT_VERSION
    original = dict(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (prior,)).fetchone())
    weak = '{"summary":"合成の候補","symptoms":[]}'
    with monkeypatch.context() as old:
        old.setattr(llm, "EXTRACT_VERSION", 9)
        assert llm._replace_current(ledger, row, weak) is False
        first = json.loads(ledger.artifacts(llm.UPGRADE_KIND)[0]["content"])
        assert first["state"] == "held_prior" and first["target_version"] == 9
        assert not _pending(ledger)
    assert _pending(ledger)  # Old target's hold is not a receipt for the current target.
    assert llm._replace_current(ledger, row, weak) is False
    assert dict(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (prior,)).fetchone()) == original
    receipts = [json.loads(item["content"]) for item in ledger.artifacts(llm.UPGRADE_KIND)]
    assert {item["target_version"] for item in receipts} == {9, LLM_EXTRACT_VERSION}
    newest, = [item for item in receipts if item["target_version"] == LLM_EXTRACT_VERSION]
    assert newest["state"] == "held_prior" and newest["reason"] == "protected_loss"
    assert newest["prior_artifact_id"] == prior and newest["hash"] == row["content_hash"]
    assert newest["contract"] == llm._upgrade_contract()
    assert not _pending(ledger)


def test_current_canonical_projection_has_priority_over_version_nine_retry(source):
    ledger, row, prior = source
    assert llm.EXTRACT_VERSION == LLM_EXTRACT_VERSION and _pending(ledger)
    ledger.artifact_add("semantic_facts_v4", '{"facts":[]}', project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": row["content_hash"], "engine_version": 4,
              "projection_version": PROJECTION_VERSION})
    assert not _pending(ledger)
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (prior,)).fetchone()
