"""Summary, candidate and coverage reuse is judged against the facts current now (synthetic).

A cloud-produced summary over locally produced facts stays current when the
local model changes, but the facts it was audited over are redone at the same
fingerprint.  Every attempt decides from durable state (the recorded input
binding versus the current facts) — never from what one pass remembers — so a
pass that stops between any two stages cannot leave a summary deferred forever.
"""
import json
import time

import pytest

import semantic_audit
import semantic_drain
import semantic_policy
import semantic_runtime as runtime
import semantic_send_gate
import semantic_store
import semantic_transport
from semantic_testkit import NO_FACTS, _PassJev, _canonical_cfg, _kind_rows, _llm_v2, _seeded_two

_B_STATEMENT = "アムロジピン5mgの継続が記載されている"


class _Model:
    """Local facts (worded per local model when ``reword``); the summary is cloud's."""

    def __init__(self, reword=True):
        self.reword = reword
        self.calls = []
        self.fail_member_two = 0

    def __call__(self, prompt):
        self.calls.append(prompt)
        if "要約器" in prompt:
            semantic_transport._record_inference("anthropic")
            return _llm_v2(prompt)
        semantic_transport._record_inference("local")
        if "メトホルミン" in prompt and self.fail_member_two:
            self.fail_member_two -= 1
            return "not json"
        if self.reword and "アムロジピン" in prompt and semantic_transport.LLM_MODEL == "synthetic-local-b":
            fact = {"statement": _B_STATEMENT, "kind": "medication_event", "action": "continue",
                    "subject_role": "patient", "polarity": "affirmed", "workflow_status": "performed",
                    "importance": "T1", "evidence_quote": "アムロジピン5mgを継続"}
            return json.dumps({"facts": [fact], "category_presence": dict(NO_FACTS, medication="one")},
                              ensure_ascii=False)
        return _llm_v2(prompt)

    def summaries(self):
        return sum("要約器" in prompt for prompt in self.calls)


def _run(db, model, rounds=1, jev=None):
    jev = jev or _PassJev()
    for _ in range(rounds):
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        semantic_drain.run_due(db, _canonical_cfg(), {"errors": []}, time.monotonic() + 300,
                               jev_client=jev, llm_fn=model)
    return jev


def _summary(db, mid):
    scfg = semantic_policy.runtime_config(_canonical_cfg())[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [mid])
    row = semantic_send_gate.current_summary(db.db, 1, mid, bundle, policy, fact_source="canonical")
    return row["artifact_id"] if row else None


def _state(db):
    return db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]


def _claims(db, artifact_id):
    """The stored claims of a summary row (the rendered layer repeats facts on its own)."""
    row = db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return json.dumps(json.loads(row[0]).get("claims"), ensure_ascii=False)


def _reasons(monkeypatch):
    import semantic_job_processor
    seen = []
    real = semantic_job_processor._why

    def why(diagnostics, reason):
        seen.append(reason)
        return real(diagnostics, reason)
    monkeypatch.setattr(semantic_job_processor, "_why", why)
    return seen


def _coverage_questions(jev):
    return sum("source_fact_coverage" in questions for questions in jev.calls)


def _change_local_model(db, monkeypatch):
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
    db.semantic_seed(1, [1, 2], {"source": "history_import"})


@pytest.fixture
def cloud_over_local(tmp_path, monkeypatch):
    """Both members PASS with cloud summaries over facts of local model A."""
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    _run(db, _Model(), rounds=2)
    before = {mid: _summary(db, mid) for mid in (1, 2)}
    assert all(before.values()) and _state(db) == "done"
    for row in _kind_rows(db, semantic_policy.KIND_SUMMARY):
        assert json.loads(row["meta"])["provider_provenance"]["providers"] == ["anthropic"]
    yield db, before
    db.close()


def test_cloud_summary_over_redone_local_facts_is_regenerated_after_an_interrupted_pass(
        cloud_over_local, monkeypatch):
    db, before = cloud_over_local
    _change_local_model(db, monkeypatch)
    model = _Model()
    # the pass that redoes member 1's facts stops at member 2, before any summary
    model.fail_member_two = 1
    summaries = len(_kind_rows(db, semantic_policy.KIND_SUMMARY))
    _run(db, model)
    assert len(_kind_rows(db, semantic_policy.KIND_SUMMARY)) == summaries and model.summaries() == 0
    _run(db, model, rounds=3)
    after = {mid: _summary(db, mid) for mid in (1, 2)}
    assert _state(db) == "done"
    assert after[1] and after[1] != before[1] and model.summaries() == 1
    assert _B_STATEMENT in _claims(db, after[1]) and _B_STATEMENT not in _claims(db, before[1])
    # member 2's facts did not change: its cloud summary is still the current one
    assert after[2] == before[2]


def test_identically_redone_facts_keep_the_cloud_summary_and_coverage(cloud_over_local, monkeypatch):
    db, before = cloud_over_local
    _change_local_model(db, monkeypatch)
    model = _Model(reword=False)
    jev = _run(db, model, rounds=2)
    assert _state(db) == "done"
    # the local facts were redone, but to the same facts: nothing derived from them is redone
    assert model.calls and model.summaries() == 0 and _coverage_questions(jev) == 0
    assert {mid: _summary(db, mid) for mid in (1, 2)} == before


def test_coverage_of_earlier_facts_is_reevaluated_and_bound(cloud_over_local, monkeypatch):
    db, _ = cloud_over_local
    before = {mid: len(_kind_rows(db, semantic_policy.KIND_COVERAGE, mid)) for mid in (1, 2)}
    _change_local_model(db, monkeypatch)
    _run(db, _Model(), rounds=2)
    assert _state(db) == "done"
    # only the member whose facts changed is evaluated again, bound to its new facts
    assert {mid: len(_kind_rows(db, semantic_policy.KIND_COVERAGE, mid)) for mid in (1, 2)} == {
        1: before[1] + 1, 2: before[2]}
    rows = _kind_rows(db, semantic_policy.KIND_COVERAGE, 1)
    assert len({json.loads(row["meta"])["fact_input_binding"] for row in rows}) == 2


def test_cloud_candidate_of_earlier_facts_is_not_reused(tmp_path, monkeypatch):
    """Interrupted between the candidate snapshot and the audit, then the facts change."""
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    try:
        real_audit = semantic_audit.audit_claims

        def not_sent(*_args, **_kwargs):
            raise runtime.LLMNotSent("synthetic_audit_stop")
        monkeypatch.setattr(semantic_audit, "audit_claims", not_sent)
        model = _Model()
        _run(db, model)
        assert model.summaries() >= 1 and not _kind_rows(db, semantic_policy.KIND_SUMMARY, 1)
        stale = _kind_rows(db, semantic_policy.KIND_CANDIDATE, 1)
        assert len(stale) == 1
        monkeypatch.setattr(semantic_audit, "audit_claims", real_audit)
        _change_local_model(db, monkeypatch)
        model = _Model()
        _run(db, model, rounds=3)
        assert _state(db) == "done" and _summary(db, 1)
        # member 1's earlier candidate was replaced, never audited against the new facts
        candidates = _kind_rows(db, semantic_policy.KIND_CANDIDATE, 1)
        assert len(candidates) == 2 and _B_STATEMENT in candidates[-1]["content"]
        assert _B_STATEMENT in _claims(db, _summary(db, 1))
    finally:
        db.close()


def test_candidate_of_current_facts_survives_an_interrupted_audit(cloud_over_local, monkeypatch):
    db, _ = cloud_over_local
    _change_local_model(db, monkeypatch)
    real_audit = semantic_audit.audit_claims

    def not_sent(*_args, **_kwargs):
        raise runtime.LLMNotSent("synthetic_audit_stop")
    monkeypatch.setattr(semantic_audit, "audit_claims", not_sent)
    model = _Model()
    _run(db, model)
    assert model.summaries() == 1
    monkeypatch.setattr(semantic_audit, "audit_claims", real_audit)
    _run(db, model, rounds=3)
    # the candidate written over the redone facts is reused: no second summary call
    assert _state(db) == "done" and model.summaries() == 1 and _summary(db, 1)


def _row(db, kind, meta):
    return db.artifact_add(kind, "{}", project_id=1, message_id=1, meta=meta)


@pytest.mark.parametrize("recorded", [None, "binding", "other"])
def test_recorded_binding_decides_and_unrecorded_rows_follow_fact_order(tmp_path, recorded):
    from inference_version import CURRENT_VERSION, VERSION_KEY
    db = _seeded_two(tmp_path)
    try:
        base = {VERSION_KEY: CURRENT_VERSION, "fingerprint": "fp", "policy_fingerprint": "policy"}
        facts = semantic_policy.KIND_FACTS

        def bound():
            return semantic_store.built_over_current_facts(
                db.db, semantic_policy.KIND_COVERAGE, 1, "fp", "policy", recorded, "binding", facts)
        _row(db, facts, base)
        _row(db, semantic_policy.KIND_COVERAGE, base)
        assert bound() is (recorded in (None, "binding"))
        _row(db, facts, base)      # the facts are rewritten after the evaluation
        assert bound() is (recorded == "binding")
    finally:
        db.close()


class _FailSummary(_Model):
    """The summarizer fails ``fails`` times: invalid output, or a stop before dispatch."""

    def __init__(self, fails, mode):
        super().__init__()
        self.fails, self.mode = fails, mode

    def __call__(self, prompt):
        if "要約器" in prompt and self.fails:
            self.fails -= 1
            self.calls.append(prompt)
            if self.mode == "not_sent":
                raise runtime.LLMNotSent("synthetic_stop")
            semantic_transport._record_inference("anthropic")
            return "not json"
        return super().__call__(prompt)


@pytest.mark.parametrize("mode,fails", [("model", 2), ("not_sent", 1)])
def test_failed_regeneration_is_regenerated_on_resume_not_reaudited(cloud_over_local, monkeypatch, mode, fails):
    """A PENDING stub written by a failed regeneration does not hide which facts the old summary had."""
    db, before = cloud_over_local
    _change_local_model(db, monkeypatch)
    model = _FailSummary(fails, mode)
    _run(db, model)
    assert _state(db) == "pending" and _summary(db, 1) is None
    failed = model.summaries()
    _run(db, model, rounds=3)
    after = _summary(db, 1)
    assert _state(db) == "done" and after and after != before[1]
    # the summarizer ran again and the stored claims are the new ones
    assert model.summaries() == failed + 1
    assert _B_STATEMENT in _claims(db, after) and _claims(db, after) != _claims(db, before[1])


class _ActionModel(_Model):
    """Model B states the fact identically but records another unprojected field."""

    def __call__(self, prompt):
        if ("要約器" not in prompt and "アムロジピン" in prompt
                and semantic_transport.LLM_MODEL == "synthetic-local-b"):
            self.calls.append(prompt)
            semantic_transport._record_inference("local")
            out = json.loads(_llm_v2(prompt))
            for fact in out.get("facts", []):
                fact["action"] = "start"
            return json.dumps(out, ensure_ascii=False)
        return super().__call__(prompt)


@pytest.mark.parametrize("interrupt", [False, True])
def test_same_projected_facts_in_another_document_republish(cloud_over_local, monkeypatch, interrupt):
    """The summary was rendered and published with the earlier document: it is not reused as is.

    Its claims are bound to the unchanged projected facts, so the bound
    pre-audit candidate is audited and rendered with the current document
    (no summarizer call), instead of deferring on the stale publication."""
    import semantic_facts
    db, before = cloud_over_local
    reasons = _reasons(monkeypatch)
    _change_local_model(db, monkeypatch)
    model = _ActionModel()
    model.fail_member_two = 1 if interrupt else 0
    jev = _run(db, model, rounds=3)
    docs = [semantic_facts.doc_hash(json.loads(row["content"]))
            for row in _kind_rows(db, semantic_policy.KIND_FACTS_V2, 1)]
    assert len(set(docs)) == 2           # the document changed, its projection did not
    after = _summary(db, 1)
    assert _state(db) == "done" and after and after != before[1]
    assert "prior_pass_unverified" not in reasons and model.summaries() == 0
    assert jev.requests_made >= 1        # the candidate was audited again
    assert _claims(db, after) == _claims(db, before[1])
    assert {mid: _summary(db, mid) for mid in (2,)} == {2: before[2]}


@pytest.mark.parametrize("variant", ["reword", "identical", "nochange"])
def test_empty_repair_candidate_over_earlier_facts(tmp_path, monkeypatch, variant):
    """An empty repair reserved over earlier facts holds nothing once the facts are redone."""
    import semantic_summary_stages
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    original = semantic_drain.summarize
    calls, emptied = [], []

    def summarize(llm, bundle, mid, *args, **kwargs):
        result = original(llm, bundle, mid, *args, **kwargs)
        calls.append((mid, bool(kwargs.get("feedback"))))
        if mid == 1 and not emptied:
            emptied.append(mid)
            summary = result[0] if isinstance(result, tuple) else result
            summary["claims"] = []       # the first summary is empty: the one repair is spent
        return result
    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, "summarize", semantic_drain.summarize)
    real_audit = semantic_audit.audit_claims
    stop = [True]

    def audit(client, bundle, summary, *args, **kwargs):
        if stop[0] and summary["target_message_id"] == 1:
            raise runtime.LLMNotSent("synthetic_audit_stop")
        return real_audit(client, bundle, summary, *args, **kwargs)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)
    try:
        _run(db, _Model(), rounds=2)
        assert calls == [(1, False), (1, True)] and _state(db) == "pending"
        stop[0] = False
        if variant != "nochange":
            _change_local_model(db, monkeypatch)
        reasons = _reasons(monkeypatch)
        del calls[:]
        _run(db, _Model(reword=variant == "reword"), rounds=3)
        assert _state(db) == "done" and _summary(db, 1) and not reasons
        # reworded facts: summarized once more from them, never repaired again;
        # otherwise the repaired candidate bound to the current facts is audited
        assert [call for call in calls if call[0] == 1] == ([(1, False)] if variant == "reword" else [])
        assert (_B_STATEMENT in _claims(db, _summary(db, 1))) is (variant == "reword")
    finally:
        db.close()


def test_legacy_fact_source_interrupted_after_a_model_change(tmp_path, monkeypatch):
    from semantic_testkit import _FakeJev, _cfg, _llm, _seeded
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded(tmp_path)
    cfg = _cfg("enforce", loop_mode="off")
    state = {"fail_extract_at": None, "extracts": 0, "summaries": 0}

    def llm(prompt):
        if "要約器" in prompt:
            state["summaries"] += 1
            semantic_transport._record_inference("anthropic")
            return _llm(prompt)
        semantic_transport._record_inference("local")
        if "事実候補抽出器" in prompt:
            state["extracts"] += 1
            if state["fail_extract_at"] == state["extracts"]:
                return "not json"
            out = json.loads(_llm(prompt))
            if semantic_transport.LLM_MODEL == "synthetic-local-b":
                out["facts"][0]["statement"] = "用量変更が書かれている"
            return json.dumps(out, ensure_ascii=False)
        return _llm(prompt)

    def run(rounds):
        for _ in range(rounds):
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            db.db.commit()
            semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                   jev_client=_FakeJev(), llm_fn=llm)
    try:
        run(2)
        assert _state(db) == "done"
        summaries = len(_kind_rows(db, semantic_policy.KIND_SUMMARY, 1))
        monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-b")
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        state.update(extracts=0, summaries=0, fail_extract_at=2)
        run(4)
        assert _state(db) == "done" and state["summaries"] >= 1
        assert len(_kind_rows(db, semantic_policy.KIND_SUMMARY, 1)) > summaries
    finally:
        db.close()


def test_rows_without_bindings_rerun_without_calls(cloud_over_local):
    """Candidate and coverage rows written before bindings existed: nothing changed, no call."""
    db, before = cloud_over_local
    for kind, key in ((semantic_policy.KIND_COVERAGE, "fact_input_binding"),
                      (semantic_policy.KIND_CANDIDATE, "claim_input_binding")):
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$." + key + "') WHERE kind=?", (kind,))
    db.db.commit()
    db.semantic_seed(1, [1, 2], {"source": "history_import"})
    model = _Model()
    jev = _run(db, model, rounds=2)
    assert _state(db) == "done" and not model.calls and jev.requests_made == 0
    assert {mid: _summary(db, mid) for mid in (1, 2)} == before
