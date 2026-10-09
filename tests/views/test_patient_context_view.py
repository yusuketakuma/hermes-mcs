"""Synthetic snapshot context preserves source/subject and keeps aggregate text-free."""
import json

import pytest

import ledger
import mcs_view
import read_model
from patient_context import extract_context
from semantic_testkit import _message, _patient
from test_read_model import _artifact, _db, _hash_of


BODY = "病名: 架空病A\n既往歴: 以前の架空病B\nADL: 歩行は介助\n【記録】家族は架空病C"


@pytest.fixture
def context_db(tmp_path):
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body=BODY), _message(2, body="病名: 架空別投稿病")], project_id=1)
    _patient(db, pid=2)
    db.save_messages([_message(3, pid=2, body="病名: 架空別患者病")], project_id=2)
    yield db
    db.close()


def _seed_context(db, mid=1):
    body = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    items = extract_context(body)
    _artifact(db, "extract_v1", mid, {"patient_context": items}, {"hash": _hash_of(db, mid)})
    return items


def test_detail_retains_context_despite_current_canonical_and_aggregate_has_counts(context_db):
    db = context_db
    _seed_context(db)
    llm = {"category": "history", "text": "家族は架空病C", "evidence": "家族は架空病C", "subject": "family"}
    _artifact(db, "extract_llm", 1, {"patient_context": [llm]}, {"hash": _hash_of(db, 1)})
    _artifact(db, "canonical_projection", 1, {"canonical_facts": []}, {"hash": _hash_of(db, 1)})
    detail = read_model.read_model(db.db, scope="detail", project_id=1)
    row = detail["records"][0]
    assert row["extraction"]["canonical_projection"]["state"] == "current"
    assert len(row["patient_context"]) == 4
    assert row["patient_context"][-1]["subject"] == "family"
    assert all(item["source"]["message_id"] == 1 and item["source"]["project_id"] == 1
               and item["source"]["content_hash"] == _hash_of(db, 1) for item in row["patient_context"])
    aggregate = read_model.read_model(db.db, project_id=1)
    assert aggregate["records"][0]["patient_context"] == [
        {"category": "adl", "count": 1}, {"category": "diagnoses", "count": 1},
        {"category": "history", "count": 2}]
    assert "架空" not in json.dumps(aggregate, ensure_ascii=False)
    assert all(record["project_id"] == 1 for record in detail["records"])


@pytest.mark.parametrize("fault", ["edited", "deleted", "foreign_project", "corrupt", "invalidated", "wrong_quote", "bad_category"])
def test_noncurrent_or_invalid_context_never_enters_view(context_db, fault):
    db = context_db
    items = _seed_context(db)
    if fault == "edited":
        db.db.execute("UPDATE messages SET content_hash='edited' WHERE message_id=1")
    elif fault == "deleted":
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    elif fault == "foreign_project":
        db.db.execute("DROP TRIGGER g1_artifacts_msg_upd")  # Simulate a corrupt imported snapshot.
        db.db.execute("UPDATE artifacts SET project_id=2 WHERE message_id=1")
    elif fault == "corrupt":
        db.db.execute("UPDATE artifacts SET content='[' WHERE message_id=1")
    elif fault == "invalidated":
        db.db.execute("UPDATE artifacts SET meta=? WHERE message_id=1",
                      (json.dumps({"hash": _hash_of(db, 1), "invalidated": True}),))
    elif fault == "wrong_quote":
        db.db.execute("UPDATE artifacts SET content=? WHERE message_id=1",
                      (json.dumps({"patient_context": [{**item, "evidence": "ない架空根拠"} for item in items]}),))
    else:
        db.db.execute("UPDATE artifacts SET content=? WHERE message_id=1",
                      (json.dumps({"patient_context": [{**items[0], "category": {"broken": True}}]}),))
    db.db.commit()
    assert read_model.read_model(db.db, scope="detail", project_id=1)["records"][0]["patient_context"] == []


def test_latest_current_extraction_wins_without_changing_limit_coverage(context_db):
    db = context_db
    items = _seed_context(db)
    _artifact(db, "extract_v1", 1, {"patient_context": items[:1]}, {"hash": _hash_of(db, 1)})
    page = read_model.read_model(db.db, scope="detail", limit=1)
    full = read_model.read_model(db.db, scope="detail")
    assert len(page["records"][0]["patient_context"]) == 1
    assert page["records"] == full["records"][:1]
    assert page["coverage"] == full["coverage"]
    assert (page["total"], page["truncated"]) == (3, True)


def test_existing_evidence_timeline_and_cursor_return_source_bound_context(context_db, tmp_path):
    _seed_context(context_db)
    _seed_context(context_db, 2)
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
    view = mcs_view.View(snapshot)
    try:
        evidence = view.read("evidence", project=1, message_id=1)
        assert evidence["message"]["patient_context"][0]["category"] == "diagnoses"
        page = view.read("timeline", project=1, limit=1)
        assert page["items"][0]["patient_context"] and page["next_cursor"]
        rest = view.read("timeline", project=1, limit=1, cursor=page["next_cursor"])
        assert rest["items"][0]["patient_context"] and rest["next_cursor"] is None
        assert {page["items"][0]["message_id"], rest["items"][0]["message_id"]} == {1, 2}
    finally:
        view.close()


def test_memo_context_detail_state_provenance_and_aggregate_privacy(context_db):
    db = context_db
    db.db.execute("UPDATE patients SET karte_id=project_id*10")
    db.db.commit()
    db.karte_summary_store(1, 10, {"comment": "住所: 合成住所のみ\nADL: 架空介助",
                                   "updated_at": "2026-10-07", "user": {"name": "合成記入者"}})
    db.karte_summary_store(2, 20, None)
    detail = read_model.read_model(db.db, scope="detail")["project_metadata"]["records"]
    assert [row["state"] for row in detail] == ["reported", "fetched_empty"]
    item = detail[0]["patient_context"][0]
    assert item["text"] == "合成住所のみ" and item["subject"] == "unspecified"
    assert item["source"]["artifact_id"] and item["source"]["project_id"] == 1
    assert item["source"]["updated_at"] == "2026-10-07" and item["source"]["fetched_at"] > 0
    aggregate = read_model.read_model(db.db)
    assert "合成住所" not in json.dumps(aggregate, ensure_ascii=False)
    assert "合成記入者" not in json.dumps(aggregate, ensure_ascii=False)
    assert aggregate["project_metadata"]["records"][0]["patient_context"] == [
        {"category": "adl", "count": 1}, {"category": "demographics", "count": 1}]
    page = read_model.read_model(db.db, limit=1)["project_metadata"]
    assert (len(page["records"]), page["total"], page["truncated"]) == (1, 2, True)


def test_memo_unknown_unfetched_and_changed_mapping_stay_distinct(context_db):
    db = context_db
    assert read_model.read_model(db.db)["project_metadata"]["records"][0]["state"] == "not_fetched"
    db.db.execute("UPDATE patients SET karte_id=10 WHERE project_id=1")
    db.db.commit()
    db.karte_summary_store(1, 10, {"comment": "ADL: 架空介助", "updated_at": "2026-10-07"})
    db.db.execute("UPDATE patients SET karte_id=11 WHERE project_id=1")
    db.db.commit()
    record = read_model.read_model(db.db, scope="detail", project_id=1)["project_metadata"]["records"][0]
    assert record["state"] == "unknown" and record["patient_context"] == []
    db.db.execute("UPDATE artifacts SET meta='[' WHERE kind='karte_summary'")
    db.db.commit()
    assert read_model.read_model(db.db)["project_metadata"]["records"][0]["state"] == "unknown"


def test_legacy_patient_table_without_karte_id_keeps_metadata_state_unknown(context_db):
    db = context_db
    db.db.execute("UPDATE patients SET karte_id=10 WHERE project_id=1")
    db.db.commit()
    db.karte_summary_store(1, 10, {"comment": "ADL: 架空介助", "updated_at": "2026-10-07"})
    # Historical snapshot layout: a mapping cannot be assumed when its column is absent.
    db.db.execute("ALTER TABLE patients DROP COLUMN karte_id")
    for scope in ("aggregate", "detail"):
        records = read_model.read_model(db.db, scope=scope)["project_metadata"]["records"]
        assert [row["state"] for row in records] == ["unknown", "not_fetched"]
        assert all(row["patient_context"] == [] for row in records)


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm"])
def test_legacy_message_bound_context_without_project_id_is_current(context_db, kind):
    db = context_db
    items = extract_context(BODY)
    aid = db.artifact_add(kind, json.dumps({"patient_context": items}),
                          message_id=1, meta={"hash": _hash_of(db, 1)})
    detail = read_model.read_model(db.db, scope="detail", project_id=1)
    row = detail["records"][0]
    assert row["extraction"][kind] == {"state": "current", "artifact_id": aid}
    assert len(row["patient_context"]) == len(items)
    message = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    assert read_model.message_patient_context(db.db, message) == row["patient_context"]
    aggregate = read_model.read_model(db.db, project_id=1)
    assert aggregate["coverage"]["extraction"][kind]["current"] == 1
    assert "架空" not in json.dumps(aggregate, ensure_ascii=False)


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
def test_published_context_still_requires_explicit_project_binding(context_db, kind):
    db = context_db
    db.artifact_add(kind, json.dumps({"canonical_facts": [], "patient_context": extract_context(BODY)}),
                    message_id=1, meta={"hash": _hash_of(db, 1), "engine_version": 4,
                                        "projection_version": read_model.PROJECTION_VERSION})
    row = read_model.read_model(db.db, scope="detail", project_id=1)["records"][0]
    assert row["extraction"][kind]["state"] == "pending"
    assert row["patient_context"] == []
