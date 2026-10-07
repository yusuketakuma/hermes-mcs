"""Clinical detail preserves independent registrations, attribute sources and generations."""
import json

import pytest

import read_model
import structured_view
from project_metadata import ARTIFACT_KIND, normalize_rows
from project_metadata import clinical_definition_fingerprint
from project_metadata_view import get_project_metadata
from test_patient_context_view import context_db
from test_read_model import _artifact, _hash_of

__all__ = ["context_db"]
MED = {"begin_date": "2026-10-01", "end_date": None,
       "medicine_informations": [{"id": 66, "name": "架空登録薬"}]}
DEFINITION = {"lab_test_item": {"id": 55, "name": "架空観測項目", "unit": "架空単位"}}
VALUE = {"scalar": 23, "observation_issued_at": "2026-10-07T09:00:00+09:00"}


def test_equal_definition_refresh_preserves_values_but_real_change_invalidates(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    pin = _metadata(db, "observation_items", [DEFINITION])
    aid = _metadata(db, "observation_values", [VALUE], item_id=55, definition=DEFINITION, pin=pin)
    row = db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
    payload = json.loads(row[0])
    payload["definition_fingerprint"] = clinical_definition_fingerprint(
        10, {55: normalize_rows("observation_items", [DEFINITION])[0]})
    with db.db:
        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(payload), aid))
    _metadata(db, "observation_items", [DEFINITION], at=101)
    result = get_project_metadata(db.db, 1, "observation_values", item_id=55, now=110)
    assert result["state"] == "complete" and result["rows"][0]["scalar"] == 23
    changed = {"lab_test_item": {**DEFINITION["lab_test_item"], "unit": "changed-fictional-unit"}}
    _metadata(db, "observation_items", [changed], at=102)
    result = get_project_metadata(db.db, 1, "observation_values", item_id=55, now=110)
    assert result["state"] == "stale" and result["rows"] == []
    assert result["reason"] == "definition_mapping_changed"


def test_private_summary_shows_observation_provenance_without_promoting_chat_values(context_db, monkeypatch):
    import notify_views
    db = context_db
    _clinical(db, monkeypatch)
    _metadata(db, "medication_periods", [MED])
    _metadata(db, "observation_items", [DEFINITION])
    _metadata(db, "observation_values", [VALUE], item_id=55, definition=DEFINITION)
    text = "\n".join(notify_views._registered_clinical_lines(db.db, 1))
    assert "チャットとは別の記録" in text and "架空観測項目: 23" in text
    assert "架空単位" in text and VALUE["observation_issued_at"] in text
    assert "1処方期間" in text and "現在の状態を断定しません" in text


def test_canonical_context_details_reach_machine_read_without_a_legacy_llm(context_db):
    db = context_db
    body = "合成薬Aの残薬は3錠です。"
    with db.db:
        db.db.execute("UPDATE messages SET body_text=?,content_hash='synthetic-context' WHERE message_id=1", (body,))
    context = {"category": "medication_management", "text": "残薬は3錠", "evidence": body,
               "subject": "patient", "details": [{"key": "residual_quantity", "value": "3錠", "evidence": body}]}
    _artifact(db, "semantic_facts_v4", 1, {"patient_context": [context]},
              {"hash": "synthetic-context", "engine_version": 4})
    detail = read_model.read_model(db.db, scope="detail", project_id=1)
    item = next(r for r in detail["records"] if r["message_id"] == 1)["patient_context"][0]
    assert item["details"][0]["value"] == "3錠"
    aggregate = read_model.read_model(db.db, project_id=1)
    item = next(r for r in aggregate["records"] if r["message_id"] == 1)["patient_context"][0]
    assert item["details"] == [{"key": "residual_quantity", "count": 1}]
    assert "3錠" not in json.dumps(aggregate, ensure_ascii=False)


def test_unclassified_free_memo_is_preserved_only_in_detail(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    comment = "完全合成の自由文。未分類の内容も原記録として残します。"
    db.karte_summary_store(1, 10, {"comment": comment, "updated_at": "2026-10-07T10:00:00+09:00"})
    detail = read_model.read_model(db.db, scope="detail", project_id=1)["project_metadata"]["records"][0]
    assert detail["comment"] == comment and detail["unclassified_text"]
    aggregate = read_model.read_model(db.db, project_id=1)
    assert comment not in json.dumps(aggregate, ensure_ascii=False)
    assert "comment" not in aggregate["project_metadata"]["records"][0]


def _metadata(db, dataset, rows, *, pid=1, item_id=None, definition=None, complete=True,
              reason=None, at=100, pin=None):
    payload = {"contract": "project-metadata/1", "dataset": dataset, "scope": "karte",
               "entity_id": 10, "item_id": item_id, "complete": complete,
               "reason": reason, "http_status": None, "attempted_at": at,
               "rows": normalize_rows(dataset, rows), "definition": definition}
    if pin is not None:
        payload["definition_source_artifact_id"] = pin
        payload["definition_source"] = "observation_items"
    return db.artifact_add(ARTIFACT_KIND, json.dumps(payload), project_id=pid)


def _clinical(db, monkeypatch):
    db.db.execute("UPDATE patients SET project_type='medical',karte_id=10 WHERE project_id=1")
    db.db.execute("UPDATE patients SET project_type='group',karte_id=10 WHERE project_id=2")
    db.db.commit()
    monkeypatch.setattr(read_model, "_snapshot_meta", lambda _: {
        "generation_id": "synthetic-generation", "generated_at": 110, "published": True})


def test_registered_sources_keep_measurement_definition_and_independent_fetch_state(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    _metadata(db, "medication_periods", [MED, MED])
    _metadata(db, "observation_items", [DEFINITION])
    _metadata(db, "observation_values", [VALUE, VALUE], item_id=55, definition=DEFINITION)
    _metadata(db, "medication_periods", [], complete=False, reason="network_error", at=105)
    _metadata(db, "observation_values", [VALUE], pid=2, item_id=55, definition=DEFINITION)
    result = read_model.read_model(db.db, scope="detail")["registered_data"]
    assert result["total"] == 3 and not result["truncated"]
    assert all(row["project_id"] == 1 for row in result["records"])
    by_dataset = {row["dataset"]: row for row in result["records"]}
    medication = by_dataset["medication_periods"]
    assert medication["source"] == "mcs_structured" and medication["state"] == "failed"
    assert medication["last_complete_at"] == 100 and medication["attempted_at"] == 105
    assert medication["historical"] and not medication["current_known"]
    assert medication["rows"][0]["medicine_informations"][0]["name"] == "架空登録薬"
    values = by_dataset["observation_values"]
    assert values["rows"][0]["observation_issued_at"] == VALUE["observation_issued_at"]
    assert values["rows"][0]["scalar"] == 23
    assert values["definition"]["lab_test_item"]["unit"] == "架空単位"
    assert values["definition_binding"] == "legacy_unpinned"
    assert values["chat_comparison"] == "not_compared" and result["clinical_state"] == "not_inferred"
    aggregate = read_model.read_model(db.db)["registered_data"]
    encoded = json.dumps(aggregate, ensure_ascii=False)
    assert "架空" not in encoded and "2026-10" not in encoded
    assert all("rows" not in row and "attempted_at" not in row for row in aggregate["records"])
    assert {field["key"] for field in aggregate["records"][-1]["fields"]} == {"scalar", "observation_issued_at"}
    page = read_model.read_model(db.db, scope="detail", limit=1)["registered_data"]
    assert (page["total"], page["truncated"]) == (3, True)
    assert page["records"][0]["rows_total"] == 2 and page["records"][0]["rows_truncated"]
    assert len(page["records"][0]["rows"]) == 1


def test_changed_or_missing_definition_generation_excludes_old_values(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    pin = _metadata(db, "observation_items", [DEFINITION])
    _metadata(db, "observation_values", [VALUE], item_id=55, definition=DEFINITION, pin=pin)
    current = get_project_metadata(db.db, 1, "observation_values", item_id=55, now=110)
    assert current["state"] == "complete" and current["definition_binding"] == "current"
    _metadata(db, "observation_items", [{"lab_test_item": {"id": 55, "name": "架空別定義", "unit": "別単位"}}], at=105)
    stale = get_project_metadata(db.db, 1, "observation_values", item_id=55, now=110)
    assert stale["state"] == "stale" and stale["reason"] == "definition_mapping_changed"
    assert stale["rows"] == [] and stale["definition"] is None
    assert not stale["current_known"] and not stale["historical"]
    row = read_model.read_model(db.db, scope="detail")["registered_data"]["records"][-1]
    assert row["definition_binding"] == "mapping_changed" and row["rows"] == []
    db.db.execute("UPDATE patients SET karte_id=11 WHERE project_id=1")
    db.db.commit()
    assert get_project_metadata(db.db, 1, "observation_values", item_id=55, now=110)["rows"] == []


def test_scope_changed_failure_retains_independent_last_complete(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    _metadata(db, "medication_periods", [MED])
    _metadata(db, "medication_periods", [], complete=False, reason="scope_changed", at=105)
    result = get_project_metadata(db.db, 1, "medication_periods", now=110)
    assert result["state"] == "failed" and result["reason"] == "scope_changed"
    assert result["last_complete_at"] == 100 and result["rows"]


def test_invalid_registered_item_ids_do_not_break_the_machine_view(context_db, monkeypatch):
    db = context_db
    _clinical(db, monkeypatch)
    for item_id in (True, "invalid", 10 ** 100, -1):
        _metadata(db, "observation_values", [VALUE], item_id=item_id, definition=DEFINITION)
    result = read_model.read_model(db.db, scope="detail")["registered_data"]
    assert result["total"] == 2
    assert all(row["dataset"] != "observation_values" for row in result["records"])


def test_context_detail_attributes_reach_evidence_without_aggregate_values(context_db, tmp_path):
    import ledger
    import mcs_view

    db = context_db
    body = "服薬: 架空OTCを昨夜服用、確認者は架空薬剤師"
    db.db.execute("UPDATE messages SET body_text=? WHERE message_id=1", (body,))
    db.db.commit()
    details = [{"key": "actual_frequency", "value": "昨夜", "evidence": "昨夜服用"},
               {"key": "confirmed_by", "value": "架空薬剤師", "evidence": "確認者は架空薬剤師"}]
    item = {"category": "medication_management", "text": "架空OTCを昨夜服用、確認者は架空薬剤師",
            "evidence": body, "subject": "patient", "details": details}
    _artifact(db, "extract_llm", 1, {"patient_context": [item]}, {"hash": _hash_of(db, 1)})
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    view = mcs_view.View(snapshot)
    try:
        shown = view.read("evidence", project=1, message_id=1)["message"]["patient_context"][0]
        assert shown["details"] == details and shown["source"]["message_id"] == 1
        aggregate = view.read("read_model", project=1)["records"][0]["patient_context"][0]
        assert aggregate["details"] == [{"key": "actual_frequency", "count": 1}, {"key": "confirmed_by", "count": 1}]
        assert "昨夜" not in json.dumps(aggregate, ensure_ascii=False)
        assert "架空薬剤師" not in json.dumps(aggregate, ensure_ascii=False)
    finally:
        view.close()


@pytest.mark.parametrize("context,suffix", [
    ({"subject": "family"}, "対象:家族"), ({"subject": "other"}, "対象:本人以外"),
    ({"status": "planned"}, "予定"), ({"condition": "空腹時のみ"}, "条件:空腹時のみ")])
def test_typed_lab_context_never_becomes_current_patient_measurement(context, suffix):
    body = "架空検査23mg/dL"
    lines = structured_view._lab_lines({"labs": [{"name": "架空検査", "value": 23, "unit": "mg/dL",
                                                 "unverified": False, "evidence": body, **context}]}, body)
    assert lines[0].startswith("検査候補（未確認）:") and suffix in lines[0]


def test_past_laboratory_time_is_reported_without_inventing_posting_date():
    body = "先月の架空検査23mg/dL"
    lines = structured_view._lab_lines({"labs": [{"name": "架空検査", "value": 23, "unit": "mg/dL",
                                                 "evidence": body, "status": "past", "measured_on": "先月"}]}, body)
    assert "測定時期:先月" in lines[0] and "過去の報告" in lines[0]
