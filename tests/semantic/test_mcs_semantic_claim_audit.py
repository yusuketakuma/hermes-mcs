"""Synthetic mcs semantic claim audit contracts with isolated shared fixtures."""
import semantic_audit
import semantic_drain
import semantic_jev
import semantic_store
import json
import time
import pytest
import semantic_jev as jev
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded
from mcs_semantic_testkit import _ClaimCapture as _ClaimCapture, _claim_case as _claim_case


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

    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
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

    drafts = []

    def llm(prompt):
        if "事実候補抽出器" in prompt:
            return _llm(prompt)
        drafts.append("無根拠の断定" + str(len(drafts)))
        return json.dumps({"claims": [{
            "section": "medication", "text": drafts[-1],
            "claim_kind": "reported_fact", "fact_refs": []}],
            "limitations": []})

    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=llm)
    meta = json.loads(db.artifacts("semantic_audit",
                                   message_id=1)[-1]["meta"])
    # still unsupported after the single allowed repair -> human review
    assert meta["audit_status"] == "NEEDS_REVIEW"
    initial = db.artifacts("semantic_candidate", message_id=1)
    assert len(initial) == 1 and len(drafts) >= 2
    assert json.loads(initial[0]["content"])["claims"][0]["text"] == drafts[0]
    final = db.artifacts("semantic_summary", message_id=1)[-1]
    assert json.loads(final["content"])["claims"][0]["text"] == drafts[1]
    assert json.loads(initial[0]["meta"])["fingerprint"] == json.loads(final["meta"])["fingerprint"]
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
    out = semantic_drain.run_due(db, _cfg("enforce"), res,
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
    out2 = semantic_drain.run_due(db, _cfg("enforce"), res,
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

    out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=Cap(), llm_fn=_llm)
    assert out["done"] == 1 and captured
    ctx = captured[0]["context"]
    roles = {c["role"] for c in ctx}
    assert "evidence_quote" in roles and "evidence_context" in roles
    win = next(c["text"] for c in ctx if c["role"] == "evidence_context")
    assert "確認お願いします" in win    # outside the bare quote
    source = json.loads(next(c["text"] for c in ctx if c["role"] == "evidence_metadata"))
    member = semantic_store.thread_bundle(db.db, 1, 1)["members"][0]
    assert source == {key: member[key] for key in
                      ("message_id", "parent_id", "revision", "posted_at", "sender")}
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
    findings = semantic_audit.audit_code(
        {"members": [member]}, facts, {"claims": [], "limitations": []})
    assert any(f["code"] == "quantity_untraced" for f in findings)
    # a quantity inside its own quote still traces fine
    quote2 = "カロナール300mgを1日3回に変更"
    s = body.index(quote2)
    facts[0]["_evidence"]["quote"] = quote2
    facts[0]["_evidence"]["start_codepoint"] = s
    facts[0]["_evidence"]["end_codepoint"] = s + len(quote2)
    findings = semantic_audit.audit_code(
        {"members": [member]}, facts, {"claims": [], "limitations": []})
    assert not any(f["code"] == "quantity_untraced" for f in findings)


@pytest.mark.parametrize("code", [
    "change_kind:choice_invalid", "change_kind:probabilities_invalid",
    "change_kind:unknown_type"])
def test_malformed_detail_answer_retries_bounded_instead_of_failing(tmp_path,
                                                                     code):
    """2026-09-30: one malformed medication-detail reply (choice outside
    the criteria, a bad distribution, ...) must not kill the job after a
    single attempt — it retries within the bounded budget; a
    whole-envelope protocol error (test_nonretryable_jev_error_fails_bounded)
    still fails at once. Every per-answer code is ``qid:reason``."""
    class DetailFlaky(_FakeJev):
        def evaluate(self, state, questions, deadline):
            if set(questions) & set(jev.MED_DETAIL_QUESTIONS):
                self.requests_made += 1
                self.last_error = jev.JevError("protocol_error", code)
                raise self.last_error
            return super().evaluate(state, questions, deadline)

    db = _seeded(tmp_path)
    fake = DetailFlaky()
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300, jev_client=fake, llm_fn=_llm)
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert job["state"] == "pending" and job["attempts"] == 1
    for _ in range(8):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                         time.monotonic() + 300, jev_client=fake, llm_fn=_llm)
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert job["state"] == "failed" and job["attempts"] > 1   # bounded, not endless
    db.close()


@pytest.mark.parametrize("code,expected", [
    ("relation:probabilities_invalid", "retry"),
    ("status:probabilities_invalid", "retry"),
    ("has_symptom_state:probabilities_invalid", "retry"),
    ("claim_0:choice_invalid", "retry"),
    ("answers_missing", "failed"),
    ("", "failed")])
def test_per_answer_defect_retries_at_every_jev_site(code, expected):
    """2026-10-10: a loop-relation reply with a bad distribution failed
    its job at once — the 2026-09-30 per-answer rule lived only at the
    medication-detail and coverage sites. The shared classifier now
    applies it to fact/claim audit, propositions, loops and QC alike;
    whole-envelope protocol errors stay terminal."""
    error = jev.JevError("protocol_error", code)
    assert semantic_jev.failure_class(error) == expected


def test_malformed_claim_audit_answer_retries_bounded(tmp_path):
    class ClaimFlaky(_FakeJev):
        def evaluate(self, state, questions, deadline):
            for qid, q in questions.items():
                if q.get("criteria") == jev.CLAIM_SUPPORT_OPTIONS:
                    self.requests_made += 1
                    self.last_error = jev.JevError(
                        "protocol_error", f"{qid}:probabilities_invalid")
                    raise self.last_error
            return super().evaluate(state, questions, deadline)

    db = _seeded(tmp_path)
    fake = ClaimFlaky()
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300, jev_client=fake, llm_fn=_llm)
    job = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert job["state"] == "pending" and job["attempts"] == 1
    db.close()


def test_claims_share_requests_up_to_the_configured_size():
    bundle, summary = _claim_case(13)
    client = _ClaimCapture(choice_map={"claim_1_12": "not_supported"})
    findings, evaluated = semantic_audit.audit_claims(client, bundle, summary,
                                             time.monotonic() + 60, 0.7, 12)
    assert evaluated
    assert [len(q) for _, q in client.sent] == [12, 1]
    assert len(summary["_claim_audit"]) == 13
    assert findings == [{"code": "claim_not_supported", "claim": "claim_1_12",
                         "confidence": 0.9}]


def test_each_question_names_only_its_own_claim_and_evidence():
    bundle, summary = _claim_case(2)
    client = _ClaimCapture()
    semantic_audit.audit_claims(client, bundle, summary, time.monotonic() + 60)
    [(state, questions)] = client.sent
    assert state["target"]["claims"] == [{"id": "claim_1_0", "text": "合成の主張0"},
                                         {"id": "claim_1_1", "text": "合成の主張1"}]
    assert {e["id"] for e in state["context"]} == {
        "ev_0", "ev_0_ctx", "ev_0_meta", "ev_1", "ev_1_ctx", "ev_1_meta"}
    first = questions["claim_1_0"]["instructions"]
    assert "id claim_1_0 " in first and "entries: ev_0, ev_0_ctx, ev_0_meta)" in first
    assert "ev_1" not in first
    assert "合成の主張0" not in first                 # claim text stays data


def test_a_shared_source_is_sent_once():
    bundle, summary = _claim_case(3, shared=True)
    client = _ClaimCapture()
    semantic_audit.audit_claims(client, bundle, summary, time.monotonic() + 60)
    [(state, questions)] = client.sent
    assert [e["id"] for e in state["context"]] == [
        "ev_shared", "ev_shared_ctx", "ev_shared_meta"]
    assert all("entries: ev_shared, ev_shared_ctx, ev_shared_meta)" in q["instructions"]
               for q in questions.values())


def test_a_claim_without_evidence_cites_none():
    bundle, summary = _claim_case(1)
    summary["claims"][0]["evidence_refs"] = []
    client = _ClaimCapture()
    semantic_audit.audit_claims(client, bundle, summary, time.monotonic() + 60)
    [(state, questions)] = client.sent
    assert state["context"] == []
    assert "(state.context entries: none)" in questions["claim_1_0"]["instructions"]


def test_a_failed_request_leaves_the_audit_unevaluated():
    bundle, summary = _claim_case(3)
    client = _ClaimCapture(fail_on=2, choice_map={"claim_1_1": "contradicts"})
    findings, evaluated = semantic_audit.audit_claims(client, bundle, summary,
                                             time.monotonic() + 60, 0.7, 2)
    assert not evaluated
    assert findings == [{"code": "claim_contradicts", "claim": "claim_1_1",
                         "confidence": 0.9},
                        {"code": "support_unevaluated", "claim": "claim_1_2"}]
    assert client.last_error.kind == "transport"


@pytest.mark.parametrize("size", [-1, 0, 49, True, 1.5, "12", None])
def test_an_invalid_request_size_leaves_the_audit_unevaluated(size):
    bundle, summary = _claim_case(2)
    client = _ClaimCapture()
    findings, evaluated = semantic_audit.audit_claims(client, bundle, summary,
                                                time.monotonic() + 60, 0.7, size)
    assert (findings, evaluated) == ([{"code": "support_unevaluated"}], False)
    assert client.sent == []


def test_the_drain_passes_the_configured_request_size(tmp_path, monkeypatch):
    seen = []
    real = semantic_audit.audit_claims

    def audit_spy(*args):
        seen.append(args[5])
        return real(*args)

    monkeypatch.setattr(semantic_audit, 'audit_claims', audit_spy)
    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow", max_questions_per_request=5),
                               {"errors": []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm)
        assert out["done"] == 1 and seen and set(seen) == {5}
    finally:
        db.close()
