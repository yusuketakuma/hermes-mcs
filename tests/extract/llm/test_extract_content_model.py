"""Artifact content names the model that actually served the row; synthetic only."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import json

import extract_llm
import cloud_inference
from test_extraction_review import _message, db

__all__ = ["db"]

_CFG = {"local_llm": {"model": "synthetic-configured-model"}}


def test_success_content_records_the_configured_model(db, monkeypatch):
    db.save_messages([_message(body="独居です。")])
    monkeypatch.setattr(extract_llm, "load_config", lambda: _CFG)
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: _CFG)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda prompt, **kw: {
        "symptoms": [], "patient_context": [{"category": "living", "text": "独居です",
                             "evidence": "独居です", "subject": "patient"}]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.run_pending(db, limit=1, budget_s=30)["done"] == 1
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='extract_llm'").fetchone()
    assert json.loads(row["content"])["_model"] == "synthetic-configured-model"


def test_error_row_records_the_pinned_model(db):
    db.save_messages([_message(body="独居です。")])
    r = dict(db.db.execute("SELECT * FROM messages").fetchone())
    r["_target"] = ("", "synthetic-configured-model")
    with db.db:
        extract_llm._fail_tx(db, r, 0)
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='extract_llm'").fetchone()
    assert json.loads(row["content"])["_model"] == "synthetic-configured-model"
