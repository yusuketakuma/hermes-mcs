"""Canonical-mode drain regression tests (C02-C04).

The canonical path is gated pre-enablement; these tests drive a full
``semantic.run_due`` with synthetic LLM/Jev stubs and assert the
pre-enablement repairs: per-target v2 documents, bounded coverage
resume, and evaluated-only audit reuse.
"""
import json
import time

import semantic
import semantic_extraction as extraction
import semantic_facts as sf
from mcs_requests import payload_hash
from semantic_policy import (KIND_AUDIT, KIND_FACT_AUDIT, KIND_FACTS_V2,
                             KIND_SUMMARY)
from test_mcs_semantic import (_FakeJev, _cfg, _ledger, _message,
                               _patient)

NO_FACTS = {c: "none" for c in sf.MANDATORY_CATEGORIES}


def _seeded_two(tmp_path):
    """Parent + reply with DISTINCT bodies -> one job, targets {1,2}."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1, body="アムロジピン5mgを継続します。")]
    p.messages[0].replies = [
        _message(2, parent=1, body="メトホルミン500mgが出ています。")]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


def _canonical_cfg():
    return _cfg("enforce", loop_mode="off",
                fact_source="canonical",
                fact_source_gate="g6-v1:test")


def _med_fact(drug, quote, action):
    return {"statement": quote, "kind": "medication_event",
            "action": action, "subject_role": "patient",
            "polarity": "affirmed", "workflow_status": "performed",
            "importance": "T1", "evidence_quote": quote}


def _llm_v2(prompt):
    """Per-target v2 extraction + empty summary.  The prompt embeds the
    member body, so the response is keyed by which drug it mentions."""
    if "要約器" in prompt:
        return json.dumps({"claims": [], "limitations": []},
                          ensure_ascii=False)
    if "アムロジピン" in prompt:
        return json.dumps({
            "facts": [_med_fact("アムロジピン", "アムロジピン5mgを継続",
                                "continue")],
            "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)
    if "メトホルミン" in prompt:
        return json.dumps({
            "facts": [_med_fact("メトホルミン", "メトホルミン500mgが出ています",
                                "continue")],
            "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)
    return json.dumps({"facts": [],
                       "category_presence": dict(NO_FACTS)},
                      ensure_ascii=False)


def _preflight_jev():
    """FakeJev answering 'present' for medication, 'absent' elsewhere —
    preflight obligations close cleanly for the two-target corpus."""
    choice_map = {f"has_{c}": "absent" for c in sf.MANDATORY_CATEGORIES}
    choice_map["has_medication"] = "present"
    return _FakeJev(choice_map=choice_map)


def _drain(db, llm=None, jev=None):
    return semantic.run_due(
        db, _canonical_cfg(), {"errors": []},
        time.monotonic() + 300,
        jev_client=jev or _preflight_jev(), llm_fn=llm or _llm_v2)


def _summary_doc(db, mid):
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (KIND_SUMMARY, mid)).fetchone()
    return json.loads(row["content"]) if row else None


def _artifacts(db, kind, mid):
    return db.db.execute(
        "SELECT content,meta FROM artifacts WHERE kind=? "
        "AND message_id=? ORDER BY artifact_id",
        (kind, mid)).fetchall()


def _meta(row):
    return json.loads(row["meta"] or "{}")


def test_per_target_v2_doc_reaches_mandatory_render(tmp_path):
    """C02: two targets must not share the LAST member's v2_doc —
    mandatory facts inside each summary come only from its own doc."""
    db = _seeded_two(tmp_path)
    try:
        out = _drain(db)
        assert out["done"] == 1 and not out["failed"]
        s1 = _summary_doc(db, 1)
        s2 = _summary_doc(db, 2)
        assert s1 and s2
        f1 = " ".join(s1.get("mandatory_facts") or [])
        f2 = " ".join(s2.get("mandatory_facts") or [])
        assert "アムロジピン" in f1 and "メトホルミン" not in f1
        assert "メトホルミン" in f2 and "アムロジピン" not in f2
    finally:
        db.close()


def _seed_incomplete_v2(db, mid, fp, *, retried=False):
    doc = {"version": sf.CONTRACT_VERSION,
           "source": {"message_id": f"m{mid}", "revision": "r1",
                      "content_hash": "h", "body_codepoints": 5,
                      "content_quality": "full",
                      "attachments_complete": True,
                      "source_fingerprint": fp},
           "atoms": [], "chunks": [], "obligations": [],
           "evidence": [], "facts": [], "relations": [],
           "coverage": {"category_counts": {},
                        "open_obligation_ids": ["ob_x"],
                        "limitations": ["chunks_incomplete"],
                        "status": "incomplete"}}
    meta = {"fingerprint": fp, "schema": "v2",
            "fact_source": "canonical",
            "coverage_status": "incomplete"}
    if retried:
        meta["coverage_retry"] = True
        meta["needs_review"] = True
    db.artifact_add(KIND_FACTS_V2,
                    json.dumps(doc, ensure_ascii=False),
                    project_id=1, message_id=mid, model="test",
                    meta=meta)


def test_incomplete_coverage_resumes_once_then_stops(tmp_path):
    """C03: a stored incomplete coverage doc is re-extracted ONCE
    (bounded resume); a doc already carrying the retry marker is
    parked for review — never re-extracted again."""
    db = _seeded_two(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
        fp = bundle["source_fingerprint"]
        member = next(m for m in bundle["members"] if m["message_id"] == 1)

        def ambiguous_llm(prompt):
            result = json.loads(_llm_v2(prompt))
            result["category_presence"]["medication"] = "ambiguous"
            return json.dumps(result)

        incomplete = extraction.extract_facts_v2(
            ambiguous_llm, member, ledger=db, source_fingerprint=fp,
            project_id=1, jev_client=_preflight_jev())["doc"]
        assert incomplete["coverage"]["status"] == "incomplete"
        db.artifact_add(KIND_FACTS_V2, json.dumps(incomplete),
                        project_id=1, message_id=1,
                        meta={"fingerprint": fp, "coverage_status": "incomplete"})

        calls = []

        def counting_llm(prompt):
            if "要約器" not in prompt:
                calls.append(prompt)
            return _llm_v2(prompt)

        _drain(db, llm=counting_llm)
        # the stored incomplete doc was NOT reused — extraction ran
        assert any("アムロジピン" in p for p in calls)
        rows = _artifacts(db, KIND_FACTS_V2, 1)
        latest = _meta(rows[-1])
        assert latest.get("coverage_retry") is True

        # parked for review: a doc already carrying the retry marker is
        # never re-extracted; the durable job stops for review.
        _seed_incomplete_v2(db, 1, fp, retried=True)
        calls.clear()
        db.db.execute(
            "UPDATE fetch_jobs SET state='pending', next_try=0 "
            "WHERE kind='semantic'")
        db.db.commit()
        out = _drain(db, llm=counting_llm)
        assert out["failed"] == 1
        job = db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] == "failed"
        assert not any("アムロジピン" in p for p in calls)
    finally:
        db.close()


def test_unevaluated_fact_audit_is_re_run(tmp_path):
    """C04: a stored evaluated:false fact audit is not a reusable
    verdict — the drain re-audits and records a completed artifact."""
    db = _seeded_two(tmp_path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
        fp = bundle["source_fingerprint"]
        member = next(m for m in bundle["members"]
                      if m["message_id"] == 1)
        doc = extraction.extract_facts_v2(
            _llm_v2, member, time.monotonic() + 60, ledger=db,
            source_fingerprint=fp, project_id=1,
            jev_client=_preflight_jev())["doc"]
        doc_hash = payload_hash({"f": doc["facts"], "e": doc["evidence"]})
        # stale mid-audit outage: evaluated never completed
        db.artifact_add(
            KIND_FACT_AUDIT,
            json.dumps({"status": "NEEDS_REVIEW", "evaluated": False,
                        "findings": [], "fact_verdicts": {}},
                       ensure_ascii=False),
            project_id=1, message_id=1, model="test",
            meta={"fingerprint": fp, "doc_hash": doc_hash,
                  "policy_fingerprint": semantic.policy_fingerprint(
                      semantic.semantic_config(_canonical_cfg())[0]),
                  "audit_status": "NEEDS_REVIEW"})

        _drain(db)
        audits = [json.loads(r["content"]) for r in
                  _artifacts(db, KIND_FACT_AUDIT, 1)
                  if _meta(r).get("doc_hash") == doc_hash]
        assert any(a.get("evaluated") for a in audits), \
            "evaluated:false audit was reused instead of re-run"
    finally:
        db.close()


def _many_facts_llm(n, statement_len=20):
    """v2 extraction returning n verified-eligible facts per target —
    the >40 corpus the old max_facts cap silently dropped."""
    def llm(prompt):
        if "要約器" in prompt:
            return json.dumps({"claims": [], "limitations": []},
                              ensure_ascii=False)
        quote = ("アムロジピン" if "アムロジピン" in prompt
                 else "メトホルミン" if "メトホルミン" in prompt else "薬")
        facts = [{"statement": (f"合成事実{i:03d}番 "
                                + "詳細" * statement_len)[:statement_len],
                  "kind": "medication_event", "action": "continue",
                  "subject_role": "patient", "polarity": "affirmed",
                  "workflow_status": "performed", "importance": "T1",
                  "evidence_quote": quote} for i in range(n)]
        return json.dumps({"facts": facts,
                           "category_presence":
                           dict(NO_FACTS, medication="one")},
                          ensure_ascii=False)
    return llm


def _audit_doc(db, mid):
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (KIND_AUDIT, mid)).fetchone()
    return json.loads(row["content"]) if row else None


def test_drain_publishes_all_verified_facts_past_old_cap(tmp_path):
    """T4: 41+ verified facts reach the summary uncapped — rendered
    fact IDs equal the verified set, paged within the char budget."""
    db = _seeded_two(tmp_path)
    try:
        out = _drain(db, llm=_many_facts_llm(45))
        assert out["done"] == 1 and not out["failed"]
        s1 = _summary_doc(db, 1)
        ids = s1.get("mandatory_fact_ids") or []
        # pipeline may merge a rule-derived fact on top of the 45 stub
        # facts — the contract is: everything verified renders, >40
        assert len(ids) > 40
        assert len(s1.get("mandatory_facts") or []) == len(ids)
        assert len(set(ids)) == len(ids)
        pages = s1.get("mandatory_pages") or []
        assert pages and len(pages) > 1
        seen = [fid for p in pages for fid in p["fact_ids"]]
        assert sorted(seen) == sorted(ids)
        assert s1.get("mandatory_overview")
    finally:
        db.close()


def test_drain_flags_incomplete_mandatory_pages(tmp_path):
    """T4 failure path: a fact line that cannot fit any page budget
    marks the summary NEEDS_REVIEW — publication incomplete, never
    PASS."""
    db = _seeded_two(tmp_path)
    try:
        out = _drain(db, llm=_many_facts_llm(3, statement_len=5000))
        assert out["done"] == 1 and not out["failed"]
        audit = _audit_doc(db, 1)
        assert audit["status"] == "NEEDS_REVIEW"
        assert any(f["code"] == "mandatory_render_incomplete"
                   for f in audit["findings"])
    finally:
        db.close()


def _seed_one(tmp_path):
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1, body="アムロジピン5mgを継続します。")]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


class _RejectFactOnceJev(_FakeJev):
    """Preflight: medication present.  The first per-fact audit of each
    fact answers not_supported; later audits of the same fact support."""

    def __init__(self, only=""):
        choice_map = {f"has_{c}": "absent" for c in sf.MANDATORY_CATEGORIES}
        choice_map["has_medication"] = "present"
        super().__init__(choice_map=choice_map)
        self.only = only
        self.rejected = set()

    def evaluate(self, state, questions, deadline):
        target = (state.get("target") or {}).get("id")
        text = (state.get("target") or {}).get("text") or ""
        if target in questions and target not in self.rejected \
                and self.only in text:
            self.rejected.add(target)
            self.choice_map[target] = "not_supported"
        elif target in questions:
            self.choice_map[target] = "supports"
        return super().evaluate(state, questions, deadline)


def _job(db):
    return dict(db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
    ).fetchone())


def _latest_diag(db):
    row = db.db.execute(
        "SELECT content FROM artifacts WHERE kind='v4_diagnostic' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return json.loads(row["content"]) if row else None


def test_repair_reopening_obligations_never_publishes_pass(tmp_path):
    """U06-F01: a repair that drops the rejected medication fact reopens
    its obligation — the repaired doc is incomplete and must park for
    review, never mint the semantic_facts_v4 PASS read model."""
    db = _seed_one(tmp_path)
    try:
        def llm(prompt):
            if "要約器" in prompt:
                return json.dumps({"claims": [], "limitations": []},
                                  ensure_ascii=False)
            if "監査で不合格" in prompt:     # repair drops the fact
                return json.dumps(
                    {"facts": [], "category_presence":
                     dict(NO_FACTS, medication="one")},
                    ensure_ascii=False)
            return json.dumps(
                {"facts": [_med_fact("アムロジピン", "アムロジピン5mgを継続",
                                     "continue")],
                 "category_presence": dict(NO_FACTS, medication="one")},
                ensure_ascii=False)

        out = _drain(db, llm=llm, jev=_RejectFactOnceJev())
        latest = json.loads(_artifacts(db, KIND_FACTS_V2, 1)[-1]["content"])
        assert latest["coverage"]["status"] == "incomplete"
        kinds = {r[0] for r in db.db.execute(
            "SELECT kind FROM artifacts WHERE message_id=1")}
        assert "semantic_facts_v4" not in kinds
        assert "canonical_projection" not in kinds
        assert out["failed"] == 1 and not out["done"]
        assert _job(db)["state"] == "failed"
        audit = _audit_doc(db, 1)
        assert audit["status"] == "NEEDS_REVIEW"
        assert {"code": "canonical_coverage_incomplete"} in audit["findings"]
        assert _latest_diag(db)["status"] == "NEEDS_REVIEW"
    finally:
        db.close()


def test_evaluated_needs_review_without_fact_findings_is_terminal(tmp_path):
    """U06-F02: an evaluated fact audit that is NEEDS_REVIEW with no
    repairable per-fact finding (Jev: an important event is missing)
    is a clinical verdict — the job stops for review in one drain with
    a NEEDS_REVIEW diagnostic, never an endless PENDING deferral."""
    db = _seed_one(tmp_path)
    try:
        choice_map = {f"has_{c}": "absent" for c in sf.MANDATORY_CATEGORIES}
        choice_map["has_medication"] = "present"
        choice_map["source_fact_coverage"] = "missing"
        out = _drain(db, jev=_FakeJev(choice_map=choice_map))
        assert out["failed"] == 1 and not out["deferred"]
        assert _job(db)["state"] == "failed"
        diag = _latest_diag(db)
        assert diag["status"] == "NEEDS_REVIEW"
        codes = [f["code"] for f in diag["findings"]]
        assert "fact_stage_hard_fail" in codes
        assert "source_fact_coverage_missing" in codes
        assert _audit_doc(db, 1)["status"] == "NEEDS_REVIEW"
        kinds = {r[0] for r in db.db.execute(
            "SELECT kind FROM artifacts WHERE message_id=1")}
        assert "semantic_facts_v4" not in kinds
    finally:
        db.close()


def test_rejected_fact_repaired_and_supported_still_passes(tmp_path):
    """Control for U06-F01: a repair that keeps coverage complete is
    re-audited and may still publish PASS."""
    db = _seed_one(tmp_path)
    try:
        out = _drain(db, jev=_RejectFactOnceJev(only="継続"))
        latest = json.loads(_artifacts(db, KIND_FACTS_V2, 1)[-1]["content"])
        assert latest["coverage"]["status"] == "complete"
        assert out["done"] == 1 and not out["failed"]
        kinds = {r[0] for r in db.db.execute(
            "SELECT kind FROM artifacts WHERE message_id=1")}
        # the fact stage passed (projection minted); the summary audit
        # verdict is independent of this control
        assert "canonical_projection" in kinds
        fact_audit = json.loads(_artifacts(db, KIND_FACT_AUDIT, 1)[-1]["content"])
        assert fact_audit["status"] == "PASS"
    finally:
        db.close()


def test_budget_exhausted_at_promote_is_a_counted_retry(tmp_path,
                                                        monkeypatch):
    """U06-F03: a job budget that runs out between the last guarded
    call and promotion is not source drift — no STALE results are
    written and the job consumes a bounded retry attempt instead of
    re-running forever uncounted."""
    import semantic_runtime as runtime
    db = _seeded_two(tmp_path)
    try:
        real = runtime.guard

        def guard(ledger, token, **kw):
            if kw.get("stage") == "promote":
                raise runtime.RuntimeBudget("promote")
            return real(ledger, token, **kw)

        monkeypatch.setattr(runtime, "guard", guard)
        _drain(db)
        audits = [json.loads(r["content"])["status"]
                  for r in _artifacts(db, KIND_AUDIT, 1)]
        assert "STALE" not in audits
        summaries = [_meta(r) for r in _artifacts(db, KIND_SUMMARY, 1)]
        assert not any(m.get("stale") for m in summaries)
        job = _job(db)
        assert job["state"] == "pending" and job["attempts"] == 1
    finally:
        db.close()
