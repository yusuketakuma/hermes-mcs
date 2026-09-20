"""Phase-J semantic layer contracts — mock Jev/LLM, synthetic SQLite.

Coverage map (spec MCS-REFACTOR-FIRST-20260920 AT ids):
  AT-011 qid scope pinning        -> test_registry_scope_is_explicit
  AT-012 noul needs no confidence -> test_noul_without_confidence_ok
  AT-013 type confusion rejected  -> test_noul_type_confusion_rejected
  AT-014 choice contract          -> test_choice_validation
  AT-015 retryable statuses       -> test_retryable_status_backoff
  AT-016 contract/auth no retry   -> test_nonretryable_fail_fast
  AT-064 fixed model              -> test_model_mismatch_rejected
  AT-019 bundle build             -> test_bundle_fixed_shape
  AT-035 stale input demotion     -> test_stale_generation_not_promoted
  AT-049 shadow never outbox      -> test_shadow_no_outbox
  AT-053 OFF stops work           -> test_off_no_seed_no_drain
  AT-059 replay idempotent        -> test_replay_dedup_current_fp
  AT-067 budget + flag validation -> test_config_fail_closed / budget
  INV-06  job seeded in same tx   -> test_ingest_seeds_semantic_job
  INV-07  evidence enforcement    -> test_claim_without_evidence_audit
  INV-10  repair at most once     -> test_repair_once_then_needs_review
  INV-11  loops never auto-request-> test_loop_candidate_no_request_write
  INV-15  fingerprint discipline  -> test_fingerprint_covers_revisions
  INV-16  shadow artifact kinds   -> test_shadow_full_pipeline
  AT-065  snapshot readers intact -> test_snapshot_view_semantic
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import ledger
import mcs_adapter
import mcs_view
import semantic
import semantic_jev as jev

BODY = "明日からカロナール300mgを1日3回に変更します。確認お願いします。"


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, pid=1, parent=None, body=BODY, unread=True):
    return mcs_adapter.Message(
        message_id=mid, project_id=pid, parent_id=parent,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="",
        posted_at="2026-09-19T00:00:00+09:00",
        body_html=f"<p>{body}</p>", body_state="full",
        is_unread=unread, reply_count=0)


def _patient(db, pid=1):
    p = SimpleNamespace(project_id=pid, project_type="medical",
                        patient_name="テスト患者", disease="",
                        station_name="", url="https://www.medical-care."
                        "net/projects/medical/%d" % pid,
                        fetch_state="complete", fetch_reason=None,
                        messages=[])
    db.save_patient(p)
    return p


def _cfg(mode="shadow", **kw):
    return {"semantic": {"mode": mode,
                         "daily_request_budget": kw.pop("budget", 50),
                         **kw}}


def _cfg_path(tmp_path, mode):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"semantic": {"mode": mode,
                                          "daily_request_budget": 50}}))
    return str(p)


class _FakeJev:
    """Deterministic evaluate(): noul=0.9, choice=first option."""
    def __init__(self, noul=0.9, choice_map=None, error=None):
        self.requests_made = 0
        self.noul = noul
        self.choice_map = choice_map or {}
        self.error = error
        self.calls = []

    def evaluate(self, state, questions, deadline):
        self.requests_made += 1
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        out = {}
        for qid, q in questions.items():
            if q.get("type") == "choice":
                opts = list(q.get("options") or {})
                pick = self.choice_map.get(qid, opts[0] if opts else None)
                out[qid] = {"type": "choice", "choice": pick,
                            "confidence": 0.9,
                            "distribution": {o: 1.0 / len(opts)
                                             for o in opts}}
            else:
                out[qid] = {"type": "noul", "noul": self.noul}
        return {"answers": out, "model": jev.JEV_MODEL}


def _llm(prompt):
    if "要約器" in prompt:
        return json.dumps({"claims": [{
            "section": "medication",
            "text": "カロナールを300mg×3回に変更する記載がある",
            "claim_kind": "reported_fact", "fact_refs": [0],
            "status": "planned", "polarity": "affirmed"}],
            "limitations": ["採血結果の記載なし"]})
    if "事実候補抽出器" in prompt:
        return json.dumps({"facts": [{
            "statement": "用量変更の記載がある",
            "kind": "medication_event",
            "status": "planned", "polarity": "affirmed",
            "time_text": "明日から", "quantity": "300mg",
            "evidence_quote": "カロナール300mgを1日3回に変更"}]})
    return None


# ---------- Jev client contract ----------

def test_registry_scope_is_explicit():
    assert len(jev.PROPOSITIONS) == 12
    for pid, p in jev.PROPOSITIONS.items():
        assert pid.startswith("P") and p["label_ja"]
        # instruction names its target — a renamed key can never
        # silently re-scope the evaluation (AT-011)
        assert "state.target" in p["instructions"]
        assert p["true"] and p["false"]


def test_noul_without_confidence_ok():
    q = {"q1": jev.noul_question("i", "t", "f")}
    out = jev.validate_answers(
        {"model": jev.JEV_MODEL, "answers": {"q1": {"noul": 0.42}}}, q,
        jev.JEV_MODEL)
    assert out["answers"]["q1"]["noul"] == 0.42


def test_noul_type_confusion_rejected():
    q = {"q1": jev.noul_question("i", "t", "f")}
    for bad in ({"noul": True}, {"noul": "0.9"}, {"noul": None},
                {"noul": float("nan")}, {"noul": 1.5}, {}, "x"):
        with pytest.raises(jev.JevError) as ei:
            jev.validate_answers({"model": jev.JEV_MODEL,
                                  "answers": {"q1": bad}}, q,
                                 jev.JEV_MODEL)
        assert ei.value.kind == "protocol_error"


def test_choice_validation():
    q = {"q1": jev.choice_question("i", {"a": "A", "b": "B"})}
    good = {"q1": {"choice": "a", "confidence": 0.7,
                   "distribution": {"a": 0.7, "b": 0.3}}}
    out = jev.validate_answers({"model": jev.JEV_MODEL,
                                "answers": good}, q, jev.JEV_MODEL)
    assert out["answers"]["q1"]["choice"] == "a"
    for bad in ({"choice": "z", "confidence": 0.7,
                 "distribution": {"a": 0.7, "b": 0.3}},
                {"choice": "a", "confidence": 2.0,
                 "distribution": {"a": 0.7, "b": 0.3}},
                {"choice": "a", "confidence": 0.7,
                 "distribution": {"a": 0.7, "b": 0.7}},
                {"choice": "a", "confidence": 0.7},
                {"choice": "a"},
                ):
        with pytest.raises(jev.JevError):
            jev.validate_answers({"model": jev.JEV_MODEL,
                                  "answers": {"q1": bad}}, q,
                                 jev.JEV_MODEL)


def test_answer_set_mismatch_rejected():
    q = {"a": jev.noul_question("i", "t", "f"),
         "b": jev.noul_question("i", "t", "f")}
    for bad in ({"a": {"noul": 0.5}},
                {"a": {"noul": 0.5}, "b": {"noul": 0.5},
                 "c": {"noul": 0.1}}):
        with pytest.raises(jev.JevError, match="protocol_error"):
            jev.validate_answers({"model": jev.JEV_MODEL,
                                  "answers": bad}, q, jev.JEV_MODEL)


def test_model_mismatch_rejected():
    q = {"a": jev.noul_question("i", "t", "f")}
    with pytest.raises(jev.JevError, match="model_mismatch"):
        jev.validate_answers(
            {"model": "jev-latest",
             "answers": {"a": {"noul": 0.5}}}, q, jev.JEV_MODEL)


def test_retryable_status_backoff():
    calls = []

    def post(body, timeout):
        calls.append(1)
        if len(calls) == 1:
            return 429, {"Retry-After": "0"}, b"{}"
        return 200, {}, json.dumps({
            "model": jev.JEV_MODEL,
            "answers": {"a": {"noul": 0.8}}}).encode()

    c = jev.JevClient(api_key="k", post_fn=post, max_attempts=3)
    out = c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                     time.monotonic() + 30)
    assert out["answers"]["a"]["noul"] == 0.8 and len(calls) == 2


def test_nonretryable_fail_fast():
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 422, {}, b"{}"

    c = jev.JevClient(api_key="k", post_fn=post)
    with pytest.raises(jev.JevError, match="contract_error"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)
    assert len(calls) == 1          # contract errors never blind-retry

    def post401(body, timeout):
        calls.append(1)
        return 401, {}, b"{}"

    c = jev.JevClient(api_key="k", post_fn=post401)
    with pytest.raises(jev.JevError, match="auth_error"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)


def test_no_api_key():
    c = jev.JevClient(api_key=None, post_fn=lambda b, t: (200, {}, b""))
    with pytest.raises(jev.JevError, match="no_api_key"):
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)


def test_verdict_thresholds():
    assert jev.verdict_for(0.9) == "MATCH"
    assert jev.verdict_for(0.5) == "UNDETERMINED"
    assert jev.verdict_for(0.1) == "NO_MATCH"


# ---------- config gate ----------

def test_config_fail_closed():
    scfg, errs = semantic.semantic_config({})
    assert scfg["mode"] == "off" and errs == []
    scfg, errs = semantic.semantic_config({"semantic": "yes"})
    assert scfg["mode"] == "off" and errs
    scfg, errs = semantic.semantic_config(
        {"semantic": {"mode": "on", "daily_request_budget": -5,
                      "project_ids": "all"}})
    assert scfg["mode"] == "off"
    assert "config: semantic_mode_invalid" in errs
    assert "config: semantic_daily_request_budget_invalid" in errs
    assert "config: semantic_project_ids_invalid" in errs
    scfg, _ = semantic.semantic_config(
        {"semantic": {"mode": "enforce", "model": "jev-latest"}})
    assert scfg["mode"] == "off"    # non-fixed model -> refuses stage


# ---------- bundle / fingerprint ----------

def _seeded(tmp_path):
    """Patient + unread parent + reply, saved through the notify path
    with semantic=True — produces one pending kind='semantic' job."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    p.messages[0].replies = [_message(2, parent=1)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


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


def test_no_semantic_flag_no_job(tmp_path):
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    db.save_patient(p, notify={"source": "unread"})   # semantic=False
    assert db.db.execute("SELECT count(*) FROM fetch_jobs "
                         "WHERE kind='semantic'").fetchone()[0] == 0
    db.close()


def test_bundle_fixed_shape(tmp_path):
    db = _seeded(tmp_path)
    b = semantic.thread_bundle(db, 1, 1)
    assert b["bundle_id"].startswith("bundle_1_1_")
    assert {m["message_id"] for m in b["members"]} == {1, 2}
    assert b["content_quality"] == "full"
    assert b["registry_version"] == jev.REGISTRY_VERSION
    st = semantic.jev_state(b, 1)
    assert st["target"]["id"] == "m1" and BODY in st["target"]["text"]
    assert st["context"][0]["id"] == "m2"
    # sender minimization: profession/type only, no display names (INV-05)
    assert "sender_name" not in json.dumps(st)
    db.close()


def test_fingerprint_covers_revisions(tmp_path):
    db = _seeded(tmp_path)
    b1 = semantic.thread_bundle(db, 1, 1)
    fp1 = b1["source_fingerprint"]
    assert semantic.thread_bundle(db, 1, 1)["source_fingerprint"] == fp1
    db.save_messages([_message(2, parent=1, body="返信を編集した本文")])
    b2 = semantic.thread_bundle(db, 1, 1)
    assert b2["source_fingerprint"] != fp1       # context edit -> new gen
    assert semantic.bundle_fingerprint(
        b2["members"], model="jev-x") != b2["source_fingerprint"]
    db.close()


# ---------- pipeline ----------

def test_off_no_seed_no_drain(tmp_path):
    db = _seeded(tmp_path)
    assert semantic.seed(db, 1, cfg_path="/nonexistent.json") is None
    res = {"errors": []}
    out = semantic.run_due(db, {"semantic": {"mode": "off"}}, res,
                           time.monotonic() + 60)
    assert out == {"mode": "off", "done": 0, "deferred": 0,
                   "failed": 0, "left": None, "budget_exhausted": False}
    assert db.job_pending("semantic", 1, 1)["state"] == "pending"
    db.close()


def test_shadow_full_pipeline(tmp_path):
    db = _seeded(tmp_path)
    res = {"errors": []}
    fake = _FakeJev(choice_map={})   # claim-support default = 'supports'
    out = semantic.run_due(db, _cfg("shadow"), res,
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
    out2 = semantic.run_due(db, _cfg("shadow"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 0
    db.close()


def test_enforce_enqueues_notice_per_target(tmp_path):
    """A PASS generation covering two posts must emit one notice PER
    audited target — a shared (root, fp) delivery key used to dedup
    every later target's claims out of the notice entirely, while the
    header still claimed them."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    out = semantic.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    rows = db.db.execute("SELECT payload FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 2
    pls = [json.loads(r["payload"]) for r in rows]
    assert {p["target_message_id"] for p in pls} == {1, 2}
    assert len({p["delivery_key"] for p in pls}) == 2
    for p in pls:
        # multi-target notices name which covered post they report on
        assert "要約対象" in p["text"]
        assert "要約" in p["text"] and "medical-care.net" in p["text"]
    # replay same generation -> deduped delivery keys, no second intents
    db.semantic_seed(1, [1], {"origin": "replay"})
    semantic.run_due(db, _cfg("enforce"), res, time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 2
    db.close()


def test_shadow_no_outbox(tmp_path):
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    kinds = {r[0] for r in db.db.execute(
        "SELECT DISTINCT kind FROM notify_outbox")}
    assert kinds == {"new_messages"}
    db.close()


def test_claim_without_evidence_audit(tmp_path):
    db = _seeded(tmp_path)
    calls = {"n": 0}

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return _llm(prompt)
        calls["n"] += 1
        # claim with empty fact_refs -> INV-07 violation -> repair -> fix
        if calls["n"] == 1:
            return json.dumps({"claims": [{
                "section": "medication", "text": "無根拠の断定",
                "claim_kind": "reported_fact", "fact_refs": []}],
                "limitations": []})
        return _llm(prompt)

    semantic.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=llm)
    audit = json.loads(db.artifacts("semantic_audit",
                                    message_id=1)[-1]["meta"])
    assert audit["audit_status"] == "PASS"   # repaired once, then passed
    assert audit["repair_count"] == 1
    # target1: bad + repaired = 2 calls; target2: clean = 1 call
    assert calls["n"] == 3
    db.close()


def test_repair_once_then_needs_review(tmp_path):
    db = _seeded(tmp_path)

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return _llm(prompt)
        return json.dumps({"claims": [{
            "section": "medication", "text": "無根拠の断定",
            "claim_kind": "reported_fact", "fact_refs": []}],
            "limitations": []})

    semantic.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=llm)
    meta = json.loads(db.artifacts("semantic_audit",
                                   message_id=1)[-1]["meta"])
    # still unsupported after the single allowed repair -> human review
    assert meta["audit_status"] == "NEEDS_REVIEW"
    db.close()


def test_jev_unavailable_leaves_pending(tmp_path):
    db = _seeded(tmp_path)
    res = {"errors": []}
    semantic.run_due(db, _cfg("shadow", budget=0), res,
                     time.monotonic() + 60,
                     jev_client=None, llm_fn=_llm)
    job = db.job_pending("semantic", 1, 1)
    assert job is not None                     # deferred, not failed/done
    art = db.artifacts("semantic_assess", message_id=1)[-1]
    assert json.loads(art["meta"])["technical_status"] == "pending"
    db.close()


def test_stale_generation_not_promoted(tmp_path):
    db = _seeded(tmp_path)
    real_bundle = semantic.thread_bundle

    def drifting_bundle(ledg, pid, root, targets=None):
        # second call (the generation guard) sees a 'newer' bundle
        b = real_bundle(ledg, pid, root, targets)
        if drifting_bundle.calls > 0 and b:
            b["source_fingerprint"] = "x" * 64
        drifting_bundle.calls += 1
        return b

    drifting_bundle.calls = 0
    orig = semantic.thread_bundle
    semantic.thread_bundle = drifting_bundle
    try:
        out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
    finally:
        semantic.thread_bundle = orig
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
    semantic.run_due(db, _cfg("shadow"), res, time.monotonic() + 300,
                     jev_client=fake, llm_fn=_llm)
    n = fake.requests_made
    (tmp_path / "cfg.json").write_text(json.dumps(_cfg("shadow")))
    semantic.seed(db, 1, cfg_path=str(tmp_path / "cfg.json"))
    semantic.run_due(db, _cfg("shadow"), res, time.monotonic() + 300,
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

    semantic.run_due(db, _cfg("shadow"), {"errors": []},
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


def test_evidence_span_locate():
    assert semantic._locate_quote(BODY, "カロナール300mg") == (4, 14)
    assert semantic._locate_quote(BODY, "存在しない文") is None
    assert semantic._locate_quote("aa aa", "aa") is None   # ambiguous
    f, ok, dropped = semantic.extract_facts(
        lambda p: json.dumps({"facts": [{
            "statement": "x", "kind": "other", "status": "not_stated",
            "polarity": "affirmed", "time_text": None, "quantity": None,
            "evidence_quote": "存在しない引用"}]}),
        {"message_id": 1, "revision": "r", "body_original": BODY})
    assert ok and dropped == 0
    assert f[0]["validation_status"] == "unverified"
    assert f[0]["evidence_refs"] == []


def test_daily_budget_exhausted(tmp_path):
    db = _seeded(tmp_path)
    db.artifact_add("semantic_usage", "{}", project_id=1, message_id=1,
                    meta={"jev_requests": 5})
    res = {"errors": []}
    out = semantic.run_due(db, _cfg("shadow", budget=5), res,
                           time.monotonic() + 60,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 0
    assert out["budget_exhausted"] is True
    assert "semantic: daily_budget_exhausted" in res["errors"]
    db.close()


def test_degraded_notice_enforce_only(tmp_path):
    db = _seeded(tmp_path)
    # age the notify intent past the delay, leave the job unprocessed
    db.db.execute("UPDATE notify_outbox SET created_at=?",
                  (time.time() - 3600,))
    db.db.commit()
    res = {"errors": []}
    semantic.run_due(db, _cfg("enforce", delayed_notice_seconds=900,
                            budget=0),
                           res, time.monotonic() + 60,
                           jev_client=None, llm_fn=_llm)
    rows = db.db.execute("SELECT payload FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 1
    pl = json.loads(rows[0]["payload"])
    assert pl["degraded"] is True and "原文" in pl["text"]
    # second drain -> same delivery_key -> no duplicate
    semantic.run_due(db, _cfg("enforce", delayed_notice_seconds=900,
                            budget=0),
                     res, time.monotonic() + 60, jev_client=None,
                     llm_fn=_llm)
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 1
    # shadow mode must never enqueue (AT-049)
    db2_path = tmp_path / "b.db"
    db2 = ledger.Ledger(str(db2_path))
    p2 = SimpleNamespace(project_id=1, project_type="medical",
                         patient_name="t", disease="", station_name="",
                         url="", fetch_state="complete",
                         fetch_reason=None,
                         messages=[_message(9)])
    db2.save_patient(p2, notify={"source": "unread"}, semantic=True)
    db2.db.execute("UPDATE notify_outbox SET created_at=?",
                   (time.time() - 3600,))
    db2.db.commit()
    semantic.run_due(db2, _cfg("shadow", delayed_notice_seconds=60),
                     {"errors": []}, time.monotonic() + 60,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db2.db.execute("SELECT count(*) FROM notify_outbox "
                          "WHERE kind='semantic_notice'").fetchone()[0] == 0
    db.close()
    db2.close()


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
    semantic.run_due(db, json.loads(cfgf.read_text()), res,
                     time.monotonic() + 300, jev_client=fake,
                     llm_fn=flipping, cfg_path=str(cfgf))
    pending = db.db.execute("SELECT count(*) FROM fetch_jobs "
                            "WHERE kind='semantic' AND state='pending'"
                            ).fetchone()[0]
    assert pending >= 1            # remaining work survives OFF
    db.close()


def test_notifier_semantic_notice_render(tmp_path, monkeypatch):
    """The sender renders the frozen payload text — no re-derivation,
    no patient lookups at send time (INV-14). The send-time gates
    (enforce mode + live fingerprint) pass on the live generation."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    ev = db.db.execute("SELECT * FROM notify_outbox "
                       "WHERE kind='semantic_notice'").fetchone()
    assert ev is not None
    frozen = json.loads(ev["payload"])["text"]
    import notifier
    monkeypatch.setattr(notifier, "_config",
                        lambda: {"semantic": {"mode": "enforce"}})
    content, files = notifier._format_event(db, ev)
    assert content == frozen and files == []
    db.close()


def test_snapshot_view_semantic(tmp_path):
    """Artifacts land in the published snapshot and mcs_view exposes
    them read-only — same reader gate as every other table (AT-065)."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snaps"))
    view = mcs_view.View(snap)
    res = view.read("semantic", project=1, message_id=1)
    assert res["semantic"]["semantic_summary"]
    assert res["semantic"]["semantic_audit"][0]["meta"][
        "audit_status"] == "PASS"
    db.close()


# ---------- review-fix regressions ----------

def test_degraded_skips_delivered_history(tmp_path):
    """§19.3: degraded fallback covers only events whose base notice is
    STILL undelivered — an accepted/suppressed historical row must
    never spawn a '新着取得' notice."""
    db = _seeded(tmp_path)
    # the seeding left one pending new_messages event; add a second,
    # long-delivered one and age everything past the delay
    db.outbox_add("new_messages", 1, {"message_ids": [1]})
    old = db.db.execute(
        "SELECT event_id FROM notify_outbox WHERE kind='new_messages' "
        "ORDER BY event_id DESC").fetchone()["event_id"]
    db.outbox_mark(old, "accepted")
    db.db.execute("UPDATE notify_outbox SET created_at=?",
                  (time.time() - 7200,))
    db.db.commit()
    scfg = semantic.semantic_config(
        _cfg("enforce", delayed_notice_seconds=900))[0]
    assert semantic._emit_degraded(db, scfg) == 1   # only the pending one
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox "
        "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 1
    pl = json.loads(rows[0]["payload"])
    assert pl["degraded"] is True
    assert pl["src_event_id"] != old   # not the delivered event
    # idempotent — a second scan adds nothing
    assert semantic._emit_degraded(db, scfg) == 0
    db.close()


def test_replay_heals_missing_notify_intent(tmp_path):
    """§18.3 regression: a run that committed summary+audit but lost
    the plan/outbox rows (the old crash window) must recreate both on
    the next run instead of marking done with the intent gone."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    out = semantic.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    # simulate the pre-fix crash state
    db.db.execute("DELETE FROM artifacts WHERE kind='notify_plan'")
    db.db.execute("DELETE FROM notify_outbox "
                  "WHERE kind='semantic_notice'")
    db.db.commit()
    db.semantic_seed(1, [1], {"origin": "replay"})
    out2 = semantic.run_due(db, _cfg("enforce"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 1
    assert db.artifacts("notify_plan", message_id=1)
    assert db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    db.close()


def test_pending_audit_retries_then_passes(tmp_path):
    """AT-058 regression: a mid-audit Jev outage must leave the job
    retryable — never 'done' on a PENDING audit — and the next run
    re-audits the stored summary to a terminal status."""
    db = _seeded(tmp_path)

    class AuditDown(_FakeJev):
        def evaluate(self, state, questions, deadline):
            if all(str(k).startswith("claim_") for k in questions):
                raise jev.JevError("http_500", retryable=True)
            return super().evaluate(state, questions, deadline)

    res = {"errors": []}
    out = semantic.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=AuditDown(), llm_fn=_llm)
    assert out["done"] == 0 and out["deferred"] == 1
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()
    assert job["state"] == "pending" and job["attempts"] == 1
    audits = db.artifacts("semantic_audit", message_id=1)
    assert json.loads(audits[-1]["meta"])["audit_status"] == "PENDING"
    # Jev back — force the retry due and re-run: re-audit, not skip
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    out2 = semantic.run_due(db, _cfg("enforce"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 1
    meta = json.loads(db.artifacts("semantic_audit",
                                   message_id=1)[-1]["meta"])
    assert meta["audit_status"] == "PASS"
    # and the PASS notice enqueued despite the earlier PENDING plan row
    assert db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
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

    out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
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


def test_claim_audit_sees_surrounding_context(tmp_path):
    """AT-028: the support check must see the quote's surrounding
    window, not just the bare quote — a neighboring negation or
    condition has to be visible to the auditor."""
    db = _seeded(tmp_path)
    captured = []

    class Cap(_FakeJev):
        def evaluate(self, state, questions, deadline):
            if all(str(k).startswith("claim_") for k in questions):
                captured.append(state)
            return super().evaluate(state, questions, deadline)

    out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=Cap(), llm_fn=_llm)
    assert out["done"] == 1 and captured
    ctx = captured[0]["context"]
    roles = {c["role"] for c in ctx}
    assert "evidence_quote" in roles and "evidence_context" in roles
    win = next(c["text"] for c in ctx if c["role"] == "evidence_context")
    assert "確認お願いします" in win    # outside the bare quote
    db.close()


def test_loop_scan_not_capped(tmp_path):
    """§17.2 regression: every open candidate is evaluated against new
    targets — not just the newest 10 — and re-runs add no duplicates."""
    db = _seeded(tmp_path)
    for i in range(20, 32):          # 12 pre-existing open candidates
        db.artifact_add("loop_candidate", json.dumps({
            "loop_id": f"loop_{i}", "project_id": 1,
            "kind": "pending_item", "description": f"item{i}",
            "origin": {"message_id": i, "revision": "r",
                       "evidence_refs": []},
            "assignee_text": None, "due_text": None,
            "state": "PROPOSED", "history": []}),
            project_id=1, message_id=i, meta={"candidate_fp": f"fp{i}"})
    res = {"errors": []}
    out = semantic.run_due(db, _cfg("shadow"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    evs = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='loop_event'"
    ).fetchall()
    origins = {json.loads(e["content"])["loop_origin_id"] for e in evs}
    assert origins == set(range(20, 32))
    n0 = len(evs)
    # replay on the same generation: pair dedup -> zero new events
    db.semantic_seed(1, [1], {"origin": "replay"})
    semantic.run_due(db, _cfg("shadow"), res,
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind='loop_event'"
    ).fetchone()["c"] == n0
    db.close()


def test_nonretryable_jev_error_fails_bounded(tmp_path):
    """A non-retryable Jev failure must consume attempts — deferring
    forever would burn a call every tick on an input that can never
    pass. Six attempts later the job is terminally failed and visible
    in status_report."""
    db = _seeded(tmp_path)
    fake = _FakeJev(error=jev.JevError("protocol_error",
                                     retryable=False))
    res = {"errors": []}
    for _ in range(6):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
        db.db.commit()
        semantic.run_due(db, _cfg("shadow"), res,
                         time.monotonic() + 300,
                         jev_client=fake, llm_fn=_llm)
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone()
    assert job["state"] == "failed"
    db.close()


def test_jev_usage_deltas_exact(tmp_path):
    """The daily budget ledger sums per-attempt deltas — it must equal
    the client's real request count including claim-audit and loop
    calls, not a cumulative counter snapshotted per artifact."""
    db = _seeded(tmp_path)
    fake = _FakeJev()
    semantic.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=fake, llm_fn=_llm)
    assert fake.requests_made > 0
    assert semantic.jev_usage_today(db) == fake.requests_made
    db.close()


def test_sem_block_drops_stale_revision(tmp_path, monkeypatch):
    """INV-15: a PASS summary describes one revision — after a body
    edit the audited block must not attach to the new revision."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    import notifier
    monkeypatch.setattr(
        notifier, "_config",
        lambda: {"semantic": {"mode": "enforce"}})
    ev = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    content, _ = notifier._format_event(db, ev)
    assert "要約（自動検査済）" in content
    # edit the parent body -> new content_hash -> block must drop
    p = _patient(db)
    p.messages = [_message(1, body=BODY + "（追記あり）", unread=False)]
    db.save_patient(p)
    ev = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    content, _ = notifier._format_event(db, ev)
    assert "要約（自動検査済）" not in content
    db.close()


def test_off_mode_parks_queued_notice(tmp_path, monkeypatch):
    """§22.1: an enforce notice committed before an OFF flip must never
    send — the intent stays queued for a later enforce flush."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    n = db.db.execute("SELECT COUNT(*) c FROM notify_outbox "
                      "WHERE kind='semantic_notice'").fetchone()["c"]
    assert n == 2          # one queued intent per audited target
    import notifier
    monkeypatch.setattr(
        notifier, "_config",
        lambda: {"semantic": {"mode": "off"},
                 "discord_channel_id": "123"})
    monkeypatch.setattr(notifier, "_token", lambda: "tok")
    calls = []
    monkeypatch.setattr(notifier, "_post",
                        lambda *a, **k: calls.append(a) or "1")
    notifier.flush(db)
    # the base new_messages notice sends; the semantic_notice must not
    assert not any("要約" in str(c[2]) for c in calls)
    row = db.db.execute(
        "SELECT state FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    assert row["state"] == "pending"   # parked, not destroyed
    db.close()


def test_stale_generation_notice_suppressed(tmp_path, monkeypatch):
    """§18.4: a PASS notice enqueued for fingerprint fp must suppress
    terminally once the thread's live fingerprint has moved on."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    p = _patient(db)
    p.messages = [_message(1, body=BODY + "（編集）", unread=False)]
    db.save_patient(p)                 # fingerprint moves
    import notifier
    monkeypatch.setattr(
        notifier, "_config",
        lambda: {"semantic": {"mode": "enforce"},
                 "discord_channel_id": "123"})
    monkeypatch.setattr(notifier, "_token", lambda: "tok")
    calls = []
    monkeypatch.setattr(notifier, "_post",
                        lambda *a, **k: calls.append(a) or "1")
    notifier.flush(db)
    assert not any("要約" in str(c[2]) for c in calls)
    row = db.db.execute(
        "SELECT state FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    assert row["state"] == "suppressed"
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
            return mcs_adapter.MessageBatch(
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
    out = semantic.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert json.loads(db.artifacts("semantic_audit", message_id=50)[-1]
                      ["meta"])["audit_status"] == "PASS"
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox "
                         "WHERE kind='semantic_notice'"
                         ).fetchone()["c"] == 0
    db.close()


def test_replay_of_imported_message_no_notice(tmp_path):
    """AT-055: replaying a thread that was never in a new_messages
    intent re-evaluates but cannot create notification eligibility."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(50)], project_id=1, semantic=False)
    semantic.seed(db, 50, cfg_path=_cfg_path(tmp_path, "enforce"))
    out = semantic.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox"
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
    out = semantic.run_due(db, _cfg("enforce"), {"errors": []},
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


def test_notice_suppressed_when_src_event_suppressed(tmp_path,
                                                   monkeypatch):
    """Send-time gate: if the origin arrival intent was suppressed
    (e.g. archive), the semantic follow-up must not deliver."""
    db = _seeded(tmp_path)
    semantic.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    ev = db.db.execute("SELECT * FROM notify_outbox "
                       "WHERE kind='semantic_notice'").fetchone()
    src = json.loads(ev["payload"])["src_event_id"]
    db.outbox_suppress(src)
    import notifier
    monkeypatch.setattr(notifier, "_config",
                        lambda: {"semantic": {"mode": "enforce"}})
    with pytest.raises(notifier._StaleSend):
        notifier._format_event(db, ev)
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

    out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=flaky)
    assert out["deferred"] == 1
    assert not db.artifacts("semantic_facts", message_id=1)
    # LLM back -> the whole body is re-extracted, all chunks covered
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    out2 = semantic.run_due(db, _cfg("shadow"), {"errors": []},
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 1
    meta = json.loads(db.artifacts("semantic_facts",
                                   message_id=1)[-1]["meta"])
    assert meta["chunks_total"] >= 2
    db.close()


def test_pending_assess_artifact_dedup(tmp_path):
    """A durable Jev wait-state records ONE pending artifact per
    generation — repeated drains must not stack identical rows."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    for _ in range(3):
        semantic.run_due(db, _cfg("shadow", budget=0), res,
                         time.monotonic() + 300,
                         jev_client=None, llm_fn=_llm)
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
        db.db.commit()
    rows = db.artifacts("semantic_assess", message_id=1)
    assert len(rows) == 1
    assert json.loads(rows[0]["meta"])["technical_status"] == "pending"
    db.close()


def test_daily_budget_binds_inside_job(tmp_path):
    """The daily cap is per-request, not per-job: mid-job claim/detail
    calls stop at the remaining budget instead of overshooting."""
    def post(body, timeout):
        answers = {}
        for qid, q in body["questions"].items():
            if q.get("type") == "choice":
                opts = list(q["options"])
                answers[qid] = {"type": "choice", "choice": opts[0],
                                "confidence": 0.9,
                                "distribution":
                                {o: 1.0 / len(opts) for o in opts}}
            else:
                answers[qid] = {"type": "noul", "noul": 0.9}
        return 200, {}, json.dumps(
            {"model": jev.JEV_MODEL, "answers": answers}).encode()

    db = _seeded(tmp_path)
    client = jev.JevClient(api_key="k", post_fn=post)
    semantic.run_due(db, _cfg("enforce", budget=3),
                     {"errors": []}, time.monotonic() + 300,
                     jev_client=client, llm_fn=_llm)
    assert client.requests_made == 3        # capped mid-job, not after
    job = db.db.execute("SELECT state,attempts FROM fetch_jobs "
                        "WHERE kind='semantic'").fetchone()
    assert job["state"] == "pending"        # audit unfinished -> retry
    db.close()


# ---------- review-fix regressions (second pass) ----------

def test_oversized_response_fails_fast():
    """A deterministic non-retryable failure (response_too_large) must
    stop after ONE post — looping the identical request would only
    burn the daily budget on an input that can never succeed."""
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 200, {}, b"x" * (jev.MAX_RESPONSE_BYTES + 1)

    c = jev.JevClient(api_key="k", post_fn=post, max_attempts=3)
    with pytest.raises(jev.JevError) as ei:
        c.evaluate({}, {"a": jev.noul_question("i", "t", "f")},
                   time.monotonic() + 30)
    assert ei.value.detail == "response_too_large"
    assert len(calls) == 1                  # one request, not three


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
    out = semantic.run_due(db, _cfg("enforce"), {"errors": []},
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


def test_quantity_untraced_needs_quote():
    """AT-025/026: a quantity that appears only ELSEWHERE in the post —
    not inside the fact's own evidence quote — is untraced, even
    though it exists verbatim in the body."""
    body = "血圧は120を記録。カロナール300mgを1日3回に変更します。"
    member = {"message_id": 1, "revision": "r1", "body_original": body}
    quote = "血圧は120を記録"
    s = body.index(quote)
    facts = [{
        "fact_id": "fact_1_0", "kind": "observation",
        "statement": "血圧値の記載", "subject_ref": "m1",
        "drug_ref": None, "status": "not_stated", "polarity": "affirmed",
        "quantity": "300mg", "time_text": None, "occurred_at": None,
        "evidence_refs": ["ev_1_0"], "validation_status": "candidate",
        "_evidence": {"evidence_id": "ev_1_0", "message_id": 1,
                      "revision_id": "r1", "start_codepoint": s,
                      "end_codepoint": s + len(quote),
                      "quote": quote}}]
    findings = semantic.audit_code(
        {"members": [member]}, facts, {"claims": [], "limitations": []})
    assert any(f["code"] == "quantity_untraced" for f in findings)
    # a quantity inside its own quote still traces fine
    quote2 = "カロナール300mgを1日3回に変更"
    s = body.index(quote2)
    facts[0]["_evidence"]["quote"] = quote2
    facts[0]["_evidence"]["start_codepoint"] = s
    facts[0]["_evidence"]["end_codepoint"] = s + len(quote2)
    findings = semantic.audit_code(
        {"members": [member]}, facts, {"claims": [], "limitations": []})
    assert not any(f["code"] == "quantity_untraced" for f in findings)


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
    out = semantic.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=llm)
    assert out["deferred"] == 1
    audits = db.artifacts("semantic_audit", message_id=1)
    assert len(audits) == 1
    meta = json.loads(audits[0]["meta"])
    assert meta["audit_status"] == "NEEDS_REVIEW"
    assert meta["repair_count"] == 1      # budget spent, durably
    # writer back -> rerun completes WITHOUT re-auditing mid-1
    fixed["v"] = True
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='semantic'")
    db.db.commit()
    out2 = semantic.run_due(db, _cfg("enforce"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=llm)
    assert out2["done"] == 1
    assert calls["bad"] == 2   # initial + one repair, never re-run
    assert len(db.artifacts("semantic_audit", message_id=1)) == 1
    assert len(db.artifacts("semantic_summary", message_id=1)) == 1
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
    # first window: the 4 lowest-id jobs are out of scope -> deferred
    semantic.run_due(db, cfg, res, time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm, max_jobs=4)
    out = semantic.run_due(db, cfg, res, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm,
                           max_jobs=4)
    assert out["done"] == 1
    row = db.db.execute(
        "SELECT state FROM fetch_jobs WHERE kind='semantic' "
        "AND project_id=1").fetchone()
    assert row["state"] == "done"
    left = db.db.execute(
        "SELECT COUNT(*) c FROM fetch_jobs WHERE kind='semantic' "
        "AND state='pending'").fetchone()["c"]
    assert left == 6              # filtered jobs stay pending/resumable
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

    out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
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
        "relation": "completion_report"}), project_id=1, message_id=2)
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
