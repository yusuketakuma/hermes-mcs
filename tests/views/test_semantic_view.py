"""Malformed artifact metadata stays non-current in the read-only view."""
import json

import pytest

import ledger
import mcs_view


@pytest.mark.parametrize("field", ["meta", "content"])
def test_deep_artifact_json_preserves_other_history(tmp_path, field):
    db_path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(db_path))
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,body_state,body_text,content_hash) "
                "VALUES(1,1,'full','synthetic message',?)", ("a" * 64,))
        store.artifact_add("semantic_summary", '{"summary":"synthetic valid"}',
                           project_id=1, message_id=1)
        corrupt_id = store.artifact_add("semantic_summary", "{}",
                                        project_id=1, message_id=1)
        with store.db:
            store.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?",
                             ('[' * 10000 + '0' + ']' * 10000, corrupt_id))
        snapshot = ledger.publish_snapshot(str(db_path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    view = mcs_view.View(snapshot)
    try:
        rows = view.read("semantic", project=1, message_id=1)["semantic"]["semantic_summary"]
        assert len(rows) == 2
        assert any(r["content"] == {"summary": "synthetic valid"} for r in rows)
        corrupt = next(r for r in rows if r["artifact_id"] == corrupt_id)
        assert corrupt[field] == ({} if field == "meta" else None)
        assert corrupt["current"] is False
    finally:
        view.close()


@pytest.mark.parametrize("metadata", [[], None, True, 7, "synthetic"])
def test_semantic_view_preserves_history_with_non_object_metadata(tmp_path, metadata):
    db_path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(db_path))
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,body_state,body_text,content_hash) "
                "VALUES(1,1,'full','synthetic message',?)", ("a" * 64,))
            store.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                "VALUES('semantic_summary',1,1,'{}',?)", (json.dumps(metadata),))
        snapshot = ledger.publish_snapshot(str(db_path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    view = mcs_view.View(snapshot)
    try:
        result = view.read("semantic", project=1, message_id=1)
        artifact = result["semantic"]["semantic_summary"][0]
        assert artifact["current"] is False
        assert artifact["effective_status"] == "STALE"
        assert artifact["meta"] == {}
    finally:
        view.close()


def test_semantic_view_lists_canonical_artifacts(tmp_path):
    """T5: the read-only view exposes every canonical artifact kind —
    facts v2, audit, and the projection pharmacists actually read —
    labelled by the same fingerprint/policy contract as Phase-J."""
    import semantic_store
    db_path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(db_path))
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,body_state,"
                "body_text,content_hash) "
                "VALUES(1,1,'full','synthetic message',?)", ("a" * 64,))
        fp = semantic_store.thread_bundle(store, 1, 1)["source_fingerprint"]
        with store.db:
            store.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,"
                "content,meta) VALUES('semantic_policy',1,1,'polx','{}')")
            for kind, meta in (
                    ("semantic_facts_v2", {"fingerprint": fp,
                                           "policy_fingerprint": "polx"}),
                    ("semantic_facts_audit",
                     {"fingerprint": fp, "policy_fingerprint": "polx",
                      "audit_status": "PASS"}),
                    ("canonical_projection",
                     {"fingerprint": fp, "policy_fingerprint": "polx"})):
                store.db.execute(
                    "INSERT INTO artifacts(kind,project_id,message_id,"
                    "content,meta) VALUES(?,1,1,'{}',?)",
                    (kind, json.dumps(meta)))
        snapshot = ledger.publish_snapshot(str(db_path),
                                           str(tmp_path / "snapshots"))
    finally:
        store.close()
    view = mcs_view.View(snapshot)
    try:
        result = view.read("semantic", project=1, message_id=1)
        sem = result["semantic"]
        assert len(sem["semantic_facts_v2"]) == 1
        assert len(sem["semantic_facts_audit"]) == 1
        assert len(sem["canonical_projection"]) == 1
        assert sem["semantic_facts_v2"][0]["current"] is True
        audit = sem["semantic_facts_audit"][0]
        assert audit["current"] is True
        assert audit["effective_status"] == "PASS"
        assert sem["canonical_projection"][0]["current"] is True
    finally:
        view.close()


@pytest.mark.parametrize("flags", [{"invalidated": True}, {"error": "failed"}])
def test_matching_fingerprint_does_not_revive_invalidated_artifact(tmp_path, flags):
    import semantic_store
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,body_state,body_text,content_hash) "
                "VALUES(1,1,'full','synthetic message',?)", ("a" * 64,))
        fingerprint = semantic_store.thread_bundle(store, 1, 1)["source_fingerprint"]
        store.artifact_add("semantic_policy", "polx")
        store.artifact_add("canonical_projection", "{}", project_id=1, message_id=1,
                           meta={"fingerprint": fingerprint,
                                 "policy_fingerprint": "polx", **flags})
        snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                           str(tmp_path / "snapshots"))
    finally:
        store.close()
    view = mcs_view.View(snapshot)
    try:
        artifact = view.read("semantic", project=1, message_id=1)["semantic"]["canonical_projection"][0]
        assert artifact["current"] is False
        assert artifact["effective_status"] == "STALE"
    finally:
        view.close()
