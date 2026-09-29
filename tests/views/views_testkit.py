"""Shared synthetic fixtures for the views test family — the stats
snapshot schema/row writers and the qc view's seeded ledger.  Not a
test module (no ``test_`` prefix); sibling files import it via the
tests/ sys.path bootstrap."""
import json
from datetime import datetime, timezone

import extract_llm
import ledger
import mcs_adapter
import mcs_view
from extract_testkit import _hash, _ledger


SCHEMA = """
CREATE TABLE snapshot_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                            generation_id TEXT, generated_at REAL);
CREATE TABLE patients (project_id INTEGER PRIMARY KEY, is_archived INTEGER,
                       fetch_state TEXT, created_at REAL);
CREATE TABLE messages (message_id INTEGER PRIMARY KEY, project_id INTEGER,
                       parent_id INTEGER, sender_id INTEGER,
                       sender_name TEXT, sender_type TEXT, profession TEXT,
                       organization TEXT, posted_at TEXT, posted_at_ts INTEGER,
                       body_text TEXT, body_state TEXT, content_hash TEXT,
                       reply_count INTEGER DEFAULT 0);
CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, kind TEXT,
                        project_id INTEGER, message_id INTEGER,
                        content TEXT, model TEXT, meta TEXT, created_at REAL);
CREATE TABLE requests (request_id INTEGER PRIMARY KEY, project_id INTEGER,
                       status TEXT, due_date TEXT, updated_at REAL,
                       source_message_id INTEGER);
"""


SNAP_TS = 1789975073.0  # 2026-09-21 JST


def _msg(db, mid, pid=1, sender=1, name="n1", prof="看護師", org="orgA",
         ts=1789900000, state="full", chash="h1"):
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, None, sender, name, "staff", prof, org,
         "2026-09-20T10:00:00+09:00", ts, "b", state, chash, 0))


def _extract(db, mid, chash, meds, events=None):
    content = {"meds": meds}
    if events is not None:
        content["events"] = events
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps(content), json.dumps({"hash": chash})))


def _message(mid, project_id=1):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=None,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="",
        posted_at=datetime.now(timezone.utc).isoformat(),
        body_html=f"本文{mid}", body_state="full", is_unread=False,
        reply_count=0)


def _v2(db, mid):
    db.artifact_add(
        "extract_llm", json.dumps({"meds": [], "urgency": "routine"}),
        project_id=1, message_id=mid,
        meta={"hash": _hash(db, mid),
              "extract_version": extract_llm.EXTRACT_VERSION})


def _qc(db, mid, content, chash=None):
    source_id = db.db.execute(
        "SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
        "AND message_id=?", (mid,)).fetchone()[0]
    db.artifact_add(
        "extract_qc", json.dumps(content), project_id=1, message_id=mid,
        model="jev", meta={"hash": chash or _hash(db, mid),
                           "extract_version": extract_llm.EXTRACT_VERSION,
                           "source_artifact_id": source_id,
                           "qc": content.get("qc")})


def _view(db, tmp_path):
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snap"))
    return mcs_view.View(snap)


def _seeded(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(i) for i in (1, 2, 3, 4)])
    for i in (1, 2, 3, 4):
        _v2(db, i)
    return db
