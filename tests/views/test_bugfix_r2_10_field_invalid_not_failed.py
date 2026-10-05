"""反応以外の項目の不正（mentions_invalid 等）を再取得失敗と表示しない。"""
import json
import sqlite3

import message_metadata as mm


def _db(error):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE message_metadata(message_id INTEGER, source TEXT, "
               "content TEXT, checked_at REAL, last_error TEXT)")
    content = {"reactions": {"value": [{"type": "viewed", "count": 2,
                                        "self_reacted": False}],
                             "observed_at": 1.7e9}}
    for source in ("capture", "shadow"):
        db.execute("INSERT INTO message_metadata VALUES(5,?,?,?,?)",
                   (source, json.dumps(content), 1.7e9, error))
    return db


def test_mentions_invalid_keeps_reactions_observed():
    db = _db("mentions_invalid")
    meta = mm.get_message_metadata(db, 5)
    assert meta["reactions_status"] == "observed"
    assert meta["mentions_status"] == "invalid"
    assert meta["last_error"] is None
    assert "再取得失敗" not in mm.stamp_line(meta)
    assert mm.get_metadata_shadow_status(db, 5)["state"] != "failed"


def test_reaction_and_fetch_errors_still_reported():
    assert mm.get_message_metadata(_db("reactions_invalid"), 5)["last_error"] \
        == "reactions_invalid"
    assert mm.get_message_metadata(_db("network_error"), 5)["last_error"] \
        == "network_error"
    assert mm.get_message_metadata(
        _db("mentions_invalid,network_error"), 5)["last_error"] == "metadata_error"
