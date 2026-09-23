"""Canonical projection + consumer shadowing tests (T12).

``project_v2_doc_legacy`` renders an audited v2 doc into the legacy
extract_llm content shape — verified facts only, honestly lossy.
``current_fact_pred`` makes a hash-current canonical_projection row
shadow the message's extract_llm row for every read-side consumer.
"""
import json

import semantic_projection as projection
import semantic_facts as sf
from mcs_queries import current_fact_pred
from test_mcs_semantic import _seeded


def _fact(fid, kind="medication_event", verified=True, **kw):
    fact = {"fact_id": fid, "kind": kind, "subject": "patient:1",
            "actor": "sender:s1", "statement": "アムロジピン継続",
            "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "performed", "event_time": "unknown",
            "valid_time": "unknown", "quantity": "unknown",
            "evidence_ids": ["ev_1"], "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "verified" if verified else "unverified"}
    if kind == "medication_event":
        fact["action"] = "continue"
    fact.update(kw)
    return fact


EV = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
      "start": 0, "end": 9, "quote": "アムロジピン", "atom_id": "a1"}


def _doc(facts):
    return {"version": sf.CONTRACT_VERSION,
            "source": {"message_id": "m1", "revision": "r1",
                       "content_hash": "h", "body_codepoints": 20,
                       "content_quality": "full",
                       "attachments_complete": True,
                       "source_fingerprint": "sf_x"},
            "atoms": [], "chunks": [], "obligations": [],
            "evidence": [EV], "facts": list(facts), "relations": [],
            "coverage": {"category_counts": {},
                         "open_obligation_ids": [], "limitations": [],
                         "status": "complete"}}


def test_projection_maps_verified_medication_fact():
    doc = _doc([_fact("f1", action="stop",
                      workflow_status="performed", quantity="5mg",
                      statement="アムロジピン5mg中止")])
    out = projection.project_v2_doc_legacy(doc)
    assert len(out["meds"]) == 1
    med = out["meds"][0]
    assert med["action"] == "stop" and med["status"] == "past"
    assert med["subject"] == "patient" and not med["negated"]
    assert med["dose"] == "5mg"
    assert med["evidence"] == "アムロジピン"


def test_projection_skips_unverified_and_unmappable():
    doc = _doc([_fact("f1", verified=False),
                _fact("f2", kind="preference",
                      statement="訪問は午前希望"),
                _fact("f3", kind="symptom_state",
                      statement="疼痛あり",
                      workflow_status="reported")])
    out = projection.project_v2_doc_legacy(doc)
    assert "meds" not in out            # unverified fact never projects
    assert "requests" not in out        # preference has no legacy slot
    assert out["symptoms"][0]["status"] == "new"


def test_projection_subject_and_events():
    doc = _doc([_fact("f1", subject="role:family"),
                _fact("f2", kind="care_event",
                      statement="来週退院予定",
                      workflow_status="planned")])
    out = projection.project_v2_doc_legacy(doc)
    assert out["meds"][0]["subject"] == "family"
    assert out["events"] == ["discharge"]


def _artifact(db, kind, mid, content, meta):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES(?,?,?,?)",
        (kind, mid, json.dumps(content), json.dumps(meta)))


def test_current_fact_pred_prefers_canonical_projection(tmp_path):
    db = _seeded(tmp_path)
    try:
        hash_row = db.db.execute(
            "SELECT content_hash FROM messages WHERE message_id=1"
        ).fetchone()
        content_hash = hash_row["content_hash"]
        _artifact(db.db, "extract_llm", 1,
                  {"meds": [{"name": "旧薬", "action": "start"}]},
                  {"hash": content_hash})
        db.db.commit()

        sql = ("SELECT a.kind, a.content FROM artifacts a"
               " JOIN messages m ON m.message_id=a.message_id"
               " WHERE a.kind IN ('extract_llm','canonical_projection')"
               f" {current_fact_pred()} AND a.message_id=1")
        rows = db.db.execute(sql).fetchall()
        assert [r["kind"] for r in rows] == ["extract_llm"]

        # A hash-current canonical projection shadows the legacy row.
        _artifact(db.db, "canonical_projection", 1,
                  {"meds": [{"name": "新薬", "action": "stop"}]},
                  {"hash": content_hash})
        # A STALE canonical projection (old hash) must not shadow.
        _artifact(db.db, "canonical_projection", 2,
                  {"meds": [{"name": "古い", "action": "stop"}]},
                  {"hash": "outdated"})
        _artifact(db.db, "extract_llm", 2,
                  {"meds": [{"name": "旧2", "action": "start"}]},
                  {"hash": db.db.execute(
                      "SELECT content_hash FROM messages"
                      " WHERE message_id=2").fetchone()["content_hash"]})
        db.db.commit()
        rows = {r["message_id"]: r["kind"] for r in db.db.execute(
            sql.replace("a.message_id=1", "a.message_id IN (1,2)")
            .replace("a.kind, a.content",
                     "a.kind, a.content, a.message_id")).fetchall()}
        assert rows[1] == "canonical_projection"
        assert rows[2] == "extract_llm"
    finally:
        db.close()
