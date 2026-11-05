"""Synthetic mcs semantic source jobs contracts with isolated shared fixtures."""
import mcs_contract
import semantic_drain
import semantic_policy
import semantic_store
import json
import time
import pytest
from inference_version import CURRENT_VERSION, VERSION_KEY
import ledger
import mcs_view
import semantic
from semantic_testkit import BODY, _FakeJev, _cfg, _ledger, _llm, _message, _patient, _seeded
from mcs_semantic_testkit import _cfg_path as _cfg_path, _finish_reprocess_for_loop_fixture as _finish_reprocess_for_loop_fixture


# ---------- bundle / fingerprint ----------

def test_ingest_seeds_semantic_job(tmp_path):
    db = _seeded(tmp_path)
    row = db.db.execute(
        "SELECT kind,project_id,message_id,payload,state FROM fetch_jobs "
        "WHERE kind='semantic'").fetchone()
    assert row and row["message_id"] == 1 and row["state"] == "pending"
    pl = json.loads(row["payload"])
    assert set(pl["targets"]) == {1, 2}
    # notify intent and job share the transaction — both must exist
    assert db.db.execute("SELECT 1 FROM notify_outbox "
                         "WHERE kind='new_messages'").fetchone()
    # same event again -> merge, not a second job row
    db.semantic_seed(1, [2], {"source": "replay"})
    assert db.db.execute("SELECT count(*) FROM fetch_jobs "
                         "WHERE kind='semantic'").fetchone()[0] == 1
    db.close()


def test_fingerprint_covers_revisions(tmp_path):
    db = _seeded(tmp_path)
    b1 = semantic_store.thread_bundle(db.db, 1, 1)
    fp1 = b1["source_fingerprint"]
    assert semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"] == fp1
    db.save_messages([_message(2, parent=1, body="返信を編集した本文")])
    b2 = semantic_store.thread_bundle(db.db, 1, 1)
    assert b2["source_fingerprint"] != fp1       # context edit -> new gen
    assert semantic_store.bundle_fingerprint(
        b2["members"], model="jev-x") != b2["source_fingerprint"]
    db.close()


# ---------- pipeline ----------

def test_off_no_seed_no_drain(tmp_path):
    db = _seeded(tmp_path)
    assert semantic.seed(db, 1, cfg_path="/nonexistent.json") is None
    res = {"errors": []}
    out = semantic_drain.run_due(db, {"semantic": {"mode": "off"}}, res,
                           time.monotonic() + 60)
    assert out == {"mode": "off", "done": 0, "deferred": 0,
                   "failed": 0, "left": None, "budget_exhausted": False,
                   "progressed": 0}
    assert db.job_pending("semantic", 1, 1)["state"] == "pending"
    db.close()


def test_stale_generation_not_promoted(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    real_bundle = semantic_store.thread_bundle
    real_process = semantic_drain._process_job
    active = [False]

    def process(*args, **kwargs):
        active[0] = True
        return real_process(*args, **kwargs)

    def drifting_bundle(ledg, pid, root, targets=None):
        # second call (the generation guard) sees a 'newer' bundle
        b = real_bundle(ledg, pid, root, targets)
        if active[0] and drifting_bundle.calls > 0 and b:
            b["source_fingerprint"] = "x" * 64
        if active[0]:
            drifting_bundle.calls += 1
        return b

    drifting_bundle.calls = 0
    monkeypatch.setattr(semantic_store, "thread_bundle", drifting_bundle)
    monkeypatch.setattr(semantic_drain, "_process_job", process)
    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["deferred"] == 1
    metas = [json.loads(a["meta"]) for a in
             db.artifacts("semantic_audit", message_id=1)]
    assert metas and metas[-1]["audit_status"] == "STALE"
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 0
    db.close()


def test_replay_dedup_current_fp(tmp_path):
    db = _seeded(tmp_path)
    res = {"errors": []}
    fake = _FakeJev()
    semantic_drain.run_due(db, _cfg("shadow"), res, time.monotonic() + 300,
                     jev_client=fake, llm_fn=_llm)
    n = fake.requests_made
    (tmp_path / "cfg.json").write_text(json.dumps(_cfg("shadow")))
    semantic.seed(db, 1, cfg_path=str(tmp_path / "cfg.json"))
    semantic_drain.run_due(db, _cfg("shadow"), res, time.monotonic() + 300,
                     jev_client=fake, llm_fn=_llm)
    # same fingerprint -> current artifacts satisfy the job; Jev is not
    # re-queried for already-assessed targets (AT-059)
    assert fake.requests_made <= n
    db.close()


def test_loop_candidate_no_request_write(tmp_path):
    db = _seeded(tmp_path)

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return json.dumps({"facts": [{
                "statement": "薬局への確認依頼の記載がある",
                "kind": "explicit_request", "status": "not_stated",
                "polarity": "affirmed", "time_text": None,
                "quantity": None,
                "evidence_quote": "確認お願いします"}]})
        return _llm(prompt)

    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=llm)
    cands = db.artifacts("loop_candidate", message_id=1)
    assert len(cands) == 1
    cand = json.loads(cands[0]["content"])
    assert cand["state"] == "PROPOSED"
    # INV-11/19: the loop pipeline wrote NO request/command rows
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM command_receipts"
                         ).fetchone()[0] == 0
    db.close()


def test_loop_scan_not_capped(tmp_path):
    """§17.2 regression: every open candidate is evaluated against new
    targets — not just the newest 10 — and re-runs add no duplicates."""
    db = _seeded(tmp_path)
    db.save_messages([_message(i, parent=1, body=f"確認事項{i}", unread=False)
                      for i in range(20, 32)])
    _finish_reprocess_for_loop_fixture(db)
    # Reproduce one current target against previously observed sibling origins.
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET payload=json_set(payload, '$.targets', json('[1]')) "
                      "WHERE kind='semantic' AND project_id=1 AND message_id=1")
    members = {m["message_id"]: m for m in
               semantic_store.thread_bundle(db.db, 1, 1)["members"]}
    for i in range(20, 32):          # 12 sourced same-thread open candidates
        db.artifact_add("loop_candidate", json.dumps({
            "loop_id": f"loop_{i}", "project_id": 1,
            "account_scope": "mcs", "root_id": 1,
            "kind": "pending_item", "description": f"item{i}",
            "origin": {"message_id": i, "revision": members[i]["revision"],
                       "evidence_refs": [f"e{i}"],
                       "evidence": {"quote": f"確認事項{i}",
                                    "span_start": 0, "span_end": len(f"確認事項{i}")}},
            "assignee_text": None, "due_text": None,
            "state": "PROPOSED", "history": []}),
            project_id=1, message_id=i, meta={VERSION_KEY: CURRENT_VERSION, "candidate_fp": f"fp{i}",
                "fingerprint": semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"],
                "policy_fingerprint": semantic_policy.policy_fingerprint(semantic_policy.semantic_config(_cfg())[0])})
    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("shadow"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert out["inference_reprocess"]["status"] == "waiting"
    evs = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='loop_event'"
    ).fetchall()
    origins = {json.loads(e["content"])["loop_origin_id"] for e in evs}
    assert origins == set(range(20, 32))
    n0 = len(evs)
    # Revisit the same target and generation without another relation request.
    jev_client = _FakeJev()
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
    scfg = semantic_policy.semantic_config(_cfg("shadow"))[0]
    created, complete = semantic_drain.update_loops(
        db, 1, bundle, {}, jev_client, scfg, time.monotonic() + 300)
    assert created == 0 and complete and not jev_client.calls
    assert db.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind='loop_event'"
    ).fetchone()["c"] == n0
    db.close()


def test_sem_block_drops_stale_revision(tmp_path, monkeypatch):
    """INV-15: a PASS summary describes one revision — after a body
    edit the audited block must not attach to the new revision."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    import notify_flush
    monkeypatch.setattr(
        notify_flush, "_config",
        lambda: _cfg("enforce"))
    ev = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    content, _ = notify_flush._format_event(db, ev)
    assert "要約（自動検査済）" in content
    # edit the parent body -> new content_hash -> block must drop
    p = _patient(db)
    p.messages = [_message(1, body=BODY + "（追記あり）", unread=False)]
    db.save_patient(p)
    ev = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    content, _ = notify_flush._format_event(db, ev)
    assert "要約（自動検査済）" not in content
    db.close()


def test_history_import_seeds_semantic(tmp_path):
    """History-imported messages enter the semantic queue like every
    other ingestion path (INV-06) — a backfill must not silently skip
    evaluation."""
    import job_ops
    db = _ledger(tmp_path)
    _patient(db)
    db.job_add("history", 1, payload={"since": 0, "pages": 1})

    class Adapter:
        def fetch_history(self, *a, **k):
            return mcs_contract.MessageBatch(
                [_message(5)], pages=1, reached=True)

        def fetch_thread(self, *a, **k):
            return []

    res = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, res,
                             time.monotonic() + 60, semantic=True)
    row = db.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()
    assert row and 5 in json.loads(row["payload"])["targets"]
    # and without the flag it stays silent
    db2 = ledger.Ledger(str(tmp_path / "ledger2.db"))
    _patient(db2)
    db2.job_add("history", 1, payload={"since": 0, "pages": 1})
    job_ops.run_history_jobs(Adapter(), db2, {"errors": []},
                             time.monotonic() + 60, semantic=False)
    assert db2.db.execute("SELECT COUNT(*) c FROM fetch_jobs "
                          "WHERE kind='semantic'").fetchone()["c"] == 0
    db.close()
    db2.close()


# ---------- notification eligibility (INV-20 / AT-055) ----------

def test_history_import_never_notifies(tmp_path):
    """AT-055: a PASS-audited summary on history-imported input is an
    artifact, never a notification — imports must not mint arrival
    eligibility (INV-20)."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(50)], project_id=1, semantic=True)
    out = semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert json.loads(db.artifacts("semantic_audit", message_id=50)[-1]
                      ["meta"])["audit_status"] == "PASS"
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox "
                         "WHERE kind='semantic_notice'"
                         ).fetchone()["c"] == 0
    db.close()


def test_arrival_merge_keeps_eligibility(tmp_path):
    """A pending arrival-seeded job merged with a history re-seed must
    still notify — eligibility lives in the stored origin event, not
    the overwritten payload origin. Per-target: the history-merged
    post itself produces artifacts but no notice (INV-20)."""
    db = _seeded(tmp_path)   # notify-path seed for messages 1,2
    db.save_messages([_message(3, parent=1, body="取り込み追加分")],
                     project_id=1, semantic=True)   # history merge
    pl = json.loads(db.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()["payload"])
    assert pl.get("eligible") is True
    out = semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    rows = db.db.execute("SELECT payload FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchall()
    notified = {json.loads(r["payload"])["target_message_id"]
                for r in rows}
    assert notified == {1, 2}   # only the arrival-covered posts notify
    assert all(json.loads(r["payload"])["src_event_id"] is not None
               for r in rows)
    # the merged history post was still fully evaluated
    assert db.artifacts("semantic_audit", message_id=3)
    db.close()


def test_backfill_seeds_read_arrivals(tmp_path):
    """WP-02 coverage: the notify path must seed EVERY newly stored
    message — including posts that arrived already-read, which are
    exactly what backfill exists to catch. Notification eligibility
    stays separate (INV-20): the read-arrival produces artifacts but
    never a notice."""
    db = _ledger(tmp_path)
    _patient(db)
    new = db.save_messages(
        [_message(60, unread=False), _message(61, unread=True)],
        project_id=1, notify={"source": "history"}, semantic=True)
    assert set(new) == {60, 61}
    covered = set()
    for r in db.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind='semantic'"):
        covered.update(json.loads(r["payload"])["targets"])
    assert covered == {60, 61}
    out = semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 2
    assert db.artifacts("semantic_audit", message_id=60)
    notified = {json.loads(r["payload"])["target_message_id"]
                for r in db.db.execute(
                    "SELECT payload FROM notify_outbox "
                    "WHERE kind='semantic_notice'")}
    assert notified == {61}     # only the arrival-covered post notifies
    db.close()


def test_reply_save_seeds_read_replies(tmp_path):
    """The reply-job path has the same widened coverage — a reply
    persisted already-read is still evaluation input."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(70)], project_id=1)
    db.save_thread_replies(
        [_message(71, parent=70, unread=False),
         _message(72, parent=70, unread=True)],
        project_id=1, notify={"source": "reply_job"}, semantic=True)
    covered = set()
    for r in db.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind='semantic'"):
        covered.update(json.loads(r["payload"])["targets"])
    assert covered == {71, 72}
    db.close()


def test_loop_candidate_records_history(tmp_path):
    """A created candidate carries its PROPOSED history entry, the
    snapshot view derives later transitions from relation events
    (§17.3), and an already-absolute time_text populates occurred_at."""
    db = _seeded(tmp_path)

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return json.dumps({"facts": [{
                "statement": "確認依頼が未回答", "kind": "pending_item",
                "status": "not_stated", "polarity": "affirmed",
                "time_text": "2026-09-20", "quantity": None,
                "evidence_quote": "確認お願いします"}]})
        return _llm(prompt)

    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=llm)
    assert out["done"] == 1
    cands = db.artifacts("loop_candidate", project_id=1)
    assert cands
    cand = json.loads(cands[0]["content"])
    assert cand["history"] and cand["history"][0]["state"] == "PROPOSED"
    assert cand["history"][0]["trigger_message_id"] in (1, 2)
    facts = json.loads(
        db.artifacts("semantic_facts", message_id=1)[-1]["content"])
    assert facts["facts"][0]["occurred_at"] == "2026-09-20"
    # a completion_report relation derives the RESOLUTION_CANDIDATE
    # transition in the view — the stored artifact stays immutable
    db.artifact_add("loop_event", json.dumps({
        "loop_artifact_id": cands[0]["artifact_id"],
        "trigger_message_id": 2,
        "origin_revision": cand["origin"]["revision"],
        "trigger_revision": next(m["revision"] for m in
            semantic_store.thread_bundle(db.db, 1, 1)["members"] if m["message_id"] == 2),
        "root_id": 1,
        "relation": "completion_report"}), project_id=1, message_id=2,
        meta={"fingerprint": semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"],
              "policy_fingerprint": semantic_policy.policy_fingerprint(semantic_policy.semantic_config(_cfg())[0])})
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snaps"))
    view = mcs_view.View(snap)
    res = view.read("loops", project=1)
    item = next(i for i in res["items"]
                if i["artifact_id"] == cands[0]["artifact_id"])
    assert item["effective_state"] == "RESOLUTION_CANDIDATE"
    assert [h["state"] for h in item["history"]] == [
        "PROPOSED", "RESOLUTION_CANDIDATE"]
    db.close()


def test_replay_seed_records_source(tmp_path):
    """seed() must store provenance in the same shape ingest seeds use
    ({"source": ...}) — a nested {"origin": ...} wrapper silently
    dropped the replay label, making the job's descent uninspectable."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    db.save_patient(p)               # bare store: no notify, no seed
    root = semantic.seed(db, 1, cfg_path=_cfg_path(tmp_path, "shadow"))
    assert root == 1
    pl = json.loads(db.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'")
        .fetchone()["payload"])
    assert pl["origin"]["source"] == "replay"
    assert pl["origin"]["notification_free"] is True
    assert "eligible" not in pl      # replay is never notify-eligible
    db.close()


def test_eligible_priority_survives_compact_payload(tmp_path):
    """Drain ordering must not depend on json.dumps whitespace — a
    payload serialised with compact separators still ranks an
    arrival-descended job ahead of import/replay seeds."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(10), _message(20)], project_id=1)
    db.job_add("semantic", 1, 10, payload={
        "targets": [10], "origin": {"source": "history_import"}})
    now = time.time()
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
        "payload,state,next_try,created_at,updated_at) "
        "VALUES('semantic',1,20,NULL,?,'pending',0,?,?)",
        (json.dumps({"targets": [20],
                     "origin": {"source": "unread", "event_id": 1},
                     "eligible": True}, separators=(",", ":")),
         now, now))
    db.db.commit()
    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm,
                           max_jobs=1)
    assert out["done"] == 1
    rows = {r["message_id"]: r["state"] for r in db.db.execute(
        "SELECT message_id,state FROM fetch_jobs WHERE kind='semantic'")}
    assert rows[20] == "done" and rows[10] == "pending"
    db.close()


def test_empty_targets_root_is_target(tmp_path):
    """A job row without payload.targets still evaluates the root as a
    target — including the loop-relation pass, which keys on member
    role and was silently skipped for this edge."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1), _message(9, parent=1)]
    db.save_patient(p)
    db.job_add("semantic", 1, 1, payload={VERSION_KEY: CURRENT_VERSION})
    _finish_reprocess_for_loop_fixture(db)
    # This current-generation synthetic job deliberately lacks the optional key.
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET payload=json_remove(payload, '$.targets') "
                      "WHERE kind='semantic' AND project_id=1 AND message_id=1")
    source = next(m for m in semantic_store.thread_bundle(db.db, 1, 1)["members"]
                  if m["message_id"] == 9)
    db.artifact_add("loop_candidate", json.dumps({
        "loop_id": "loop_x", "project_id": 1, "kind": "pending_item",
        "description": "確認依頼が未回答",
        "account_scope": "mcs", "root_id": 1,
        "origin": {"message_id": 9, "revision": source["revision"],
                   "evidence": {"quote": BODY, "start_codepoint": 0,
                                "end_codepoint": len(BODY)}},
        "assignee_text": None,
        "due_text": None, "state": "PROPOSED", "history": []}),
        project_id=1, message_id=9, meta={
            VERSION_KEY: CURRENT_VERSION,
            "fingerprint": semantic_store.thread_bundle(db.db, 1, 1)["source_fingerprint"],
            "policy_fingerprint": semantic_policy.policy_fingerprint(semantic_policy.semantic_config(_cfg())[0])})
    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert out["inference_reprocess"]["status"] == "waiting"
    job = db.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert "targets" not in json.loads(job["payload"])
    assert db.artifacts("semantic_summary", message_id=1)
    evs = db.artifacts("loop_event", project_id=1)
    assert evs and json.loads(
        evs[0]["content"])["trigger_message_id"] == 1
    db.close()


def test_edit_reseeds_semantic(tmp_path):
    """An edited stored message is not 'new' but it IS new input: the
    winning content_hash changes, the fingerprint moves, and a semantic
    job must be re-seeded instead of leaving stale artifacts forever."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db.job_state("semantic", 1, 1) == "done"
    # same id, edited body, already-read: no notify ids, but the
    # changed revision must still seed evaluation
    db.save_messages([_message(1, body=BODY + "（編集）", unread=False)],
                     project_id=1, notify={"source": "history"},
                     semantic=True)
    assert db.job_state("semantic", 1, 1) == "pending"
    db.close()


def test_identical_resave_does_not_reseed(tmp_path):
    """An unchanged re-save must not burn a re-evaluation — only a real
    revision change re-seeds."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db.job_state("semantic", 1, 1) == "done"
    db.save_messages([_message(1, unread=False)], project_id=1,
                     notify={"source": "history"}, semantic=True)
    assert db.job_state("semantic", 1, 1) == "done"
    db.close()


def test_replay_respects_project_scope(tmp_path):
    """--replay outside project_ids must not seed — it would only churn
    in drain-time defers forever."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1)], project_id=1)
    cfgf = tmp_path / "cfg.json"
    cfgf.write_text(json.dumps({"semantic": {"mode": "shadow",
                                             "project_ids": [2]}}))
    assert semantic.seed(db, 1, cfg_path=str(cfgf)) is None
    assert db.job_state("semantic", 1, 1) is None
    db.close()


def test_transition_fences_null_payload_and_checks_current_payload_cas(tmp_path):
    # NULL normalizes to an empty legacy payload, but the version fence
    # prevents a worker from mutating that older or damaged row.
    from dataclasses import replace
    import semantic_runtime
    from inference_version import CURRENT_VERSION, VERSION_KEY
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,"
        "next_try,created_at,updated_at) VALUES('semantic',1,7,NULL,"
        "'pending',0,0,0)")
    db.db.commit()
    row = db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
    token = semantic_runtime.JobToken.from_row(row)
    assert token.payload_raw == "{}"
    assert semantic_runtime.transition_tx(db, token, "failed") is False
    db.db.commit()
    assert db.db.execute(
        "SELECT state FROM fetch_jobs WHERE job_id=?",
        (row["job_id"],)).fetchone()["state"] == "pending"
    assert db.db.execute("SELECT payload FROM fetch_jobs WHERE job_id=?",
                         (row["job_id"],)).fetchone()["payload"] is None
    # A current-generation row must still require an exact CAS match.
    payload = json.dumps({VERSION_KEY: CURRENT_VERSION, "x": 1})
    db.db.execute(
        "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,"
        "next_try,created_at,updated_at) VALUES('semantic',1,8,"
        "?,'pending',0,0,0)", (payload,))
    db.db.commit()
    row2 = db.db.execute(
        "SELECT * FROM fetch_jobs WHERE message_id=8").fetchone()
    token2 = semantic_runtime.JobToken.from_row(row2)
    assert semantic_runtime.transition_tx(db, replace(token2, project_id=2), "failed") is False
    assert semantic_runtime.transition_tx(db, replace(token2, message_id=7), "failed") is False
    assert semantic_runtime.transition_tx(db, token2, "failed") is True
    db.db.commit()
    # but a token built for a DIFFERENT payload must not fire on it
    stale = semantic_runtime.JobToken.from_row(
        {**dict(row2), "payload": json.dumps({VERSION_KEY: CURRENT_VERSION, "x": 2})})
    db.db.execute(
        "UPDATE fetch_jobs SET state='pending' WHERE message_id=8")
    db.db.commit()
    assert semantic_runtime.transition_tx(db, stale, "done") is False
    db.close()


@pytest.mark.parametrize("replacement_state", ["pending", "failed"])
def test_revive_preserves_job_changed_after_selection(
        tmp_path, monkeypatch, replacement_state):
    import semantic_drain
    db = _seeded(tmp_path)
    try:
        now = time.time()
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=2,updated_at=? "
                          "WHERE kind='semantic'", (now - 25000,))
        old = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        replacement = json.dumps({"targets": [1], "generation": "new-synthetic-generation",
                                  "retry_command_id": "new-synthetic-receipt"})
        original_loads = json.loads
        changed = False

        def loads(raw, *args, **kwargs):
            nonlocal changed
            if raw == old["payload"] and not changed:
                changed = True
                with db.db:
                    db.db.execute("UPDATE fetch_jobs SET state=?,payload=?,attempts=7,updated_at=? "
                                  "WHERE job_id=?",
                                  (replacement_state, replacement, now, old["job_id"]))
            return original_loads(raw, *args, **kwargs)

        monkeypatch.setattr(semantic_drain.json, "loads", loads)
        out = semantic_drain.revive_failed(db, now)
        current = db.db.execute("SELECT * FROM fetch_jobs WHERE job_id=?",
                                (old["job_id"],)).fetchone()
        assert changed
        assert current["payload"] == replacement
        assert current["state"] == replacement_state
        assert current["attempts"] == 7 and current["updated_at"] == now
        assert out["revived"] == 0
    finally:
        db.close()
