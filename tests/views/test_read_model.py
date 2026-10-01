"""T15 — the versioned machine read model: provenance, validity states,
scopes, coverage and attachment manifest over one snapshot generation.
All fixtures synthetic; no live DB, no network."""
import json
import sqlite3
import time

import pytest

import ledger as _ledger_mod
import read_model
from semantic_testkit import _ledger, _message, _patient

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


def test_actual_projection_preserves_validation_and_relation_type(tmp_path):
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import EV, v2_doc, v2_fact
    db = _db(tmp_path, (1,))
    try:
        doc = v2_doc([v2_fact(fid, quantity='unknown', evidence_ids=['ev_1'],
                              action='continue') for fid in ('f1', 'f2')],
                     [EV])
        doc['relations'] = [{'left_fact_id': 'f1', 'right_fact_id': 'f2',
                             'type': 'COMPLEMENTS'}]
        _artifact(db, 'canonical_projection', 1, project_v2_doc_legacy(doc),
                  {'hash': _hash_of(db, 1)})
        rec = read_model.read_model(db.db)['records'][0]
        assert {f['validation_status'] for f in rec['facts']} == {'verified'}
        assert rec['relations'] == [{'left_fact_id': 'f1', 'right_fact_id': 'f2',
                                    'kind': 'COMPLEMENTS'}]
        # A deleted source never exposes derived facts as usable.
        with db.db:
            db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        rec = read_model.read_model(db.db)['records'][0]
        assert rec['facts'] == [] and rec['relations'] == []
        assert rec['extraction']['canonical_projection']['state'] == 'stale'
    finally:
        db.close()


@pytest.mark.parametrize('fault', ['wrong_engine', 'wrong_project', 'error_flag'])
def test_v4_read_model_rejects_unusable_newer_rows(tmp_path, fault):
    db = _db(tmp_path, (1,))
    try:
        meta = {'hash': _hash_of(db, 1), 'engine_version': 4}
        _artifact(db, 'semantic_facts_v4', 1, {'canonical_facts': [{'fact_id': 'good'}]},
                  meta)
        changed = {**meta, **({'engine_version': 3} if fault == 'wrong_engine'
                             else {'error': 2} if fault == 'error_flag' else {})}
        _artifact(db, 'semantic_facts_v4', 1, {'canonical_facts': [{'fact_id': 'bad'}]},
                  changed)
        if fault == 'wrong_project':
            with db.db:
                db.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id='
                              '(SELECT MAX(artifact_id) FROM artifacts)')
        rec = read_model.read_model(db.db)['records'][0]
        assert [f['fact_id'] for f in rec['facts']] == ['good']
    finally:
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


def test_project_scope_limits_attachments_and_coverage(tmp_path):
    db = _db(tmp_path, (1,))
    _patient(db, pid=2)
    db.save_messages([_message(2, pid=2, body="synthetic other")],
                     project_id=2)
    db.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,bytes,sha256,"
        "state) VALUES(1,'f1','room-one.pdf',10,'aa','done')")
    db.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,bytes,sha256,"
        "state) VALUES(2,'f2','room-two.pdf',20,'bb','done')")
    db.db.commit()
    try:
        model = read_model.read_model(db.db, scope="detail", project_id=1)
        assert [r["message_id"] for r in model["records"]] == [1]
        assert [a["name"] for a in model["attachments"]] == [
            "room-one.pdf"]
        assert model["coverage"]["collection"]["patients"] == 1
        assert model["coverage"]["attachments"]["total"] == 1
    finally:
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
    with pytest.raises(ValueError, match="read_model_scope_invalid"):
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


def test_truncated_page_keeps_whole_scope_coverage(tmp_path):
    db = _db(tmp_path, tuple(range(1, 12)))
    page = read_model.read_model(db.db, limit=5)
    full = read_model.read_model(db.db)
    assert page["coverage"] == full["coverage"]
    assert page["coverage"]["collection"]["messages"] == page["total"]
    db.close()


def test_limit_page_builds_facts_only_for_returned_records(tmp_path, monkeypatch):
    db = _db(tmp_path, tuple(range(1, 8)))
    for mid in range(1, 8):
        _artifact(db, "canonical_projection", mid,
                  {"canonical_facts": [{"fact_id": f"f{mid}", "kind": "symptom",
                                        "evidence_ids": [f"e{mid}"]}]},
                  {"hash": _hash_of(db, mid)})
    full = read_model.read_model(db.db)
    real, calls = read_model._fact_relations, []
    monkeypatch.setattr(read_model, "_fact_relations",
                        lambda *a: calls.append(1) or real(*a))
    page = read_model.read_model(db.db, limit=3)
    assert len(calls) == 3
    assert page["records"] == full["records"][:3]
    assert [list(r) for r in page["records"]] == \
        [list(r) for r in full["records"][:3]]
    assert page["coverage"] == full["coverage"]
    assert (page["total"], page["truncated"]) == (7, True)
    db.close()


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_read_model_rejects_bad_limit(tmp_path, limit):
    db = _db(tmp_path, (1, 2))
    with pytest.raises(ValueError, match="bad_limit"):
        read_model.read_model(db.db, limit=limit)
    db.close()


@pytest.mark.parametrize("content", [
    {"canonical_facts": 1},
    {"canonical_relations": True},
    {"canonical_facts": [{"fact_id": {"raw": SECRET}}]},
    {"canonical_facts": [{"fact_id": "f1", "evidence_ids": {"raw": SECRET}}]},
    {"canonical_relations": [{"left_fact_id": [SECRET]}]},
])
@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
def test_malformed_canonical_content_is_unknown(tmp_path, content, kind):
    db = _db(tmp_path, (1,))
    try:
        _artifact(db, kind, 1, content,
                  {"hash": _hash_of(db, 1), "engine_version": 4})
        model = read_model.read_model(db.db)
        rec = model["records"][0]
        assert rec["extraction"][kind]["state"] == "unknown"
        assert rec["facts"] == [] and rec["relations"] == []
        assert SECRET not in json.dumps(model)
    finally:
        db.close()


def test_malformed_newer_projection_keeps_usable_current_row(tmp_path):
    db = _db(tmp_path, (1,))
    try:
        meta = {"hash": _hash_of(db, 1)}
        _artifact(db, "canonical_projection", 1,
                  {"canonical_facts": [{"fact_id": "valid"}]}, meta)
        _artifact(db, "canonical_projection", 1,
                  {"canonical_facts": 1}, meta)
        rec = read_model.read_model(db.db)["records"][0]
        assert rec["extraction"]["canonical_projection"]["state"] == "current"
        assert [f["fact_id"] for f in rec["facts"]] == ["valid"]
    finally:
        db.close()
