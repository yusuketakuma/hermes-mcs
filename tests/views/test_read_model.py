"""T15 — the versioned machine read model: provenance, validity states,
scopes, coverage and attachment manifest over one snapshot generation.
All fixtures synthetic; no live DB, no network."""
import json
import sqlite3
import time

import pytest

import ledger as _ledger_mod
import read_model
from test_mcs_semantic import _ledger, _message, _patient

SECRET = "SYNTHETIC_BODY_NEVER_IN_AGGREGATE_7x2"


def _db(tmp_path, mids=(1, 2, 3)):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages(
        [_message(m, body=f"{SECRET}-{m}") for m in mids], project_id=1)
    return db


def _artifact(db, kind, mid, content, meta):
    db.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,"
        "created_at) VALUES(?,1,?,?,?,?)",
        (kind, mid, json.dumps(content), json.dumps(meta), time.time()))
    db.db.commit()


def _hash_of(db, mid):
    return db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]


def test_states_current_stale_pending_unknown(tmp_path):
    db = _db(tmp_path, (1, 2, 3, 4))
    h1 = _hash_of(db, 1)
    _artifact(db, "extract_llm", 1, {"summary": "ok"},
              {"hash": h1, "extract_version": 3})
    _artifact(db, "extract_llm", 2, {"summary": "old"},
              {"hash": "superseded-hash", "extract_version": 3})
    _artifact(db, "extract_llm", 3, {}, {"hash": "hash-3", "error": 1})
    # message 4: no artifacts at all -> pending
    model = read_model.read_model(db.db)
    by_mid = {r["message_id"]: r for r in model["records"]}
    assert by_mid[1]["extraction"]["extract_llm"]["state"] == "current"
    assert by_mid[1]["state"] == "current"
    assert by_mid[2]["extraction"]["extract_llm"]["state"] == "stale"
    assert by_mid[2]["state"] == "stale"
    # error-only rows classify as unknown, never as current —
    # the failure stays visible via last_error
    assert by_mid[3]["extraction"]["extract_llm"]["state"] == "unknown"
    assert by_mid[3]["extraction"]["extract_llm"]["last_error"] is True
    assert by_mid[4]["state"] == "pending"
    db.close()


def test_deleted_source_is_never_current(tmp_path):
    db = _db(tmp_path, (1,))
    h1 = _hash_of(db, 1)
    _artifact(db, "extract_llm", 1, {"summary": "kept"},
              {"hash": h1, "extract_version": 3})
    db.db.execute("UPDATE messages SET body_state='deleted', "
                  "body_text='' WHERE message_id=1")
    db.db.commit()
    rec = read_model.read_model(db.db)["records"][0]
    # artifact hashes still match the stored hash — but the tombstoned
    # source must never read as current
    assert rec["state"] == "stale"
    assert rec["body_state"] == "deleted"
    db.close()


def test_invalidated_projection_is_stale_not_current(tmp_path):
    db = _db(tmp_path, (1,))
    h1 = _hash_of(db, 1)
    _artifact(db, "canonical_projection", 1,
              {"canonical_facts": [{"fact_id": "f1", "kind": "medication_event",
                                    "validation_status": "verified",
                                    "evidence_ids": ["e1"]}],
               "canonical_relations": []},
              {"hash": h1, "invalidated": True})
    rec = read_model.read_model(db.db)["records"][0]
    assert rec["extraction"]["canonical_projection"]["state"] == "stale"
    assert rec["facts"] == []        # stale projection exports no facts
    db.close()


def test_facts_and_relations_bind_evidence_ids(tmp_path):
    db = _db(tmp_path, (1,))
    h1 = _hash_of(db, 1)
    _artifact(db, "canonical_projection", 1,
              {"canonical_facts": [
                  {"fact_id": "f1", "kind": "medication_event",
                   "statement": "synthetic statement",
                   "validation_status": "verified",
                   "workflow_status": "planned",
                   "evidence_ids": ["e1", "e2"],
                   "evidence_quote": "synthetic quote"},
                  {"fact_id": "f2", "kind": "symptom",
                   "validation_status": "verified",
                   "evidence_ids": []}],
               "canonical_relations": [
                   {"left_fact_id": "f1", "right_fact_id": "f2",
                    "kind": "related"}]},
              {"hash": h1})
    model = read_model.read_model(db.db, scope="aggregate")
    rec = model["records"][0]
    assert rec["state"] == "current"
    assert rec["facts"][0]["fact_id"] == "f1"
    assert rec["facts"][0]["evidence_ids"] == ["e1", "e2"]
    assert rec["relations"] == [{"left_fact_id": "f1",
                                 "right_fact_id": "f2",
                                 "kind": "related"}]
    # aggregate scope never carries free text
    assert "statement" not in rec["facts"][0]
    assert "evidence_quote" not in rec["facts"][0]
    # detail scope does
    detail = read_model.read_model(db.db, scope="detail")
    assert detail["records"][0]["facts"][0]["statement"] == \
        "synthetic statement"
    db.close()


def test_aggregate_scope_contains_no_raw_content(tmp_path):
    db = _db(tmp_path, (1, 2))
    db.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,bytes,sha256,"
        "state) VALUES(1,'fid','SYNTH_SECRET_FILENAME.pdf',10,"
        "'aa','done')")
    db.db.commit()
    model = read_model.read_model(db.db, scope="aggregate")
    blob = json.dumps(model, ensure_ascii=False)
    assert SECRET not in blob
    assert "SYNTH_SECRET_FILENAME.pdf" not in blob
    assert "sender" not in blob      # sender names stay off aggregate
    att = model["attachments"][0]
    assert set(att) == {"attachment_id", "message_id", "bytes",
                        "sha256", "state"}
    detail = read_model.read_model(db.db, scope="detail")
    assert detail["attachments"][0]["name"] == "SYNTH_SECRET_FILENAME.pdf"
    db.close()


def test_coverage_distinguishes_no_record_from_no_event(tmp_path):
    db = _db(tmp_path, (1, 2))
    _artifact(db, "extract_llm", 1, {"meds": [], "symptoms": []},
              {"hash": _hash_of(db, 1), "extract_version": 3})
    model = read_model.read_model(db.db)
    cov = model["coverage"]["extraction"]["extract_llm"]
    # message 1 extracted and empty; message 2 never attempted —
    # absence of facts on 1 is a finding; absence on 2 is pending work
    assert cov == {"current": 1, "stale": 0, "pending": 1,
                   "unknown": 0}
    by_mid = {r["message_id"]: r for r in model["records"]}
    assert by_mid[1]["facts"] == [] and by_mid[1]["state"] == "current"
    assert by_mid[2]["state"] == "pending"
    db.close()


def test_snapshot_generation_binding_and_live_marker(tmp_path):
    db = _db(tmp_path, (1,))
    # live ledger: no snapshot_meta -> generation_id None + published False
    live = read_model.read_model(db.db)
    assert live["snapshot"]["published"] is False
    assert live["snapshot"]["generation_id"] is None
    db.close()
    # published snapshot binds the generation id it was cut from
    dest = tmp_path / "snaps"
    path = _ledger_mod.publish_snapshot(str(tmp_path / "ledger.db"),
                                        str(dest))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        model = read_model.read_model(conn)
    finally:
        conn.close()
    assert model["snapshot"]["published"] is True
    assert model["snapshot"]["generation_id"]
    db2 = sqlite3.connect(str(tmp_path / "ledger.db"))
    assert db2.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    db2.close()


def test_truncated_is_explicit_never_silent(tmp_path):
    db = _db(tmp_path, tuple(range(1, 12)))
    model = read_model.read_model(db.db, limit=5)
    assert model["truncated"] is True
    assert model["total"] == 11
    assert len(model["records"]) == 5
    full = read_model.read_model(db.db)
    assert full["truncated"] is False and len(full["records"]) == 11
    db.close()


def test_bad_scope_rejected(tmp_path):
    db = _db(tmp_path, (1,))
    with pytest.raises(ValueError):
        read_model.read_model(db.db, scope="everything")
    db.close()


def test_read_surfaces_share_one_snapshot_generation(tmp_path):
    """stats / signals / read_model all bind to the SAME published
    generation — a read can never silently mix snapshot generations."""
    import mcs_view
    db = _db(tmp_path, (1,))
    db.close()
    dest = tmp_path / "snaps"
    snap = _ledger_mod.publish_snapshot(str(tmp_path / "ledger.db"),
                                        str(dest))
    view = mcs_view.View(snap)
    try:
        gen = view.meta["generation_id"]
        stats = view.stats({"preset": "operational", "limit": 5})
        signals = view.signals({"limit": 5})
        model = read_model.read_model(view.db)
    finally:
        view.close()
    assert stats["snapshot"]["generation_id"] == gen
    assert signals["snapshot"]["generation_id"] == gen
    assert model["snapshot"]["generation_id"] == gen
