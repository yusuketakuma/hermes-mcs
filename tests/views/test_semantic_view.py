"""Malformed artifact metadata stays non-current in the read-only view."""
import json

import pytest

import ledger
import mcs_view


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
