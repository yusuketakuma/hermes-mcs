"""Synthetic metadata reports retain validation with constant read-query count."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import message_metadata
import metadata_report

NOW = 1_800_000_000


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER);
        CREATE TABLE message_metadata(message_id INTEGER,source TEXT,content TEXT,
            checked_at REAL,last_error TEXT,PRIMARY KEY(message_id,source));
    """)
    yield conn
    conn.close()


def _row(db, mid, source, *, raw=None, error=None, checked=NOW - 60):
    if raw is None:
        raw = json.dumps({"reactions": {"value": [], "observed_at": NOW - 120}})
    db.execute("INSERT INTO message_metadata VALUES(?,?,?,?,?)",
               (mid, source, raw, checked, error))


def _reader(db, targets=()):
    return SimpleNamespace(
        db=db, metadata_watch_targets=lambda **kwargs: list(targets))


def test_report_reads_metadata_once_including_watch_set(db):
    for mid in range(1000):
        for source in ("capture", "shadow"):
            _row(db, mid, source)
    targets = [{"message_id": mid, "project_id": 1} for mid in range(1001)]
    queries = []
    db.set_trace_callback(queries.append)
    try:
        report = metadata_report.build_report(_reader(db, targets), now=NOW)
    finally:
        db.set_trace_callback(None)
    assert report["messages"] == 1000
    assert report["outcomes"] == {"match": 1000}
    assert report["watch"] == {
        "available": True, "due": 1001, "due_never_attempted": 1,
        "oldest_shadow_check_age_s": 60,
    }
    # The selector is stubbed: one table check and one streaming metadata read.
    assert len(queries) == 2
    assert sum("sqlite_master" in sql for sql in queries) == 1


def test_report_uses_same_decoder_for_corrupt_rows_and_sanitizes_errors(db, monkeypatch):
    for mid, raw, error in [
        (1, "{broken", None),
        (2, "{}", "reactions_invalid,schema_error"),
        (3, "{}", "synthetic-private-canary"),
        (4, "{}", "mentions_invalid"),
    ]:
        _row(db, mid, "shadow", raw=raw, error=error)
    _row(db, 5, "capture")
    expected = {
        (raw, checked, error): message_metadata._read_metadata(db, mid, source)
        for mid, source, raw, checked, error in db.execute(
            "SELECT message_id,source,content,checked_at,last_error FROM message_metadata")
    }
    original = message_metadata._decode_metadata
    decoded = []

    def checked_decode(row):
        result = original(row)
        if row is not None:
            assert result == expected[tuple(row)]
            decoded.append(tuple(row))
        return result

    monkeypatch.setattr(message_metadata, "_decode_metadata", checked_decode)
    report = metadata_report.build_report(_reader(db), now=NOW)
    assert len(decoded) == 5
    assert report["outcomes"] == {
        "shadow_invalid": 1, "shadow_failed": 2,
        "shadow_without_reactions": 1, "shadow_not_attempted": 1,
    }
    assert report["shadow_failures_by_code"] == {
        "metadata_error": 1, "reactions_invalid": 1, "schema_error": 1,
    }
    assert "synthetic-private-canary" not in json.dumps(report)


def test_project_filter_retains_orphan_and_unknown_source_semantics(db):
    db.executemany("INSERT INTO messages VALUES(?,?)", [(1, 1), (2, 2), (3, 1)])
    _row(db, 1, "capture")
    _row(db, 2, "shadow")
    _row(db, 3, "legacy")
    _row(db, 4, "shadow")  # Orphan rows were included in the unscoped report.
    targets = [{"message_id": 1, "project_id": 1}, {"message_id": 2, "project_id": 2}]
    reader = _reader(db, targets)
    scoped = metadata_report.build_report(reader, now=NOW, project_id=1)
    assert scoped["messages"] == 2
    assert scoped["outcomes"] == {
        "shadow_not_attempted": 1, "capture_without_reactions": 1,
    }
    assert scoped["watch"]["due"] == scoped["watch"]["due_never_attempted"] == 1
    assert metadata_report.build_report(reader, now=NOW)["messages"] == 4


def test_snapshot_without_metadata_table_stays_unavailable(db):
    db.execute("DROP TABLE message_metadata")

    def unavailable(**kwargs):
        raise sqlite3.OperationalError("synthetic missing table")

    report = metadata_report.build_report(
        SimpleNamespace(db=db, metadata_watch_targets=unavailable), now=NOW)
    assert report["messages"] == 0
    assert report["outcomes"] == {}
    assert report["watch"] == {"available": False}
