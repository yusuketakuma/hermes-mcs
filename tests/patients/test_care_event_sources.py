"""Synthetic source and snapshot boundaries for care-event references."""
import json
import sqlite3

import pytest

import care_event_sources
import extract_llm
import inference_dependency_loss
import inference_version
from extract_testkit import _ledger, _message


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                          "WHERE project_id=1")
    yield ledger
    ledger.close()


def _save(store, mid, body, *, records=None):
    store.save_messages([_message(mid=mid, body=body, posted_at="2026-10-03T09:00:00+09:00")])
    row = dict(store.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, mid)
    row["_target"] = ("", extract_llm.resolved_model({}))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, row, json.dumps({"event_records": records or []}, ensure_ascii=False),
                                        integrity=integrity)


def _event(body, *, kind="discharge", subject="patient", status="current", negated=False):
    return {"kind": kind, "subject": subject, "status": status, "negated": negated,
            "evidence": body, "span": {"start": 0, "end": len(body)}}


def _read(store, **kwargs):
    if not store.db.in_transaction:
        store.db.execute("BEGIN")
    return care_event_sources.collect(store.db, 1, cfg={}, **kwargs)


def test_source_bound_event_preserves_axes_and_borrowed_snapshot_without_content(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    before = store.db.total_changes
    result = _read(store)
    event, = result["events"]
    assert event["kind"] == "discharge" and event["event_day"] == "2026-09-01"
    assert event["subject"] == "patient" and event["negated"] is False
    assert event["project_id"] == 1 and event["message_id"] == 1 and event["artifact_id"] > 0
    assert event["span"] == [0, len(body)] and body not in json.dumps(result, ensure_ascii=False)
    assert result["coverage"]["source_complete"] is True
    assert store.db.in_transaction and store.db.total_changes == before
    store.db.rollback()


def test_borrowed_snapshot_is_required_and_zero_is_not_unparsed(store):
    body = "本人は落ち着いています。"
    _save(store, 1, body)
    assert not store.db.in_transaction
    assert care_event_sources.collect(store.db, 1, cfg={})["coverage"]["state"] == "snapshot_required"
    checked = _read(store)
    assert checked["events"] == [] and checked["coverage"]["source_complete"] is True
    store.db.rollback()
    store.save_messages([_message(mid=2, body="本人は検査を受けました。")])
    pending = _read(store)
    assert pending["coverage"]["source_complete"] is False
    assert pending["coverage"]["not_extracted"] == 1


@pytest.mark.parametrize("body,kind,subject,status,negated", [
    ("家族は2026年9月1日に入院予定です。", "admission", "family", "planned", False),
    ("本人は2026年9月1日に退院していません。", "discharge", "patient", "current", True),
    ("本人は2026年12月1日に退院予定です。", "discharge", "patient", "planned", False),
])
def test_family_negation_and_plans_keep_source_axes(store, body, kind, subject, status, negated):
    _save(store, 1, body, records=[_event(body, kind=kind, subject=subject, status=status, negated=negated)])
    event, = _read(store)["events"]
    assert (event["subject"], event["status"], event["negated"]) == (subject, status, negated)


def test_other_patient_is_never_included_and_scope_refusal_is_not_empty(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="家族の合成記録")], project_id=2)
    out = _read(store)
    assert all(event["project_id"] == 1 and event["message_id"] == 1 for event in out["events"])
    store.db.rollback()
    store.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    refused = care_event_sources.collect(store.db, 1, cfg={})
    assert refused["events"] == [] and refused["coverage"]["state"] == "scope_unavailable"
    assert refused["coverage"]["source_complete"] is False


def test_revision_aba_and_no_configuration_never_reuse_old_extraction(store):
    original = "本人は2026年9月1日に退院しました。"
    _save(store, 1, original, records=[_event(original)])
    store.save_messages([_message(mid=1, body="本人は別の合成記録です。")])
    store.save_messages([_message(mid=1, body=original)])
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["source_complete"] is False
    assert result["coverage"]["not_extracted"] + result["coverage"]["unverified_extraction"] == 1
    without_cfg = care_event_sources.collect(store.db, 1, cfg=None)
    assert not without_cfg["events"] and not without_cfg["coverage"]["source_complete"]


def test_canonical_presence_never_silently_falls_back_to_local_result(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.artifact_add("canonical_projection", json.dumps({"event_records": []}), project_id=1, message_id=1,
                       meta={"hash": "old-revision", inference_version.VERSION_KEY: inference_version.LEGACY_VERSION})
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["canonical_unconfirmed"] == 1
    assert result["coverage"]["source_complete"] is False


def test_limits_and_incomplete_sources_are_reported_not_treated_as_zero(store):
    for mid, day in ((1, 1), (2, 2), (3, 3)):
        body = f"本人は2026年9月{day}日に退院しました。"
        _save(store, mid, body, records=[_event(body)])
    limited = _read(store, limit=2, event_limit=1)
    assert limited["coverage"]["truncated"] is True
    assert limited["coverage"]["event_truncated"] is True
    assert limited["coverage"]["source_complete"] is False
    assert len(limited["events"]) == 1
    store.db.rollback()
    store.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=3")
    partial = care_event_sources.collect(store.db, 1, cfg={})
    assert partial["coverage"]["incomplete"] == 1 and not partial["coverage"]["source_complete"]


@pytest.mark.parametrize("previously_observed", [False, True])
def test_deleted_original_never_claims_complete_history(store, previously_observed):
    if previously_observed:
        body = "本人は2026年9月1日に退院しました。"
        _save(store, 1, body, records=[_event(body)])
    store.save_messages([_message(mid=1, state="deleted", body="")])
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["deleted"] == 1
    assert result["coverage"]["source_complete"] is False


@pytest.mark.parametrize("state,complete", [("done", True), ("pending", False), (None, False), ("other", False)])
def test_unfinished_or_unknown_fetch_jobs_block_complete_history(store, state, complete):
    store.ensure_patient(2)
    with store.db:
        store.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',1,0,?)", (state,))
        store.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',2,0,NULL)")
    result = _read(store)
    assert result["coverage"]["history_complete"] is complete
    assert result["coverage"]["source_complete"] is complete


def test_oversize_metadata_is_not_selected_or_parsed(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    raw_hash = store.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    store.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                       meta={"hash": raw_hash, inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                             "padding": "x" * (care_event_sources._META_LIMIT + 1)})
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["resource_state"] == "oversize_or_nul"
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["source_complete"] is False


@pytest.mark.parametrize("table,field,limit", [
    ("messages", "body_text", care_event_sources._BODY_LIMIT),
    ("messages", "body_html", care_event_sources._HTML_LIMIT),
    ("artifacts", "meta", care_event_sources._META_LIMIT),
    ("artifacts", "content", care_event_sources._ARTIFACT_LIMIT),
])
@pytest.mark.parametrize("long", [False, True])
def test_nul_values_are_held_before_json_or_body_decoding(store, table, field, limit, long):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    malformed = "x\0" + "y" * (limit + 1 if long else 1)
    if table == "messages":
        store.db.execute(f"UPDATE messages SET {field}=? WHERE message_id=1", (malformed,))
    else:
        store.db.execute(f"UPDATE artifacts SET {field}=? WHERE kind='extract_llm' AND message_id=1", (malformed,))
    result = care_event_sources.collect(store.db, 1, cfg={})
    assert result["events"] == [] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"
    assert result["coverage"]["source_complete"] is False and malformed not in str(result)


def test_oversize_prior_chunk_content_is_held_before_progress_parses_it(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.artifact_add("extract_llm_chunk", "x" * (care_event_sources._ARTIFACT_LIMIT + 1),
                       project_id=1, message_id=1, meta={})
    result = _read(store)
    assert not result["events"] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"


@pytest.mark.parametrize("table,field", [
    ("patients", "fetch_state"), ("patients", "fetch_reason"),
    ("artifacts", "model"), ("message_revisions", "content_hash"),
])
@pytest.mark.parametrize("embedded_nul", [False, True])
def test_additional_fields_are_bounded_before_reading(store, table, field, embedded_nul):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    malformed = "x\0y" if embedded_nul else "x" * (care_event_sources._HTML_LIMIT + 1)
    if table == "message_revisions":
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(1,1,'synthetic','full',0)")
    where = {"patients": "project_id=1", "artifacts": "kind='extract_llm' AND message_id=1",
             "message_revisions": "message_id=1"}[table]
    changed = store.db.execute(f"UPDATE {table} SET {field}=? WHERE {where}", (malformed,)).rowcount
    assert changed > 0

    class RecordingConnection:
        def __init__(self, db):
            self.db = db
            self.statements = []

        @property
        def in_transaction(self):
            return self.db.in_transaction

        def execute(self, sql, *args):
            self.statements.append(sql)
            return self.db.execute(sql, *args)

    db = RecordingConnection(store.db)
    before = store.db.total_changes
    result = care_event_sources.collect(db, 1, cfg={})
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"
    assert not result["events"] and result["coverage"]["source_complete"] is False
    assert malformed not in str(result) and store.db.in_transaction and store.db.total_changes == before
    if table == "patients":
        assert not any(sql.startswith("SELECT fetch_state,fetch_reason") for sql in db.statements)
    assert not any(sql.startswith("SELECT a.artifact_id,a.kind,a.meta,a.model") for sql in db.statements)


def test_revision_row_budget_is_scoped_to_patient(store, monkeypatch):
    store.save_messages([_message(mid=1, body="本人の合成記録です。")])
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="別患者の合成記録です。")], project_id=2)
    store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                     "VALUES(1,1,'synthetic','full',0)")
    for seq in range(1, 5):
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(2,?,'synthetic','full',0)", (seq,))
    monkeypatch.setattr(care_event_sources, "_MAX_PREFLIGHT_ROWS", 2)
    assert care_event_sources.collect(store.db, 1, cfg={})["coverage"]["resource_state"] == "bounded"
    for seq in range(2, 4):
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(1,?,'synthetic','full',0)", (seq,))
    result = care_event_sources.collect(store.db, 1, cfg={})
    assert result["coverage"]["resource_state"] == "row_limit"
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert not result["events"] and not result["coverage"]["source_complete"]


@pytest.mark.parametrize("cap,reason", [("_MAX_PREFLIGHT_ROWS", "row_limit"),
                                         ("_MAX_PREFLIGHT_BYTES", "byte_limit")])
def test_preflight_budget_exhaustion_stays_unconfirmed(store, monkeypatch, cap, reason):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    monkeypatch.setattr(care_event_sources, cap, 0 if cap == "_MAX_PREFLIGHT_ROWS" else 1)
    result = _read(store)
    assert not result["events"] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == reason
    assert result["coverage"]["source_complete"] is False


def test_byte_length_unavailable_is_explicitly_unconfirmed(store):
    class OlderSQLite:
        def __init__(self, db):
            self.db = db

        @property
        def in_transaction(self):
            return self.db.in_transaction

        def execute(self, sql, *args):
            if sql == "SELECT octet_length('x')":
                raise sqlite3.OperationalError("no such function: octet_length")
            return self.db.execute(sql, *args)

    store.db.execute("BEGIN")
    result = care_event_sources.collect(OlderSQLite(store.db), 1, cfg={})
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "byte_length_unavailable"
    assert not result["events"] and result["coverage"]["source_complete"] is False


def test_old_schema_missing_occurrence_binding_field_remains_unknown():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE patients(project_id,fetch_state,fetch_reason,history_floor,initial_import,is_archived)")
        db.execute("CREATE TABLE messages(project_id,message_id,body_state,body_html,body_text,content_hash,"
                   "posted_at,posted_at_ts,parent_id,sender_id,sender_name,profession,organization)")
        db.execute("CREATE TABLE artifacts(artifact_id,project_id,message_id,kind,content,meta,model)")
        db.execute("CREATE TABLE message_revisions(message_id,seq)")
        db.execute("CREATE TABLE fetch_jobs(project_id,kind,state)")
        db.execute("CREATE TABLE attachments(message_id)")
        db.execute("BEGIN")
        result = care_event_sources.collect(db, 1, cfg={})
        assert result["coverage"]["state"] == "schema_unknown" and not result["events"]
        assert result["coverage"]["source_complete"] is False
    finally:
        db.close()


@pytest.mark.parametrize("value", [0, -1, 201, True])
def test_limits_are_bounded_without_reading(store, value):
    with pytest.raises(ValueError, match="care_event_scope_invalid"):
        care_event_sources.collect(store.db, 1, cfg={}, limit=value)
