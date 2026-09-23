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
from semantic_policy import KIND_FACT_AUDIT, KIND_FACTS_V2, KIND_SUMMARY
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
