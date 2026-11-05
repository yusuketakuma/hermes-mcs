"""Synthetic mcs semantic drain contracts with isolated shared fixtures."""
import mcs_util
import semantic_drain
import semantic_store
import json
import time
import semantic_jev as jev
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient, _seeded


def test_shadow_full_pipeline(tmp_path):
    db = _seeded(tmp_path)
    res = {"errors": []}
    fake = _FakeJev(choice_map={})   # claim-support default = 'supports'
    out = semantic_drain.run_due(db, _cfg("shadow"), res,
                           time.monotonic() + 300,
                           jev_client=fake, llm_fn=_llm)
    assert out["done"] == 1 and res["errors"] == []
    for kind in ("semantic_bundle", "semantic_assess", "semantic_facts",
                 "semantic_summary", "semantic_audit", "notify_plan"):
        assert db.db.execute("SELECT 1 FROM artifacts WHERE kind=?",
                             (kind,)).fetchone(), kind
    audit = db.artifacts("semantic_audit", message_id=1)[-1]
    assert json.loads(audit["meta"])["audit_status"] == "PASS"
    summ = json.loads(db.artifacts("semantic_summary",
                                   message_id=1)[-1]["content"])
    assert summ["claims"][0]["evidence_refs"]
    # shadow: outbox gets nothing (AT-049)
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 0
    assert db.job_state("semantic", 1, 1) == "done"
    # open loop created from the explicit_request fact (kind comes from
    # the llm mock — medication_event here, so none expected) — verify
    # no crash and idempotent re-run skips everything
    out2 = semantic_drain.run_due(db, _cfg("shadow"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 0
    db.close()


def test_shadow_no_outbox(tmp_path):
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    kinds = {r[0] for r in db.db.execute(
        "SELECT DISTINCT kind FROM notify_outbox")}
    assert kinds == {"new_messages"}
    db.close()


def test_daily_budget_exhausted(tmp_path):
    db = _seeded(tmp_path)
    db.artifact_add("semantic_usage", "{}", project_id=1, message_id=1,
                    meta={"jev_requests": 5})
    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("shadow", budget=5), res,
                           time.monotonic() + 60,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert out["budget_exhausted"] is True
    assert out["jev_skipped"].get("daily_budget") == 1
    assert "semantic: daily_budget_exhausted" not in res["errors"]
    db.close()


def test_off_flip_mid_drain(tmp_path):
    """cfg_path reload between jobs: flipping mode to off stops the
    next job — no external calls, pending rows preserved (AT-060)."""
    db = _ledger(tmp_path)
    p = _patient(db)
    for i in (10, 11):                      # two separate threads
        p.messages = [_message(i)]
        db.save_patient(p, notify={"source": "unread"}, semantic=True)
    cfgf = tmp_path / "cfg.json"
    cfgf.write_text(json.dumps(_cfg("shadow")))
    calls = {"n": 0}

    def flipping(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 1:
            cfgf.write_text(json.dumps(_cfg("off")))
        return _llm(*a, **kw)

    fake = _FakeJev()
    res = {"errors": []}
    semantic_drain.run_due(db, json.loads(cfgf.read_text()), res,
                     time.monotonic() + 300, jev_client=fake,
                     llm_fn=flipping, cfg_path=str(cfgf))
    pending = db.db.execute("SELECT count(*) FROM fetch_jobs "
                            "WHERE kind='semantic' AND state='pending'"
                            ).fetchone()[0]
    assert pending >= 1            # remaining work survives OFF
    db.close()


def test_long_body_chunked_fully(tmp_path):
    """§12.3/AT-017: a body past the old silent 4000-char cap must be
    fully processed — extractor chunks cover the tail, the summary
    prompt gets the whole body, and the ledger records chunk count."""
    db = _ledger(tmp_path)
    p = _patient(db)
    tail = "中止指示の記載"
    body = "冒頭の本文。" + "あ" * 4000 + "\n" + tail
    p.messages = [_message(1, body=body)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    seen = []

    def llm(prompt):
        seen.append(prompt)
        return _llm(prompt)

    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=llm)
    assert out["done"] == 1
    fact_prompts = [x for x in seen if "事実候補抽出器" in x]
    assert len(fact_prompts) >= 2
    assert any(tail in x for x in fact_prompts)
    assert any(tail in x for x in seen if "要約器" in x)
    meta = json.loads(
        db.artifacts("semantic_facts", message_id=1)[-1]["meta"])
    assert meta["chunks_total"] >= 2
    db.close()


def test_jev_usage_deltas_exact(tmp_path):
    """The daily budget ledger sums per-attempt deltas — it must equal
    the client's real request count including claim-audit and loop
    calls, not a cumulative counter snapshotted per artifact."""
    db = _seeded(tmp_path)
    fake = _FakeJev()
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=fake, llm_fn=_llm)
    assert fake.requests_made > 0
    assert semantic_store.jev_usage_today(db) == fake.requests_made
    db.close()


def test_chunk_failure_defers_without_partial_facts(tmp_path):
    """A chunk whose LLM response is missing/unparseable defers the
    job — the fingerprint-keyed facts artifact is never written from
    partial input, so the loss can't freeze into this generation."""
    db = _ledger(tmp_path)
    p = _patient(db)
    body = "先頭。" + "あ" * 4000 + "\n" + "末尾の記載。"
    p.messages = [_message(1, body=body)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    calls = {"n": 0}

    def flaky(prompt):
        if "事実候補抽出器" in prompt:
            calls["n"] += 1
            if calls["n"] == 2:
                return None           # chunk 2 fails
        return _llm(prompt)

    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=flaky)
    assert out["deferred"] == 1
    assert not db.artifacts("semantic_facts", message_id=1)
    # LLM back -> the whole body is re-extracted, all chunks covered
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    out2 = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 1
    meta = json.loads(db.artifacts("semantic_facts",
                                   message_id=1)[-1]["meta"])
    assert meta["chunks_total"] >= 2
    db.close()


def test_skipped_assess_artifact_dedup(tmp_path):
    """A durable Jev skip records ONE skipped artifact per
    generation — repeated drains must not stack identical rows."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    for _ in range(3):
        semantic_drain.run_due(db, _cfg("shadow", budget=0), res,
                         time.monotonic() + 300,
                         jev_client=None, llm_fn=_llm)
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
        db.db.commit()
    rows = db.artifacts("semantic_assess", message_id=1)
    assert len(rows) == 1
    assert json.loads(rows[0]["meta"])["technical_status"] == "skipped"
    db.close()


def test_daily_budget_binds_inside_job(tmp_path):
    """The daily cap is per-request, not per-job: mid-job claim/detail
    calls stop at the remaining budget instead of overshooting."""
    def post(body, timeout):
        answers = {}
        for qid, q in body["questions"].items():
            if q.get("type") == "choice":
                opts = list(q["criteria"])
                answers[qid] = {"type": "choice", "choice": opts[0],
                                "confidence": 0.9,
                                "probabilities":
                                {o: 1.0 / len(opts) for o in opts}}
            else:
                answers[qid] = {"type": "noul", "noul": 0.9}
        return 200, {}, json.dumps(
            {"usage": {"input_tokens": 1, "output_tokens": 1}, "model": jev.JEV_MODEL, "answers": answers}).encode()

    db = _seeded(tmp_path)
    client = jev.JevClient(api_key="k", post_fn=post)
    semantic_drain.run_due(db, _cfg("enforce", budget=3),
                     {"errors": []}, time.monotonic() + 300,
                     jev_client=client, llm_fn=_llm)
    assert client.requests_made == 3        # capped mid-job, not after
    job = db.db.execute("SELECT state,attempts FROM fetch_jobs "
                        "WHERE kind='semantic'").fetchone()
    assert job["state"] == "done"
    audits = db.artifacts("semantic_audit", message_id=1)
    # A finding from a completed Jev chunk remains a review outcome.
    assert json.loads(audits[-1]["meta"])["audit_status"] == "NEEDS_REVIEW"
    db.close()


def test_deferred_run_preserves_completed_results(tmp_path):
    """A mid-job deferral must still commit each completed target's
    outcome — otherwise the next run re-spends Jev calls AND silently
    resets the one-shot repair budget, which lives on the audit
    artifact (INV-10, §18.2)."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    p.messages[0].replies = [_message(2, parent=1, body="別の本文")]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    fixed = {"v": False}
    calls = {"bad": 0, "good": 0}

    def llm(prompt):
        if "要約器" in prompt:
            target = prompt.split("対象投稿:\n<<<\n")[1].split("\n>>>")[0]
            if "別の本文" in target:
                if not fixed["v"]:
                    return None          # mid-2's writer is down
                calls["good"] += 1
                return _llm(prompt)
            calls["bad"] += 1
            # reported_fact with no evidence -> repair -> still bad
            return json.dumps({"claims": [{
                "section": "medication", "text": "無根拠の断定",
                "claim_kind": "reported_fact", "fact_refs": []}],
                "limitations": []})
        return _llm(prompt)

    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=llm)
    assert out["deferred"] == 1
    audits = db.artifacts("semantic_audit", message_id=1)
    audits = sorted(audits, key=lambda row: row["artifact_id"])
    assert [json.loads(row["meta"])["audit_status"] for row in audits] == ["PENDING", "NEEDS_REVIEW"]
    assert any(finding["code"] == "claim_without_evidence"
               for finding in json.loads(audits[0]["content"])["findings"])
    meta = json.loads(audits[-1]["meta"])
    assert meta["audit_status"] == "NEEDS_REVIEW"
    assert meta["repair_count"] == 1      # budget spent, durably
    summary = db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                            (meta["summary_artifact_id"],)).fetchone()
    assert meta["summary_hash"] == mcs_util.payload_hash(json.loads(summary["content"]))
    assert len(db.artifacts("semantic_repair", message_id=1)) == 1
    # writer back -> rerun completes WITHOUT re-auditing mid-1
    fixed["v"] = True
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    out2 = semantic_drain.run_due(db, _cfg("enforce"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=llm)
    assert out2["done"] == 1
    assert calls["bad"] == 2   # initial + one repair, never re-run
    assert len(db.artifacts("semantic_audit", message_id=1)) == 2
    assert len(db.artifacts("semantic_summary", message_id=1)) == 2
    db.close()


def test_project_filter_defers_not_starves(tmp_path):
    """Out-of-scope jobs must not occupy every drain window — they are
    deferred so an in-scope job queued behind them still runs, while
    staying pending for a later project_ids change."""
    db = _ledger(tmp_path)
    for pid in range(2, 8):            # 6 out-of-scope projects
        px = _patient(db, pid=pid)
        px.messages = [_message(pid * 100, pid=pid)]
        db.save_patient(px, notify={"source": "unread"}, semantic=True)
    p1 = _patient(db, pid=1)
    p1.messages = [_message(100, pid=1)]
    db.save_patient(p1, notify={"source": "unread"}, semantic=True)
    cfg = _cfg("shadow", project_ids=[1])
    res = {"errors": []}
    # Refill skips deferred rows in the same window, reaching the scoped job.
    first = semantic_drain.run_due(db, cfg, res, time.monotonic() + 300,
                                  jev_client=_FakeJev(), llm_fn=_llm, max_jobs=4)
    assert first["done"] == 1
    assert first["deferred"] == 6
    out = semantic_drain.run_due(db, cfg, res, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm,
                           max_jobs=4)
    assert out["done"] == 0       # the completed job is not processed twice
    row = db.db.execute(
        "SELECT state FROM fetch_jobs WHERE kind='semantic' "
        "AND project_id=1").fetchone()
    assert row["state"] == "done"
    left = db.db.execute(
        "SELECT COUNT(*) c FROM fetch_jobs WHERE kind='semantic' "
        "AND state='pending'").fetchone()["c"]
    assert left == 6              # filtered jobs stay pending/resumable
    db.close()


def test_oversize_marker_survives_storage(tmp_path):
    """The stored summary drops _-prefixed keys; the oversize marker
    must persist in meta so a later re-audit still blocks PASS instead
    of approving an empty stub."""
    big = "あ" * 30000
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1, body=big)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    def llm(prompt):
        assert "要約器" not in prompt
        return _llm(prompt)

    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=llm)
    meta = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='semantic_summary'"
    ).fetchone()["meta"])
    assert meta["input_oversize"] is True
    audit = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='semantic_audit'"
    ).fetchone()["meta"])
    assert audit["audit_status"] == "NEEDS_REVIEW"
    assert not db.artifacts("semantic_repair")
    db.close()
