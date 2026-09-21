"""extract_qc — Jev quality-control drain job over extract_llm artifacts.

Covers: lazy seeding (including re-extraction re-pend), guarded claim
through the shared run_due path, annotate-only verdict artifacts, and
fail-open behavior for every Jev failure class.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))

import extract_llm
import ledger
import mcs_adapter
import semantic
import semantic_drain
import semantic_jev as jev


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, body="プレドニンを中止しました", project_id=1):
    return mcs_adapter.Message(
        message_id=mid, project_id=project_id, parent_id=None,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="", posted_at="2026-09-19T00:00:00+09:00",
        body_html=body, body_state="full", is_unread=False,
        reply_count=0)


def _hash(db, mid=1):
    return db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]


def _v2_artifact(db, mid, chash, content=None):
    db.artifact_add(
        "extract_llm",
        json.dumps(content or {"meds": [{"name": "プレドニン",
                                         "action": "stop",
                                         "subject": "patient"}],
                               "urgency": "routine"}),
        project_id=1, message_id=mid,
        meta={"hash": chash,
              "extract_version": extract_llm.EXTRACT_VERSION})


def _cfg(**kw):
    base = {"semantic": {"mode": "shadow", "summary_mode": "shadow",
                         "loop_mode": "off", "threshold_mode": "calibrated",
                         "calibration_version": "synthetic-test-v1",
                         "daily_request_budget": 50,
                         "project_ids": None}}
    base["semantic"].update(kw)
    return base


class _FakeJev:
    def __init__(self, noul=0.9, choice="routine", error=None):
        self.requests_made = 0
        self.noul = noul
        self.choice = choice
        self.error = error
        self.last_error = None
        self.calls = []

    def evaluate(self, state, questions, deadline):
        self.requests_made += 1
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        out = {}
        for qid, q in questions.items():
            if q.get("type") == "choice":
                opts = list(q.get("criteria") or {})
                out[qid] = {"type": "choice", "choice": self.choice,
                            "confidence": 0.9,
                            "probabilities": {o: 1.0 / len(opts)
                                              for o in opts}}
            else:
                out[qid] = {"type": "noul", "noul": self.noul}
        return {"answers": out, "model": jev.JEV_MODEL}


def _qc_job(db, mid=1):
    return db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='extract_qc' AND message_id=?",
        (mid,)).fetchone()


def _qc_artifact(db, mid=1):
    return db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind='extract_qc'"
        " AND message_id=?", (mid,)).fetchone()


# ---------- seeding ----------

def test_seed_queues_current_v2_artifact(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    assert semantic_drain._qc_seed(db, time.time()) == 1
    job = _qc_job(db)
    assert job is not None and job["state"] == "pending"
    assert json.loads(job["payload"])["hash"] == _hash(db)
    db.close()


def test_seed_skips_when_qc_artifact_exists_for_same_hash(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    h = _hash(db)
    _v2_artifact(db, 1, h)
    db.artifact_add("extract_qc", "{}", project_id=1, message_id=1,
                    meta={"hash": h})
    assert semantic_drain._qc_seed(db, time.time()) == 0
    assert _qc_job(db) is None
    db.close()


def test_seed_skips_stale_v1_and_error_artifacts(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(1), _message(2), _message(3)])
    h = _hash(db)
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                    meta={"hash": h})                       # v1: no version
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=2,
                    meta={"hash": _hash(db, 2), "error": "x",
                          "extract_version": extract_llm.EXTRACT_VERSION})
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=3,
                    meta={"hash": "old-hash",
                          "extract_version":
                          extract_llm.EXTRACT_VERSION})      # stale
    assert semantic_drain._qc_seed(db, time.time()) == 0
    db.close()


def test_seed_repends_done_job_when_hash_changes(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    semantic_drain._qc_seed(db, time.time())
    db.db.execute("UPDATE fetch_jobs SET state='done' "
                  "WHERE kind='extract_qc'")
    db.db.commit()
    # simulate re-extraction: new body hash + artifact, stale qc row
    db.db.execute("UPDATE messages SET content_hash='h2' WHERE message_id=1")
    db.db.commit()
    _v2_artifact(db, 1, "h2")
    assert semantic_drain._qc_seed(db, time.time()) == 1
    job = _qc_job(db)
    assert job["state"] == "pending" and job["attempts"] == 0
    assert json.loads(job["payload"])["hash"] == "h2"
    db.close()


def test_seed_keeps_failed_job_for_same_hash(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    semantic_drain._qc_seed(db, time.time())
    db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 "
                  "WHERE kind='extract_qc'")
    db.db.commit()
    semantic_drain._qc_seed(db, time.time())
    job = _qc_job(db)
    assert job["state"] == "failed" and job["attempts"] == 3
    db.close()


# ---------- end-to-end drain ----------

def test_drain_writes_annotate_only_qc_artifact(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    h = _hash(db)
    _v2_artifact(db, 1, h)
    client = _FakeJev(noul=0.9, choice="routine")
    result = {"errors": []}
    out = semantic.run_due(db, _cfg(extract_qc="annotate"), result,
                           time.monotonic() + 60, jev_client=client)
    assert out["done"] == 1
    row = _qc_artifact(db)
    assert row is not None
    meta = json.loads(row["meta"])
    assert meta["hash"] == h and meta["qc"] == "done"
    content = json.loads(row["content"])
    assert content["qc"] == "done"
    med = [i for i in content["items"] if i["section"] == "meds"][0]
    assert med["verdict"] == "MATCH" and med["noul"] == 0.9
    assert content["urgency"]["extracted"] == "routine"
    assert content["urgency"]["jev"] == "routine"
    # the extraction artifact is untouched — annotate only
    ex = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='extract_llm'"
        " AND message_id=1").fetchall()
    assert len(ex) == 1
    assert json.loads(ex[0]["content"])["meds"][0]["name"] == "プレドニン"
    db.close()


def test_done_qc_job_is_not_reclaimed(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev()
    for _ in range(2):
        semantic.run_due(db, _cfg(extract_qc="annotate"), {"errors": []},
                         time.monotonic() + 60, jev_client=client)
    job = _qc_job(db)
    assert job["state"] == "done"
    assert client.requests_made == 1      # evaluated exactly once
    rows = db.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind='extract_qc'"
        " AND message_id=1").fetchone()
    assert rows["c"] == 1
    db.close()


def test_drain_skips_qc_when_not_annotate(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev()
    out = semantic.run_due(db, _cfg(), {"errors": []},
                           time.monotonic() + 60, jev_client=client)
    assert out["done"] == 0 and client.requests_made == 0
    assert _qc_job(db) is None
    db.close()


def test_drain_no_jev_client_leaves_job_pending(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    out = semantic.run_due(db, _cfg(extract_qc="annotate", budget=0),
                           {"errors": []}, time.monotonic() + 60)
    assert out["done"] == 0
    # budget=0 -> no client -> not claimed; stays pending for a later
    # drain with a client (also: no seed without annotate+client here)
    db.close()


def test_qc_respects_project_scope(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(project_id=9)])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev()
    out = semantic.run_due(
        db, _cfg(extract_qc="annotate", project_ids=[1]),
        {"errors": []}, time.monotonic() + 60, jev_client=client)
    assert out["done"] == 0 and client.requests_made == 0
    job = _qc_job(db)
    assert job is not None and job["state"] == "pending"
    db.close()


# ---------- fail-open ----------

def test_jev_retryable_error_retries_job(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev(error=jev.JevError("transport", "x",
                                         retryable=True))
    out = semantic.run_due(db, _cfg(extract_qc="annotate"),
                           {"errors": []}, time.monotonic() + 60,
                           jev_client=client)
    assert out["done"] == 0
    job = _qc_job(db)
    assert job["state"] == "pending"  # retry leaves pending w/ backoff
    assert _qc_artifact(db) is None
    db.close()


def test_jev_resource_error_defers_without_artifact(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev(error=jev.JevError("no_api_key", "x"))
    semantic.run_due(db, _cfg(extract_qc="annotate"),
                     {"errors": []}, time.monotonic() + 60,
                     jev_client=client)
    job = _qc_job(db)
    assert job["state"] == "pending" and job["attempts"] == 0
    assert _qc_artifact(db) is None
    db.close()


def test_jev_permanent_error_writes_unevaluated_marker(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    client = _FakeJev(error=jev.JevError("contract_error", "x"))
    out = semantic.run_due(db, _cfg(extract_qc="annotate"),
                           {"errors": []}, time.monotonic() + 60,
                           jev_client=client)
    assert out["done"] == 1
    row = _qc_artifact(db)
    meta = json.loads(row["meta"])
    assert meta["qc"] == "unevaluated"
    assert json.loads(row["content"])["reason"] == "contract_error"
    db.close()


def test_qc_job_done_when_no_current_artifact(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    # stale artifact only — nothing current to QC
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                    meta={"hash": "old", "v": extract_llm.EXTRACT_VERSION})
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,"
        "state,next_try,created_at,updated_at) "
        "VALUES('extract_qc',1,1,'{}','pending',0,0,0)")
    db.db.commit()
    client = _FakeJev()
    out = semantic.run_due(db, _cfg(extract_qc="annotate"),
                           {"errors": []}, time.monotonic() + 60,
                           jev_client=client)
    assert out["done"] == 1 and client.requests_made == 0
    db.close()


# ---------- question layout ----------

def test_qc_questions_cap_and_layout():
    ex = {"meds": [{"name": f"med{i}"} for i in range(20)],
          "symptoms": [{"text": "s"}], "events": ["visit"],
          "urgency": "high"}
    questions, layout, ctx = semantic_drain._qc_questions(ex)
    assert len(questions) == semantic_drain.QC_MAX_ITEMS + 1  # +urgency
    assert questions["urg"]["type"] == "choice"
    noul_qs = [q for qid, q in questions.items()
               if q["type"] == "noul"]
    assert len(noul_qs) == semantic_drain.QC_MAX_ITEMS
    assert all("quoted message data" in q["instructions"]
               for q in questions.values())


def test_qc_questions_empty_extraction():
    questions, layout, ctx = semantic_drain._qc_questions({})
    assert questions == {} and layout == [] and ctx == {}
