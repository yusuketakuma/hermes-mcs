"""Shared synthetic fixtures for the extract test family — ledger
handle, ``mcs_adapter.Message`` factory, content-hash probe, and
extract_llm / extract_qc artifact seeders.  Not a
test module (no ``test_`` prefix); sibling files import it via the
tests/ sys.path bootstrap."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import extract_llm
import ledger
import mcs_adapter


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, body="body", state="full", project_id=1,
             parent_id=None, unread=False,
             posted_at="2026-09-19T00:00:00+09:00", profession=""):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=parent_id,
        sender_id=1, sender_name="sender", sender_type="user",
        profession=profession, organization="", posted_at=posted_at,
        body_html=body, body_state=state, is_unread=unread,
        reply_count=0,
    )


def _hash(db, mid=1):
    return db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]


def _v2_artifact(db, mid, chash, content=None, project_id=1):
    return db.artifact_add(
        "extract_llm",
        json.dumps(content or {"meds": [{"name": "プレドニン",
                                         "action": "stop",
                                         "subject": "patient"}],
                               "urgency": "routine"}),
        project_id=project_id, message_id=mid,
        meta={"hash": chash,
              "extract_version": extract_llm.EXTRACT_VERSION})


def _qc_job(db, mid=1):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='extract_qc' AND message_id=?",
        (mid,)).fetchone()


def _extract_artifact(db, mid, content, chash, qc_fix=None):
    meta = {"hash": chash,
            "extract_version": extract_llm.EXTRACT_VERSION}
    if qc_fix is not None:
        meta["qc_fix"] = qc_fix
    return db.artifact_add("extract_llm", json.dumps(content),
                           project_id=1, message_id=mid, meta=meta)


def _qc_artifact(db, mid, src_id, chash, content):
    return db.artifact_add(
        "extract_qc", json.dumps(content, ensure_ascii=False),
        project_id=1, message_id=mid, model="jev-test",
        meta={"hash": chash,
              "extract_version": extract_llm.EXTRACT_VERSION,
              "source_artifact_id": src_id, "qc": "done"})


def _seed_qc_flagged(db, body="脈は48回／分です"):
    db.save_messages([_message(body=body)])
    chash = _hash(db, 1)
    src = _extract_artifact(db, 1, {"vitals": {"bs": 48},
                                    "summary": "s"}, chash)
    _qc_artifact(db, 1, src, chash,
                 {"qc": "done", "items": [
                     {"section": "vitals", "index": "bs",
                      "item": {"vitals": {"bs": 48}},
                      "verdict": "NO_MATCH", "noul": 0.1}]})
    return chash
