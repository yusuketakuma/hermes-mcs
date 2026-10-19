"""Synthetic summary lookup borrows the transaction and defers expensive source facts."""
import json
import sqlite3

import pytest

import notify_flush
import notify_sender
import semantic_render
import semantic_send_gate


@pytest.mark.parametrize("problem", ["missing", "failed", "shadow", "unreadable"])
def test_current_summary_rejects_cheap_metadata_before_lazy_source(tmp_path, problem):
    db = sqlite3.connect(tmp_path / "synthetic.db")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if problem != "missing":
            meta = {"audit_status": "PASS", "publication_mode": "enforce",
                    "policy_fingerprint": "policy"}
            if problem == "failed":
                meta["audit_status"] = "NEEDS_REVIEW"
            if problem == "shadow":
                meta["publication_mode"] = "shadow"
            db.execute("INSERT INTO artifacts VALUES(1,'semantic_summary',1,1,'{}',?)",
                       ("not-json" if problem == "unreadable" else json.dumps(meta),))
        called = []

        def forbidden():
            called.append(True)
            raise AssertionError("ineligible summary reached thread bundle")

        assert semantic_send_gate.current_summary(db, 1, 1, forbidden, "policy") is None
        assert called == []
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    finally:
        db.close()


def test_current_summary_newest_invalid_never_revives_prior_and_keeps_pending_tx(tmp_path):
    db = sqlite3.connect(tmp_path / "synthetic.db")
    db.row_factory = sqlite3.Row
    meta = {"audit_status": "PASS", "publication_mode": "enforce",
            "policy_fingerprint": "policy", "fingerprint": "source",
            "target_revision": "revision"}
    bundle = {"source_fingerprint": "source",
              "members": [{"message_id": 1, "revision": "revision"}]}
    try:
        db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)")
        db.execute("INSERT INTO artifacts VALUES(1,'semantic_summary',1,1,'{}',?)", (json.dumps(meta),))
        db.commit()
        called = []

        def source():
            called.append("source")
            return bundle

        db.execute("BEGIN IMMEDIATE")
        current = semantic_send_gate.current_summary(db, 1, 1, source, "policy")
        assert current["artifact_id"] == 1 and called == ["source"]
        assert db.in_transaction
        db.execute("INSERT INTO artifacts VALUES(2,'semantic_summary',1,1,'{}',?)",
                   (json.dumps({**meta, "stale": True}),))
        assert semantic_send_gate.current_summary(db, 1, 1, source, "policy") is None
        assert called == ["source"]
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert semantic_send_gate.current_summary(db, 1, 1, bundle, "policy")["artifact_id"] == 1

        def unavailable():
            raise ValueError("synthetic source unavailable")

        assert semantic_send_gate.current_summary(db, 1, 1, unavailable, "policy") is None
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    finally:
        db.close()


def test_message_notice_keeps_per_parent_config_failure_isolated(monkeypatch):
    trace = []
    rows = [{"message_id": 1, "parent_id": None},
            {"message_id": 2, "parent_id": 1, "posted_at": "synthetic"}]
    monkeypatch.setattr(notify_flush, "_msg_rows", lambda *a: rows)
    monkeypatch.setattr(notify_flush, "_attachments_map", lambda *a: {})
    monkeypatch.setattr(notify_flush, "_collect_files", lambda *a: [])
    monkeypatch.setattr(notify_sender, "target", lambda *a: "discord:synthetic")

    def config():
        trace.append("config")
        if trace.count("config") == 1:
            return {}
        raise ValueError("synthetic unreadable config")

    def row(*args, **kwargs):
        trace.append("row" + str(args[3]["message_id"]))
        assert kwargs["summary_suffix"] == ""
        return str(args[3]["message_id"])

    monkeypatch.setattr(notify_flush, "_config", config)
    monkeypatch.setattr(notify_flush, "_fmt_row", row)
    monkeypatch.setattr(semantic_render, "semantic_summary_block",
                        lambda *a: pytest.fail("failed config reached summary"))
    text, files = notify_flush._message_notice(None, {"project_id": 1, "kind": "new_messages"}, {})
    assert trace == ["config", "config", "row1", "row2"]
    assert text == "[MCS unread] 新着 2 件\n\n1\n\n2" and files == []
