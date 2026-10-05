"""本人スタンプ集計がbody_state NULLの旧行を未削除として数えることの回帰テスト。"""
import json
import sqlite3

import notify_digest


def test_self_reaction_count_includes_null_body_state():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE patients(project_id, is_archived);"
        "CREATE TABLE messages(message_id, project_id, body_state);"
        "CREATE TABLE message_metadata(message_id, source, content, checked_at, last_error);"
        "INSERT INTO patients VALUES(1,0);"
        "INSERT INTO messages VALUES(10,1,NULL),(11,1,'full'),(12,1,'deleted');")
    content = json.dumps({"reactions": {"value": [
        {"type": "like", "count": 1, "self_reacted": True}], "observed_at": 150}})
    for mid in (10, 11, 12):
        db.execute("INSERT INTO message_metadata VALUES(?,?,?,?,?)",
                   (mid, "capture", content, 150, None))
    posts, counts = notify_digest._self_reaction_count(db, 100, 200)
    assert posts == 2
    assert sum(counts.values()) == 2
