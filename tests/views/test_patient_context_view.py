"""Synthetic snapshot context preserves source/subject and keeps aggregate text-free."""
import json

import pytest

import ledger
import mcs_view
import read_model
from semantic_projection import PROJECTION_VERSION
from inference_version import CURRENT_VERSION, VERSION_KEY
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
                          message_id=1, meta={"hash": _hash_of(db, 1),VERSION_KEY:CURRENT_VERSION})
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
                    message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash_of(db, 1), "engine_version": 4,
                                        "projection_version": PROJECTION_VERSION})
    row = read_model.read_model(db.db, scope="detail", project_id=1)["records"][0]
    assert row["extraction"][kind]["state"] == "pending"
    assert row["patient_context"] == []


def test_source_reader_has_no_display_dependency_or_reference_refresh(context_db, monkeypatch):
    import mcs_queries
    db = context_db
    expected = _seed_context(db)
    message = dict(db.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=1").fetchone())
    monkeypatch.setattr(read_model, "_context_references", lambda *args: pytest.fail("raw read decorated UI"))
    monkeypatch.setattr(read_model, "message_patient_context", lambda *args: pytest.fail("raw read used view"))
    actual = mcs_queries.source_patient_context(db.db, message)
    assert actual == expected
    assert all("source" not in item for item in actual)
    assert not db.db.in_transaction
    # Explicit source invalidation still rejects the same original artifacts.
    db.db.execute("UPDATE artifacts SET meta=? WHERE message_id=1", (json.dumps({"hash": message["content_hash"], "invalidated": True}),))
    db.db.commit()
    assert mcs_queries.source_patient_context(db.db, message) == []


@pytest.mark.parametrize("generation", ["extract_llm", "canonical_projection", "semantic_facts_v4"])
def test_patient_mentions_use_raw_current_owner_and_preserve_written_qualifiers(
        context_db, monkeypatch, generation):
    import sys
    from types import SimpleNamespace
    import mcs_queries
    import patient_context_view as pcv

    store = context_db
    med_quote = '家族は架空薬Aを5mg、1日2回、経口で頓用する予定はありません。'
    symptom_quote = '本人は以前架空症状がありました。'
    body = med_quote + '\n' + symptom_quote + '\n架空規則薬と架空規則症状の記載。'
    store.save_messages([_message(1, body=body)], project_id=1)
    rule = {"medications": [{"name": "架空薬A"}, {"name": "架空規則薬"}],
            "symptoms": ["架空症状", "架空規則症状"]}
    fact = {"meds": [{"name": "架空薬A", "subject": "family", "dose": "5mg", "freq": "1日2回",
                      "route": "経口", "prn": True, "status": "planned", "negated": True,
                      "evidence": med_quote, "evidence_span": [0, len(med_quote)]}],
            "symptoms": [{"text": "架空症状", "subject": "patient", "status": "past",
                          "evidence": symptom_quote, "evidence_span": [len(med_quote)+1, len(med_quote)+1+len(symptom_quote)]}]}
    _artifact(store, "extract_v1", 1, rule, {"hash": _hash_of(store, 1)})
    _artifact(store, "extract_llm", 1, {"meds": [{"name": "旧世代薬"}]},
              {"hash": _hash_of(store, 1)})
    _artifact(store, generation, 1, fact, {"hash": _hash_of(store, 1), "engine_version": 4})
    # The raw path and display decoration must not import a writer/CLI owner.
    monkeypatch.setitem(sys.modules, "rollup", SimpleNamespace())
    store.db.execute("BEGIN")
    before = store.db.total_changes
    try:
        raw = mcs_queries.source_patient_mentions(store.db, 1)
        assert [(r["category"], r["text"], r["subject"], r["mention"]) for r in raw] == [
            ("medications", "架空薬A", "family", {"dose": "5mg", "freq": "1日2回", "route": "経口", "prn": True, "status": "planned", "negated": True}),
            ("symptoms", "架空症状", "patient", {"status": "past"}),
            ("medications", "架空規則薬", "unspecified", {"unverified": True}),
            ("symptoms", "架空規則症状", "unspecified", {"unverified": True})]
        assert all(r["message_id"] == 1 and r["content_hash"] == _hash_of(store, 1)
                   and r["reference_context"]["text"] == body and "source" not in r for r in raw)
        assert [r['quote'] for r in raw[:2]] == [med_quote, symptom_quote]
        assert all(body[slice(*r['evidence_span'])] == r['evidence'] for r in raw)
        assert all(r['quote'] != body for r in raw)
        decorated = pcv._mentions(store.db, 1, {1: ("架空投稿者", "架空職種")})
        assert [r["text"] for r in decorated] == [r["text"] for r in raw]
        assert [(r['evidence'], r['evidence_span'], r['reference_context'], r['mention']) for r in decorated] == [
            (r['evidence'], r['evidence_span'], r['reference_context'], r['mention']) for r in raw]
        assert all(r["sender"] == "架空投稿者" and r["profession"] == "架空職種"
                   and r["source"] == "chat" and r["details"] == [] for r in decorated)
        assert mcs_queries.source_patient_mentions(store.db, 2) == []
        assert store.db.in_transaction and store.db.total_changes == before
    finally:
        store.db.rollback()


def test_patient_mentions_read_failure_is_not_an_empty_observation():
    import sqlite3
    from mcs_queries import source_patient_mentions

    fault = sqlite3.OperationalError("synthetic snapshot unavailable")

    class RefusingDB:
        def execute(self, *_args):
            raise fault

    with pytest.raises(sqlite3.OperationalError) as raised:
        source_patient_mentions(RefusingDB(), 1)
    assert raised.value is fault


@pytest.mark.parametrize('span', [[True, 2], [-1, 2], [0, 9999], [1, 4], None])
def test_patient_mentions_never_guess_an_invalid_or_ambiguous_assertion_span(context_db, span):
    import mcs_queries
    text = '架空薬Aを5mg服用中です。'
    body = text+'別の合成記録。'+text
    context_db.save_messages([_message(1, body=body)], project_id=1)
    fact = {'name':'架空薬A', 'dose':'5mg', 'freq':'1日2回', 'route':'経口', 'prn':True, 'evidence':text}
    if span is not None:
        fact['evidence_span'] = span
    _artifact(context_db, 'extract_llm', 1, {'meds':[fact]}, {'hash':_hash_of(context_db,1)})
    row = mcs_queries.source_patient_mentions(context_db.db, 1)[0]
    assert row['evidence_span'] is None and row['quote'] == row['evidence'] == ''
    assert row['reference_context']['text'] == body
    assert row['mention'] == {'dose':'5mg', 'freq':'1日2回', 'route':'経口', 'prn':True, 'unverified':True}


def test_patient_mentions_accept_known_repeated_occurrence_with_null_full_body(context_db):
    import mcs_queries
    text = '架空薬Aを5mg服用中です。'
    prefix = text+'別の合成記録。'
    body = prefix+text
    context_db.save_messages([_message(1, body=body)], project_id=1)
    with context_db.db:
        context_db.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=1')
    _artifact(context_db, 'extract_llm', 1, {'meds':[{
        'name':'架空薬A', 'dose':'5mg', 'evidence':text, 'evidence_span':[len(prefix),len(body)]}]},
        {'hash':_hash_of(context_db,1)})
    row = mcs_queries.source_patient_mentions(context_db.db,1)[0]
    assert row['evidence'] == text and row['evidence_span'] == [len(prefix),len(body)]
    assert 'unverified' not in row['mention']


@pytest.mark.parametrize('deleted', [False, True])
def test_changes_excludes_metadata_occurrences_but_preserves_last_body_transition(context_db, monkeypatch, deleted):
    from dataclasses import replace
    from patient_context_view import changes
    db = context_db
    source = _message(1, body=BODY)
    clock = [100.0]
    monkeypatch.setattr(ledger.time, 'time', lambda: clock[0])
    with db.db:
        db.db.execute('UPDATE messages SET first_seen=1 WHERE message_id=1')
    db.save_messages([replace(source, sender_name='合成表示A')], project_id=1)
    ev = {'project_id': 1, 'items': [{'message_id': 1, 'discovery': 'initial'}], 'as_of': 400, 'coverage': {}}
    assert changes(db.db, ev, 50)['by_discovery']['edited'] == []
    clock[0] = 200.0
    body = replace(source, body_html='' if deleted else '<p>合成の本文編集です。</p>',
                   body_state='deleted' if deleted else 'full', sender_name='合成表示A')
    db.save_messages([body], project_id=1)
    clock[0] = 300.0
    db.save_messages([replace(body, sender_name='合成表示B')], project_id=1)
    result = changes(db.db, ev, 50)['by_discovery']
    assert result['edited'] == ev['items']
    assert result['deleted'] == ([{'message_id': 1, 'observed_at': 200.0}] if deleted else [])
    later = changes(db.db, ev, 250)['by_discovery']
    assert later['edited'] == [] and later['deleted'] == []
