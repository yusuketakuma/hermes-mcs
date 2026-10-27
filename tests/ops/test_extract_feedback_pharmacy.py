"""Synthetic pharmacy feedback preserves human confirmation, source pins and one retry."""
import json
import uuid

import pytest

from adapters.common import envelopes, forms
import extract_llm
from extract_testkit import _hash, _ledger, _message
import mcs_requests
from inference_version import CURRENT_VERSION, VERSION_KEY


FIELDS = ("patient_context", "labs", "events")
BODY = "服薬管理: 家族が薬を保管しています。咳あり。"


def seed(db):
    db.save_messages([_message(body=BODY)])
    return db.artifact_add(
        "extract_llm", json.dumps({"summary": "咳あり"}),
        project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db), "extract_version": extract_llm.EXTRACT_VERSION})


def command(aid, field):
    return envelopes.extract_feedback(
        "synthetic-operator", {"project_id": 1, "extract_ref": {
            "message_id": 1, "artifact_id": aid}}, field, "合成の確認結果")


@pytest.mark.parametrize("field", FIELDS)
def test_new_feedback_field_end_to_end_reextracts_once(tmp_path, monkeypatch, field):
    db = _ledger(tmp_path)
    try:
        aid = seed(db)
        options = dict(forms.modal_fields("report")[0]["options"])
        assert field in options
        assert forms.feedback_attrs({"field": field, "note": "合成の確認結果"}) == (
            field, "合成の確認結果")
        req = command(aid, field)
        first = mcs_requests.apply_command(db, req)
        assert first["outcome"] == "applied"
        assert mcs_requests.apply_command(db, req) == first
        stored = db.db.execute(
            "SELECT content FROM artifacts WHERE kind='extract_feedback_v1'").fetchall()
        assert len(stored) == 1
        report = json.loads(stored[0][0])
        assert (report["field"], report["artifact_id"], report["hash"]) == (
            field, aid, _hash(db))
        calls = []
        monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **kwargs:
                            calls.append(body) or {"summary": "服薬管理の報告"})
        assert extract_llm.run_pending(db, limit=5, budget_s=30)["done"] == 1
        assert calls == [BODY]
        result = db.artifacts("extract_llm", message_id=1)
        assert len(result) == 1 and result[0]["artifact_id"] != aid
        assert json.loads(result[0]["meta"])["human_fix"]["applied"] is True
        assert extract_llm.run_pending(db, limit=5, budget_s=30)["selected"] == 0
        assert calls == [BODY]
        stale = {**req, "command_id": str(uuid.uuid4())}
        assert mcs_requests.apply_command(db, stale)["error"] == "extraction_changed"
    finally:
        db.close()


@pytest.mark.parametrize("field", FIELDS)
def test_new_fields_still_require_human_confirmation(tmp_path, field):
    db = _ledger(tmp_path)
    try:
        req = command(seed(db), field)
        req["human_confirmed"] = False
        assert mcs_requests.apply_command(db, req)["outcome"] == "rejected"
        assert db.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_feedback_v1'").fetchone()[0] == 0
    finally:
        db.close()


def test_unknown_feedback_field_remains_rejected(tmp_path):
    db = _ledger(tmp_path)
    try:
        req = command(seed(db), "unknown_field")
        assert mcs_requests.apply_command(db, req)["error"] == "bad_field"
        assert db.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_feedback_v1'").fetchone()[0] == 0
    finally:
        db.close()
