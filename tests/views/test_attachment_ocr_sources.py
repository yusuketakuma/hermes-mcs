"""Synthetic OCR read surfaces keep attachment text separate from chat and aggregate output."""
import hashlib
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import mcs_queries
import mcs_view
import read_model


@pytest.fixture
def source_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE messages(message_id INTEGER PRIMARY KEY,project_id INTEGER,
                              posted_at_ts INTEGER,body_text TEXT);
        CREATE TABLE attachments(attachment_id INTEGER PRIMARY KEY,message_id INTEGER,
                                 name TEXT,bytes INTEGER,sha256 TEXT,state TEXT,
                                 downloaded_at REAL,created_at REAL,error TEXT);
        INSERT INTO messages VALUES(10,1,100,'synthetic chat body');
        INSERT INTO messages VALUES(20,2,200,'other synthetic chat');
        INSERT INTO attachments VALUES(11,10,'synthetic.pdf',5,'hash','downloaded',0,0,NULL);
        INSERT INTO attachments VALUES(12,10,'unprocessed.pdf',5,'hash','downloaded',0,0,NULL);
        INSERT INTO attachments VALUES(21,20,'other.pdf',5,'hash','downloaded',0,0,NULL);
    """)
    db.execute("PRAGMA query_only=ON")
    yield db
    db.close()


@pytest.fixture
def ocr_reader(monkeypatch):
    pages = [{"page": number, "text": text,
              "text_hash": hashlib.sha256(text.encode()).hexdigest()}
             for number, text in enumerate(("synthetic OCR page one", "synthetic OCR page two"), 1)]
    calls = []

    def current(db, attachment_id, project_id=None):
        calls.append((attachment_id, project_id))
        if attachment_id == 11 and project_id in (None, 1):
            return {"artifact_id": 7, "attachment_id": 11, "message_id": 10,
                    "project_id": 1, "pages": pages, "state": "complete"}
        return None

    def view(db, attachment_id, project_id=None, include_text=False):
        doc = current(db, attachment_id, project_id)
        return ({"state": "unprocessed", "page_count": 0} if doc is None else
                {"state": doc["state"], "page_count": len(pages), "artifact_id": 7,
                 **({"pages": pages} if include_text else {})})

    monkeypatch.setitem(sys.modules, "attachment_ocr", SimpleNamespace(current=current, view=view))
    return pages, calls


def test_sources_are_distinct_scoped_and_read_only(source_db, ocr_reader):
    pages, calls = ocr_reader
    before = source_db.total_changes
    records = list(mcs_queries.attachment_ocr_sources(source_db, 1, mid=10))
    assert [row["message_id"] for row in records] == ["attachment_ocr:7:1", "attachment_ocr:7:2"]
    for row, page in zip(records, pages):
        assert row["source"] == row["source_kind"] == "attachment_ocr"
        assert row["parent_message_id"] == 10
        assert row["attachment_id"] == 11 and row["artifact_id"] == 7
        assert row["body_text"] == page["text"]
        assert row["content_hash"] == row["text_hash"] == page["text_hash"]
        assert not any(key.startswith("sender") for key in row)
    assert calls == [(11, 1), (12, 1)]
    lean = list(mcs_queries.attachment_ocr_sources(source_db, 1, hydrate=False))
    assert all("body_text" not in row for row in lean)
    assert [row["body_length"] for row in lean] == [len(page["text"]) for page in pages]
    assert list(mcs_queries.attachment_ocr_sources(source_db, 1, mid=20)) == []
    assert source_db.execute("SELECT body_text FROM messages WHERE message_id=10").fetchone()[0] == "synthetic chat body"
    assert source_db.total_changes == before


def test_manifest_hides_ocr_text_in_aggregate(source_db, ocr_reader):
    aggregate = read_model._attachments(source_db, "aggregate", 1)
    detail = read_model._attachments(source_db, "detail", 1)
    assert len(aggregate) == len(detail) == 2
    assert aggregate[0]["ocr"] == {"state": "complete", "page_count": 2, "artifact_id": 7}
    assert "synthetic OCR" not in str(aggregate)
    assert "name" not in aggregate[0]
    assert detail[0]["ocr"]["pages"] == ocr_reader[0]


def test_explicit_attachment_detail_includes_pages(source_db, ocr_reader, monkeypatch):
    view = mcs_view.View.__new__(mcs_view.View)
    view.db = source_db
    view.meta = {"generation_id": "synthetic-generation"}
    monkeypatch.setattr(view, "_require_message", lambda pid, mid: None)
    result = view._attachments(1, 10, 20, None)
    assert [row["attachment_id"] for row in result["items"]] == [12, 11]
    assert result["items"][1]["ocr"]["pages"] == ocr_reader[0]
    assert result["items"][0]["ocr"]["state"] == "unprocessed"
