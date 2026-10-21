"""Shadow pairing reads all source-bound evidence, without changing legacy rows."""
import json
import sqlite3

from test_canonical_request_evaluation import support


def test_shadow_pairs_second_evidence_ref_and_rejects_missing_refs():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE messages(message_id INTEGER,project_id INTEGER,content_hash TEXT,body_state TEXT)")
        db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)")
        for mid in (1, 2):
            candidate = {
                "source": {"revision": "synthetic"},
                "projection": {"requests": [{"action": "架空の資料確認"}]},
                "loop_facts": [{"_v2_kind": "request_pending", "statement": "架空の資料確認",
                                "_evidence": {"quote": "架空の一つ目"},
                                "evidence_refs": ["first", "second" if mid == 1 else "missing"]}],
                "evidence": [{"evidence_id": "first", "quote": "架空の一つ目"},
                             {"evidence_id": "second", "quote": "架空の二つ目の資料"}]}
            db.execute("INSERT INTO messages VALUES(?,1,'synthetic','full')", (mid,))
            db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) VALUES('v4_stage',1,?,?,?)",
                       (mid, json.dumps({"candidate": candidate}), json.dumps({"stage": "request_following_candidate"})))
            db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) VALUES('extract_llm',1,?,?,?)",
                       (mid, json.dumps({"requests": [{"action": "資料確認", "evidence": "架空の二つ目の資料"}]}),
                        json.dumps({"hash": "synthetic"})))
        before = tuple(db.iterdump())
        result = support.shadow_compare(db)
        assert result["matched"] == result["compared"] == 1
        assert result["stale_or_invalid"] == 1
        assert tuple(db.iterdump()) == before
    finally:
        db.close()
