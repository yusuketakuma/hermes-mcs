"""extract_qc — Jev quality-control drain job over extract_llm artifacts.

Covers: lazy seeding (including re-extraction re-pend), guarded claim
through the shared run_due path, annotate-only verdict artifacts, and
fail-open behavior for every Jev failure class.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import extract_llm
import semantic
import semantic_drain
import semantic_jev as jev
from extract_testkit import _hash, _ledger, _message
from test_mcs_semantic import _FakeJev


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


def _cfg(**kw):
    base = {"semantic": {"mode": "shadow", "summary_mode": "shadow",
                         "loop_mode": "off", "threshold_mode": "calibrated",
                         "calibration_version": "synthetic-test-v1",
                         "daily_request_budget": 50,
                         "project_ids": None}}
    base["semantic"].update(kw)
    return base


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
    source_id = _v2_artifact(db, 1, h)
    db.artifact_add("extract_qc", "{}", project_id=1, message_id=1,
        meta={"hash": h,
                          "extract_version": extract_llm.EXTRACT_VERSION,
                          "source_artifact_id": source_id})
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


def test_seed_skips_archive_posts(tmp_path):
    """Posts older than 60 days stay outside QC; a 30-day post is eligible."""
    db = _ledger(tmp_path)
    old = time.strftime("%Y-%m-%dT%H:%M:%S+09:00",
                        time.localtime(time.time() - 61 * 86400))
    recent = time.strftime("%Y-%m-%dT%H:%M:%S+09:00",
                           time.localtime(time.time() - 30 * 86400))
    db.save_messages([_message(1, posted_at=old), _message(2, posted_at=recent)])
    _v2_artifact(db, 1, _hash(db, 1))
    _v2_artifact(db, 2, _hash(db, 2))
    assert semantic_drain._qc_seed(db, time.time()) == 1
    assert _qc_job(db, 1) is None
    assert _qc_job(db, 2) is not None
    db.close()


def test_seed_reaps_stale_archive_jobs(tmp_path):
    """A pending QC job whose post aged out of the 60-day window is
    reaped on the next seed pass, not evaluated late."""
    db = _ledger(tmp_path)
    old = time.strftime("%Y-%m-%dT%H:%M:%S+09:00",
                        time.localtime(time.time() - 61 * 86400))
    db.save_messages([_message(1, posted_at=old)])
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,"
        "state,next_try,created_at,updated_at) "
        "VALUES('extract_qc',1,1,'{}','pending',0,0,0)")
    db.db.commit()
    semantic_drain._qc_seed(db, time.time())
    assert _qc_job(db, 1) is None
    db.close()


def test_seed_window_does_not_consume_pending_rows(tmp_path):
    """F15: rows already pending must not eat the LIMIT window — a
    second seed pass still reaches artifacts behind them."""
    db = _ledger(tmp_path)
    db.save_messages([_message(1), _message(2)])
    _v2_artifact(db, 1, _hash(db, 1))
    _v2_artifact(db, 2, _hash(db, 2))
    assert semantic_drain._qc_seed(db, time.time(), limit=1) == 1
    assert _qc_job(db, 2) is not None        # newest artifact first
    # pending job for m2 is excluded from the window -> m1 seeds now
    assert semantic_drain._qc_seed(db, time.time(), limit=1) == 1
    assert _qc_job(db, 1) is not None
    db.close()


def test_failed_seed_does_not_starve_older_work(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1), _message(2)])
        _v2_artifact(db, 1, _hash(db, 1))
        _v2_artifact(db, 2, _hash(db, 2))
        semantic_drain._qc_seed(db, time.time(), limit=1)
        db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6")
        db.db.commit()
        assert semantic_drain._qc_seed(db, time.time(), limit=1) == 1
        assert _qc_job(db, 1)["state"] == "pending"
        assert _qc_job(db, 2)["state"] == "failed"
    finally:
        db.close()


def test_qc_requeues_changed_extraction_with_same_hash_and_version(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message()])
        _v2_artifact(db, 1, _hash(db))
        semantic.run_due(db, _cfg(extract_qc="annotate"), {"errors": []},
                         time.monotonic() + 60, jev_client=_FakeJev(choice="routine"))
        old_id = json.loads(_qc_artifact(db)["meta"])["source_artifact_id"]
        new_id = _v2_artifact(db, 1, _hash(db), content={"symptoms": []})
        assert new_id != old_id
        assert semantic_drain._qc_seed(db, time.time()) == 1
        payload = json.loads(_qc_job(db)["payload"])
        assert payload["source_artifact_id"] == new_id
    finally:
        db.close()


def test_qc_seed_and_drain_use_same_valid_source(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message()])
        source_id = _v2_artifact(db, 1, _hash(db))
        for content in ("broken json", "[]"):
            db.artifact_add("extract_llm", content, project_id=1, message_id=1,
                            meta={"hash": _hash(db),
                                  "extract_version": extract_llm.EXTRACT_VERSION})
        out = semantic.run_due(db, _cfg(extract_qc="annotate"), {"errors": []},
                               time.monotonic() + 60, jev_client=_FakeJev(choice="routine"))
        assert out["done"] == 1
        assert json.loads(_qc_artifact(db)["meta"])["source_artifact_id"] == source_id
        assert semantic_drain._qc_seed(db, time.time()) == 0
    finally:
        db.close()


def test_seed_repends_done_job_on_version_bump_same_hash(tmp_path):
    """F16: QC identity includes the extractor generation — a job whose
    payload predates 'ver' tracking re-pends even when the content hash
    is unchanged."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    semantic_drain._qc_seed(db, time.time())
    # simulate a job row written before 'ver' existed + done state
    db.db.execute("UPDATE fetch_jobs SET state='done',"
                  " payload=json_object('hash',"
                  " json_extract(payload,'$.hash'))"
                  " WHERE kind='extract_qc'")
    db.db.commit()
    assert semantic_drain._qc_seed(db, time.time()) == 1
    job = _qc_job(db)
    assert job["state"] == "pending" and job["attempts"] == 0
    assert json.loads(job["payload"])["ver"] == \
        extract_llm.EXTRACT_VERSION
    db.close()


def test_seed_skips_when_qc_exists_for_same_hash_and_version(tmp_path):
    """F16: a QC artifact from an OLDER extractor generation must not
    satisfy the existence check for the current one."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    h = _hash(db)
    _v2_artifact(db, 1, h)
    db.artifact_add("extract_qc", "{}", project_id=1, message_id=1,
                    meta={"hash": h,
                          "extract_version": extract_llm.EXTRACT_VERSION
                          - 1})
    assert semantic_drain._qc_seed(db, time.time()) == 1
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
    client = _FakeJev(choice="routine")
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
    client = _FakeJev(choice="routine")
    out = semantic.run_due(db, _cfg(), {"errors": []},
                           time.monotonic() + 60, jev_client=client)
    assert out["done"] == 0 and client.requests_made == 0
    assert _qc_job(db) is None
    db.close()


def test_process_qc_job_skips_aged_post(tmp_path):
    """A queued job whose post aged past the 60-day window completes
    without spending a Jev request or writing an artifact."""
    from semantic_policy import semantic_config
    db = _ledger(tmp_path)
    old = time.strftime("%Y-%m-%dT%H:%M:%S+09:00",
                        time.localtime(time.time() - 61 * 86400))
    db.save_messages([_message(1, posted_at=old)])
    _v2_artifact(db, 1, _hash(db))
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,"
        "state,next_try,created_at,updated_at) "
        "VALUES('extract_qc',1,1,'{}','pending',0,0,0)")
    db.db.commit()
    job = db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='extract_qc'").fetchone()
    scfg, _ = semantic_config(_cfg(extract_qc="annotate"))
    client = _FakeJev(choice="routine")
    out = semantic_drain._process_qc_job(
        db, scfg, job, client, time.monotonic() + 60)
    assert out == "done" and client.requests_made == 0
    assert _qc_artifact(db) is None
    db.close()


def test_qc_artifact_reports_item_coverage(tmp_path):
    """F17: qc=done must disclose how many extracted items were actually
    checked (cap + missing answers leave unchecked items)."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db),
                 content={"meds": [{"name": f"m{i}"}
                                   for i in range(20)],
                          "urgency": "routine"})
    client = _FakeJev(noul=0.9, choice="routine")
    out = semantic.run_due(db, _cfg(extract_qc="annotate"),
                           {"errors": []}, time.monotonic() + 60,
                           jev_client=client)
    assert out["done"] == 1
    content = json.loads(_qc_artifact(db)["content"])
    cov = content["coverage"]
    assert cov["checked"] == semantic_drain.QC_MAX_ITEMS + 1
    assert cov["total"] == 21
    assert cov["unchecked"] == 20 - semantic_drain.QC_MAX_ITEMS
    assert cov["capped"] is True
    assert cov["by_field"]["urgency"]["checked"] == 1
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
    _v2_artifact(db, 1, _hash(db), project_id=9)
    client = _FakeJev(choice="routine")
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
    client = _FakeJev(choice="routine")
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


def _drain_result(status="PENDING"):
    return {"summary": {"summary_id": "sum_x", "claims": [],
                        "audit_status": status},
            "findings": [{"code": "summary_unavailable"}],
            "repaired": False, "jev_requests": 0}


def test_write_result_is_idempotent_for_identical_outcome(tmp_path):
    """FIX-SD1: a deferred job re-derives the same outcome on every pass;
    the durable summary+audit pair must be recorded once, not once per
    attempt (production showed 3 identical PENDING rows in 13 min)."""
    db = _ledger(tmp_path)
    members = {7: {"revision": 3}}
    r = _drain_result()
    for _ in range(3):
        with db.db:
            semantic_drain._write_result(
                db, 1, 7, r, "fp1", members, "PENDING", "pol1", "shadow")
    rows = db.db.execute(
        "SELECT kind, COUNT(*) c FROM artifacts "
        "WHERE kind IN ('semantic_summary','semantic_audit') "
        "GROUP BY kind").fetchall()
    assert {x["kind"]: x["c"] for x in rows} == {
        "semantic_summary": 1, "semantic_audit": 1}
    db.close()


def test_write_result_records_status_transition(tmp_path):
    """Dedup keys on the outcome — a real status change must still
    append a new pair."""
    db = _ledger(tmp_path)
    members = {7: {"revision": 3}}
    with db.db:
        semantic_drain._write_result(
            db, 1, 7, _drain_result(), "fp1", members, "PENDING",
            "pol1", "shadow")
    r2 = _drain_result("PASS")
    r2["findings"] = []
    with db.db:
        semantic_drain._write_result(
            db, 1, 7, r2, "fp1", members, "PASS", "pol1", "shadow")
    rows = db.db.execute(
        "SELECT kind, COUNT(*) c FROM artifacts "
        "WHERE kind IN ('semantic_summary','semantic_audit') "
        "GROUP BY kind").fetchall()
    assert {x["kind"]: x["c"] for x in rows} == {
        "semantic_summary": 2, "semantic_audit": 2}
    db.close()


# ---------- queue fairness (T13) ----------

def test_semantic_jobs_outrank_qc_backfill(tmp_path):
    """Both kinds pending & ineligible -> the semantic job is claimed
    before the QC backfill inside the same max_jobs window."""
    from test_mcs_semantic import _llm
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db))
    semantic_drain._qc_seed(db, time.time())          # QC job first (job_id smaller)
    db.job_add("semantic", 1, 1, payload={
        "targets": [1], "origin": {"source": "history_import"}})
    client = _FakeJev(choice="routine")
    out = semantic.run_due(db, _cfg(extract_qc="annotate"),
                           {"errors": []}, time.monotonic() + 300,
                           jev_client=client, llm_fn=_llm, max_jobs=1)
    assert out["job_metrics"][0]["kind"] == "semantic"
    assert "extract_qc" not in out["done_by_kind"]
    assert _qc_job(db)["state"] == "pending"
    # Second pass: the QC backfill runs once semantic work is done.
    out2 = semantic.run_due(db, _cfg(extract_qc="annotate"),
                            {"errors": []}, time.monotonic() + 300,
                            jev_client=client, llm_fn=_llm, max_jobs=1)
    assert out2["job_metrics"][0]["kind"] == "extract_qc"
    assert out2["done_by_kind"] == {"extract_qc": 1}
    db.close()


def test_qc_questions_cover_vitals():
    """Vitals join the audit keyed by vital name — a mislabel
    (脈は48 -> bs:48) asks whether the contextual value is a blood glucose."""
    ex = {"vitals": {"bs": 48, "hr": 72}}
    questions, layout, ctx = semantic_drain._qc_questions(ex)
    v_layout = [e for e in layout if e[1] == "vitals"]
    assert sorted(e[2] for e in v_layout) == ["bs", "hr"]
    blob = json.dumps(questions, ensure_ascii=False)
    assert "血糖値" in blob and "脈拍" in blob
    assert ctx["v0"] and "bs" in ctx["v0"]


def test_qc_vital_text_stays_out_of_instructions():
    marker = 'SYNTHETIC_UNTRUSTED_VITAL'
    questions, _, context = semantic_drain._qc_questions({
        'vitals': {marker: marker, 'hr': marker}})
    assert marker not in json.dumps(questions)
    assert marker in json.dumps(context)


def test_qc_coverage_cap_includes_vitals(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    _v2_artifact(db, 1, _hash(db), content={
        "meds": [{"name": f"synthetic-{i}"} for i in range(10)],
        "vitals": {"bt": 36.5, "hr": 72, "rr": 16, "sbp": 120,
                   "dbp": 80, "spo2": 98, "bs": 100}})
    out = semantic.run_due(
        db, _cfg(extract_qc="annotate"), {"errors": []},
        time.monotonic() + 60, jev_client=_FakeJev(noul=0.9))
    assert out["done"] == 1
    coverage = json.loads(_qc_artifact(db)["content"])["coverage"]
    assert coverage["capped"] is True
    assert coverage["total"] == 17
    assert coverage["checked"] == semantic_drain.QC_MAX_ITEMS
    assert coverage["unchecked"] == 1
    # vitals audit before meds now (highest-NO_MATCH order) — the
    # overflow lands on a med, every vital is still checked.
    assert coverage["by_field"]["vitals"]["unchecked"] == 0
    assert coverage["by_field"]["meds"]["unchecked"] == 1
    db.close()


def test_qc_reserves_clinical_sections_and_deduplicates_events():
    _, layout, _ = semantic_drain._qc_questions({
        'events': ['visit'] * 30,
        'vitals': {'hr': 72, 'sbp': 120},
        'meds': [{'name': f'合成薬{i}'} for i in range(30)],
        'symptoms': [{'text': '合成症状'}],
        'labs': [{'name': '合成検査'}],
    })
    assert len(layout) == 16
    assert {x[1] for x in layout} == {'events', 'vitals', 'meds', 'symptoms', 'labs'}
    assert sum(x[1] == 'events' for x in layout) == 1


def test_qc_dedup_keeps_original_item_indices():
    _, layout, context = semantic_drain._qc_questions({
        "events": ["visit", "visit", "exam"],
        "vitals": {"hr": 72},
    })
    assert layout == [("v0", "vitals", "hr"),
                      ("e0", "events", 0), ("e1", "events", 2)]
    assert json.loads(context["e1"]) == "exam"
