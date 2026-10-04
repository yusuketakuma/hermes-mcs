"""M1 retains observed terminal hash changes, never previous body text."""
import hashlib
import json
import sqlite3

import pytest

from ingest_testkit import _ledger, _message
from ledger import Ledger, publish_snapshot


def message(body, state="full"):
    row = _message(mid=1, state=state)
    row.body_html = body
    return row


@pytest.mark.parametrize(("states", "bodies", "expected"), [
    (["snippet", "snippet"], ["A", "B"], []),
    (["snippet", "full"], ["A", "B"], []),
    (["full", "full"], ["A", "A"], []),
    (["full", "snippet"], ["A", "B"], []),
    (["full", "full"], ["A", "B"], ["A", "B"]),
    (["full", "full", "full"], ["A", "B", "B"], ["A", "B"]),
    (["full", "full", "full"], ["A", "B", "A"], ["A", "B", "A"]),
    (["full", "deleted"], ["A", ""], ["A", ""]),
    (["full", "deleted", "deleted"], ["A", "", ""], ["A", ""]),
    (["full", "deleted", "full"], ["A", "", "B"], ["A", "", "B"]),
])
def test_terminal_transition_chain_is_observed_without_replays(tmp_path, monkeypatch, states, bodies, expected):
    db = _ledger(tmp_path)
    try:
        for n, (state, body) in enumerate(zip(states, bodies), start=1):
            monkeypatch.setattr("ledger.time.time", lambda n=n: float(n))
            db.save_messages([message(body, state)])
        rows = db.db.execute(
            "SELECT * FROM message_revisions ORDER BY seq").fetchall()
        hashes = [hashlib.sha256(body.encode()).hexdigest() for body in expected]
        assert [row["content_hash"] for row in rows] == hashes
        assert [row["seq"] for row in rows] == list(range(1, len(expected) + 1))
        assert [row["prev_content_hash"] for row in rows] == (
            [None, *hashes[:-1]] if hashes else [])
        if rows:
            assert rows[0]["observed_at"] == 1.0
        assert set(row[1] for row in db.db.execute(
            "PRAGMA table_info(message_revisions)")) == {
                "message_id", "seq", "content_hash", "prev_content_hash",
                "body_state", "observed_at"}
    finally:
        db.close()


def test_legacy_null_hash_and_missing_table_remain_ingestable(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([message("SYNTHETIC OLD BODY")])
        db.db.execute("UPDATE messages SET content_hash=NULL WHERE message_id=1")
        db.db.execute("DROP TABLE message_revisions")
        db.db.commit()
        path = db.db.execute("PRAGMA database_list").fetchone()[2]
    finally:
        db.close()
    reopened = Ledger(path)
    try:
        reopened.save_messages([message("SYNTHETIC NEW BODY")])
        assert reopened.db.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0] == 0
    finally:
        reopened.close()


def test_history_and_body_roll_back_together(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([message("SYNTHETIC ORIGINAL")])
        with pytest.raises(sqlite3.IntegrityError):
            with db.db:
                db._upsert_message(message("SYNTHETIC CHANGED"))
                db.db.execute("INSERT INTO messages(message_id) VALUES(1)")
        assert db.db.execute("SELECT body_html FROM messages WHERE message_id=1").fetchone()[0] == "SYNTHETIC ORIGINAL"
        assert db.db.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0] == 0
    finally:
        db.close()


def test_snapshot_contains_hash_chain_not_old_body(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([message("SYNTHETIC OLD SENTINEL")])
        db.save_messages([message("SYNTHETIC CURRENT")])
        path = db.db.execute("PRAGMA database_list").fetchone()[2]
        snapshot = publish_snapshot(path, str(tmp_path / "snapshots"))
        assert snapshot is not None
        with sqlite3.connect(f"file:{snapshot}?mode=ro&immutable=1", uri=True) as reader:
            history = [tuple(row) for row in reader.execute("SELECT * FROM message_revisions")]
            assert len(history) == 2
            assert "SYNTHETIC OLD SENTINEL" not in json.dumps(history)
            assert reader.execute("SELECT body_html FROM messages WHERE message_id=1").fetchone()[0] == "SYNTHETIC CURRENT"
    finally:
        db.close()


class _RacingConnection:
    """Lets a second writer commit a revision right before ours inserts."""

    def __init__(self, conn, on_revision_insert):
        self._conn = conn
        self._hook = on_revision_insert

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, sql, *args):
        if self._hook and "INSERT OR IGNORE INTO message_revisions" in sql:
            hook, self._hook = self._hook, None
            hook()
        return self._conn.execute(sql, *args)


def test_overlapping_writer_revision_is_not_silently_dropped(tmp_path):
    db = _ledger(tmp_path)
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    other = Ledger(path)
    try:
        db.save_messages([message("SYNTHETIC A")])
        db.save_messages([message("SYNTHETIC B")])

        def other_writer():
            with other.db:
                other.db.execute(
                    "INSERT INTO message_revisions VALUES(1,3,'x','y','full',9)")

        db.db = _RacingConnection(db.db, other_writer)
        db.save_messages([message("SYNTHETIC C")])
        db.db = db.db._conn
        rows = db.db.execute(
            "SELECT seq,content_hash,prev_content_hash FROM message_revisions "
            "ORDER BY seq").fetchall()
        assert [r["seq"] for r in rows] == [1, 2, 3, 4]
        assert rows[-1]["content_hash"] == hashlib.sha256(b"SYNTHETIC C").hexdigest()
        assert rows[-1]["prev_content_hash"] == "x"
    finally:
        other.close()
        db.close()


def test_overlapping_writer_same_edit_is_not_recorded_twice(tmp_path):
    db = _ledger(tmp_path)
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    other = Ledger(path)
    c_hash = hashlib.sha256(b"SYNTHETIC C").hexdigest()
    try:
        db.save_messages([message("SYNTHETIC A")])
        db.save_messages([message("SYNTHETIC B")])

        def other_writer():
            with other.db:
                other.db.execute(
                    "INSERT INTO message_revisions VALUES(1,3,?,'y','full',9)",
                    (c_hash,))

        db.db = _RacingConnection(db.db, other_writer)
        db.save_messages([message("SYNTHETIC C")])
        db.db = db.db._conn
        rows = db.db.execute(
            "SELECT seq FROM message_revisions ORDER BY seq").fetchall()
        assert [r["seq"] for r in rows] == [1, 2, 3]
    finally:
        other.close()
        db.close()
