"""Synthetic actual Ledger -> publication -> View -> proposed C1 records."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

import brain_export
import c1_records
from c1_contract import C1ContractError, validate_record
import ledger
import mcs_signals
import mcs_view
import read_model
from semantic_testkit import _message

SNAP_TS = 1790000000.5
ALLOWED = {"meta", "coverage", "message", "message_body", "patient_coverage",
           "signal", "signals_truncated"}
BODIES = {
    101: ("full", "架空の通常本文"), 102: ("full", ""),
    103: ("snippet", "SNIPPET_NOT_SENT"), 104: ("unknown", "UNKNOWN_NOT_SENT"),
    105: ("deleted", ""), 106: (None, "NULL_STATE_NOT_SENT"),
    107: ("full", None), 108: ("full", "a" * 8192),
    109: ("full", "b" * 8193), 110: ("full", "c" * 8191 + "架空"),
    111: ("full", "架" * 2730 + "😀q"), 112: ("full", "架空の返信本文"),
    113: ("full", None),
}


def _snapshot(tmp_path, monkeypatch, times=None):
    live = tmp_path / "source.db"
    with closing(ledger.Ledger(str(live))) as store:
        for pid in range(1, 9):
            store.ensure_patient(pid)
        for mid, (_, text) in BODIES.items():
            store.save_messages([_message(
                mid, parent=101 if mid == 112 else None, body=text or "")], project_id=1)
        store.save_messages([_message(501, pid=5, body="架空のarchived本文")], project_id=5)
        with store.db:
            for mid, (state, text) in BODIES.items():
                store.db.execute(
                    "UPDATE messages SET body_state=?,body_text=?,posted_at_ts=? "
                    "WHERE message_id=?", (state, text, (times or {}).get(mid, int(SNAP_TS) - 10), mid))
            store.db.execute(
                "UPDATE messages SET posted_at_ts=?,profession='薬剤師',"
                "organization='SYNTH_OWN_ORG',sender_name='SYNTH_SENDER_NAME'", (int(SNAP_TS) - 10,))
            # Apply explicit test times after setting the common synthetic metadata.
            for mid, stamp in (times or {}).items():
                store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (stamp, mid))
            for pid, state, upper, floor, archived in [
                    (1, "complete", int(SNAP_TS) - 100, -1, 0),
                    (2, "pending", 0, 0, 0),
                    (3, "incomplete", int(SNAP_TS) - 200, int(SNAP_TS) - 10 * 86400, 0),
                    (4, "complete", None, None, 0),
                    (5, "complete", int(SNAP_TS) - 300, int(SNAP_TS) - 20, 1),
                    (6, "pending", int(SNAP_TS) - 400, 0, 0),
                    (7, "incomplete", 0, None, 0),
                    (8, "pending", None, None, 0)]:
                store.db.execute(
                    "UPDATE patients SET fetch_state=?,coverage_ts=?,history_floor=?,"
                    "is_archived=?,last_complete_fetch=?,last_seen=? WHERE project_id=?",
                    (state, upper, floor, archived, SNAP_TS, SNAP_TS, pid))
            source_hash = store.db.execute(
                "SELECT content_hash FROM messages WHERE message_id=113").fetchone()[0]
            store.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at)"
                " VALUES('canonical_projection',1,113,?,?,?)",
                (json.dumps({"canonical_facts": [{"fact_id": "f-synth", "kind": "request_pending",
                    "validation_status": "unverified", "workflow_status": "pending",
                    "evidence_ids": ["e-synth"], "statement": "STATEMENT_NOT_SENT",
                    "evidence_quote": "QUOTE_NOT_SENT"}], "canonical_relations": []}),
                 json.dumps({"hash": source_hash}), SNAP_TS))
            for n in (1, 2):
                content = {"type": "request_aging", "project_id": 1, "state": "open",
                           "detected_at": SNAP_TS - n, "evidence": {"message_ids": [101]},
                           "context": {}, "note": "SIGNAL_NOTE_NOT_SENT"}
                store.db.execute(
                    "INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
                    " VALUES('signal_v1',1,?,?,?)",
                    (json.dumps(content), json.dumps({"key": f"sig-{n}"}), SNAP_TS))
    monkeypatch.setattr(ledger, "time", SimpleNamespace(time=lambda: SNAP_TS))
    published = ledger.publish_snapshot(str(live), str(tmp_path / "snapshots"))
    assert published is not None
    return live, published


def _by_type(records, kind):
    return [record for record in records if record["type"] == kind]


def test_assembly_has_no_writes_or_file_changes_and_legacy_bytes_are_unchanged(tmp_path, monkeypatch):
    # Given: the actual published snapshot and a caller-owned View transaction.
    live, snapshot = _snapshot(tmp_path, monkeypatch)
    paths = (live, Path(snapshot))
    before = [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths]
    with closing(mcs_view.View(snapshot)) as view:
        model = read_model.read_model(view.db)
        original_model = deepcopy(model)
        signals = mcs_signals.current_open(view.db, limit=1)
        legacy = brain_export._records_jsonl(view, signals, {}, model)
        statements = []
        view.db.set_trace_callback(statements.append)
        changes = view.db.total_changes
        # The real read supplies the same model object to expose accidental mutation.
        actual_read = read_model.read_model
        monkeypatch.setattr(read_model, "read_model", lambda db, scope: model)
        # When
        result = c1_records.assemble_records(view.db, signal_limit=1)
        # Then
        assert view.db.in_transaction and view.db.total_changes == changes
        assert all(s.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"} for s in statements)
        assert model == original_model
        assert brain_export._records_jsonl(view, signals, {}, model) == legacy
        assert actual_read(view.db) == original_model
        assert _by_type(result["records"], "coverage")[0]["coverage"]["collection"]["patients_incomplete"] == 5
        assert "patients_incomplete" not in original_model["coverage"]["collection"]
        legacy_records = [json.loads(line) for line in legacy.splitlines()]
        for kind in ("meta", "signal", "signals_truncated"):
            assert _by_type(result["records"], kind) == _by_type(
                legacy_records, kind)
        expected_messages = deepcopy(_by_type(legacy_records, "message"))
        for record in expected_messages:
            if record["body_state"] is None:
                record["body_state"] = "unknown"
        assert _by_type(result["records"], "message") == expected_messages
    assert [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths] == before


@pytest.mark.parametrize("mid,expected_size,truncated", [
    (101, len("架空の通常本文".encode()), False), (102, 0, False),
    (108, 8192, False), (109, 8192, True), (110, 8191, True), (111, 8190, True)])
def test_full_body_empty_and_utf8_boundaries_bind_transmitted_bytes(
        tmp_path, monkeypatch, mid, expected_size, truncated):
    # Given / When
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    body = next(r for r in _by_type(records, "message_body") if r["message_id"] == mid)
    # Then
    data = body["body_text"].encode("utf-8")
    assert len(data) == expected_size and body["body_truncated"] is truncated
    assert body["body_sha256"] == hashlib.sha256(data).hexdigest()
    assert body["body_format"] == "text" and body["sender_kind"] == "unknown"
    assert not {"sender", "sender_name", "profession", "organization"} & body.keys()


def test_partial_deleted_null_bodies_never_export_and_null_state_is_unknown(tmp_path, monkeypatch):
    # Given / When
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    # Then
    assert not {103, 104, 105, 106, 107, 113} & {
        r["message_id"] for r in _by_type(records, "message_body")}
    assert next(r for r in _by_type(records, "message") if r["message_id"] == 106)["body_state"] == "unknown"
    serialized = json.dumps(records, ensure_ascii=False)
    assert all(marker not in serialized for marker in [
        "SNIPPET_NOT_SENT", "UNKNOWN_NOT_SENT", "NULL_STATE_NOT_SENT",
        "SYNTH_SENDER_NAME", "SYNTH_OWN_ORG", "STATEMENT_NOT_SENT", "QUOTE_NOT_SENT",
        "SIGNAL_NOTE_NOT_SENT"])


def test_all_source_patients_include_zero_message_archived_and_real_coverage(tmp_path, monkeypatch):
    # Given / When
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        result = c1_records.assemble_records(view.db)
        # Then
        patients = {r["project_id"]: r for r in _by_type(result["records"], "patient_coverage")}
        assert set(patients) == set(range(1, 9))
        assert patients[1]["history_floor"] == 0
        assert patients[1]["coverage_ts"] == int(SNAP_TS) - 100
        assert patients[1]["coverage_ts"] < view.db.execute(
            "SELECT MAX(posted_at_ts) FROM messages WHERE project_id=1").fetchone()[0]
        assert patients[3]["history_floor"] == int(SNAP_TS) - 10 * 86400
        assert all(patients[pid]["history_floor"] is None for pid in (2, 4, 6, 7, 8))
        assert all(patients[pid]["coverage_ts"] is None for pid in (2, 4, 7, 8))
        assert patients[8]["fetch_state"] == "pending"
        assert 501 in {r["message_id"] for r in _by_type(result["records"], "message")}
        assert result["scope"]["archived"] == "included"
        assert result["scope"]["current_fetch_target_filter"] == "unknown"


def test_only_with_facts_keeps_actual_fact_tombstone_empty_body_and_reply_pairs(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db, signal_limit=1)["records"]
    original = deepcopy(records)
    # When
    result = c1_records.select_records(records, ALLOWED, only_with_facts=True)
    # Then
    kept = {r["message_id"] for r in _by_type(result["records"], "message")}
    assert kept == {101, 102, 105, 108, 109, 110, 111, 112, 113, 501}
    assert {r["message_id"] for r in _by_type(result["records"], "message_body")} <= kept
    assert len(_by_type(result["records"], "patient_coverage")) == 8
    assert len(_by_type(result["records"], "signal")) == 1
    assert _by_type(result["records"], "signals_truncated")[0]["total"] == 2
    assert records == original and result["absence"] == "unknown_not_absence"


def test_window_is_snapshot_bounded_unknown_time_and_floor_remain_unknown(tmp_path, monkeypatch):
    # Given
    boundary = SNAP_TS - 86400
    _, snapshot = _snapshot(tmp_path, monkeypatch, {
        101: boundary, 102: boundary - .25, 108: boundary + .25,
        109: SNAP_TS, 110: SNAP_TS + .25, 111: None})
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    original = deepcopy(records)
    # When
    result = c1_records.select_records(records, ALLOWED, since_days=1)
    # Then
    kept = {r["message_id"] for r in _by_type(result["records"], "message")}
    assert {101, 108, 109} <= kept and not {102, 110, 111} & kept
    assert {r["message_id"] for r in _by_type(result["records"], "message_body")} <= kept
    assert result["window_since"] == boundary and result["window_until"] == SNAP_TS
    assert result["messages_unknown_time"] == 1
    patients = {r["project_id"]: r for r in _by_type(result["records"], "patient_coverage")}
    assert patients[1]["history_floor"] == patients[3]["history_floor"] == int(boundary) + 1
    assert patients[5]["history_floor"] == int(SNAP_TS) - 20
    assert all(patients[pid]["history_floor"] is None for pid in (2, 4, 6, 7, 8))
    assert records == original


def test_type_filter_does_not_keep_body_held_messages_without_allowed_body(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    records += [{"type": "stat"}, {"type": "attachment"}]
    # When
    result = c1_records.select_records(records, ALLOWED - {"message_body"}, only_with_facts=True)
    # Then
    assert {r["message_id"] for r in _by_type(result["records"], "message")} == {105, 113}
    assert result["dropped_types"] == {"stat": 1, "attachment": 1, "message_body": 8}
    assert not _by_type(result["records"], "message_body")


def test_orphan_or_old_generation_body_cannot_hold_or_pair_a_message():
    # Given
    records = [
        {"type": "message", "snapshot_generation_id": "new", "message_id": 1,
         "body_state": "full", "facts": []},
        {"type": "message_body", "snapshot_generation_id": "old", "message_id": 1},
        {"type": "message_body", "snapshot_generation_id": "new", "message_id": 2}]
    # When / Then
    result = c1_records.select_records(records, ALLOWED, only_with_facts=True)
    assert result["records"] == [] and result["messages_dropped"] == 1


@pytest.mark.parametrize("stamp", [None, True, "1790000000", float("nan"), float("inf"), -1])
def test_unknown_snapshot_time_rejects_window_not_wall_clock_fallback(stamp):
    # Given / When / Then
    with pytest.raises(c1_records.SnapshotError, match="snapshot_time_required"):
        c1_records.select_records([{"type": "meta", "snapshot": {"generated_at": stamp}}],
                                  ALLOWED, since_days=1)


def test_caller_transaction_is_required_without_starting_one(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(ledger.LedgerReader(snapshot)) as reader:
        # When / Then
        with pytest.raises(c1_records.SnapshotError, match="caller_read_transaction_required"):
            c1_records.assemble_records(reader.db)
        assert not reader.db.in_transaction


def test_missing_coverage_columns_stay_null_in_real_old_snapshot(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with sqlite3.connect(snapshot) as db:
        db.execute("ALTER TABLE patients DROP COLUMN coverage_ts")
        db.execute("ALTER TABLE patients DROP COLUMN history_floor")
    with closing(mcs_view.View(snapshot)) as view:
        # When
        records = c1_records.assemble_records(view.db)["records"]
    # Then
    assert all(r["coverage_ts"] is r["history_floor"] is None
               for r in _by_type(records, "patient_coverage"))


def test_generated_records_pass_finished_validator_with_project_bound_body(tmp_path, monkeypatch):
    # Given / When
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    messages = {r["message_id"]: r for r in _by_type(records, "message")}
    # Then: exercise the real validator, including required body project IDs.
    for record in records:
        validate_record(record, message=messages.get(record.get("message_id"))
                        if record["type"] == "message_body" else None)
    body = deepcopy(_by_type(records, "message_body")[0])
    body["project_id"] = 2
    with pytest.raises(C1ContractError, match="body_message_mismatch"):
        validate_record(body, message=messages[body["message_id"]])


@pytest.mark.parametrize("state", [None, "legacy_unknown"])
def test_unrepresentable_patient_state_refuses_without_coercion_or_scope_shrinking(
        tmp_path, monkeypatch, state):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with sqlite3.connect(snapshot) as db:
        db.execute("UPDATE patients SET fetch_state=? WHERE project_id=8", (state,))
    before = Path(snapshot).read_bytes()
    with closing(mcs_view.View(snapshot)) as view:
        # When / Then
        with pytest.raises(C1ContractError, match="patient_coverage_state_invalid"):
            c1_records.assemble_records(view.db)
        assert view.db.in_transaction
    assert Path(snapshot).read_bytes() == before


def test_disallowed_message_never_leaves_orphan_body(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    # When
    result = c1_records.select_records(records, ALLOWED - {"message"})
    # Then
    assert not _by_type(result["records"], "message_body")
    assert result["messages_dropped"] == len(BODIES) + 1


def test_wrong_project_body_cannot_hold_factless_message(tmp_path, monkeypatch):
    # Given
    _, snapshot = _snapshot(tmp_path, monkeypatch)
    with closing(mcs_view.View(snapshot)) as view:
        records = c1_records.assemble_records(view.db)["records"]
    next(r for r in _by_type(records, "message_body") if r["message_id"] == 101)["project_id"] = 2
    # When
    result = c1_records.select_records(records, ALLOWED, only_with_facts=True)
    # Then
    assert 101 not in {r["message_id"] for r in _by_type(result["records"], "message")}
    assert 101 not in {r["message_id"] for r in _by_type(result["records"], "message_body")}
