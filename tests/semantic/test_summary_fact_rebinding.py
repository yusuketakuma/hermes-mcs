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
from semantic_testkit import (NO_FACTS, _FakeJev, _PassJev, _canonical_cfg, _cfg, _kind_rows, _llm, _llm_v2,
                              _seeded, _seeded_two)

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


def _prove_cloud_summaries(db):
    """Attach settled synthetic cloud receipts to direct-cloud summary fixtures."""
    import inference_dependency_loss
    import mcs_util
    scfg = semantic_policy.runtime_config(_canonical_cfg())[0]
    generation = runtime.execution_generation(scfg)
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    roots = [m["message_id"] for m in bundle["members"] if m.get("parent_id") is None]
    assert len(roots) == 1
    occurrences = []
    for member in sorted(bundle["members"], key=lambda m: m["message_id"]):
        binding = inference_dependency_loss.source_binding(db.db, 1, member["message_id"])
        assert binding is not None
        occurrences.append((member["message_id"], binding))
    source = mcs_util.payload_hash(occurrences)
    for row in (_kind_rows(db, semantic_policy.KIND_SUMMARY)
                 + _kind_rows(db, semantic_policy.KIND_CANDIDATE)):
        meta = json.loads(row["meta"])
        reservation_id = f"synthetic-summary-receipt-{row['artifact_id']}"
        owner = {"semantic_target_id": row["message_id"], "semantic_thread_root": roots[0],
                 "semantic_source": source, "semantic_generation": generation,
                 "semantic_phase": "summary"}
        db.artifact_add("cloud_usage_reserve", "{}", project_id=1, message_id=row["message_id"],
                        meta={"reservation_id": reservation_id, "route": "semantic_backlog",
                              "owner": owner})
        db.artifact_add("cloud_usage_result", "{}", project_id=1, message_id=row["message_id"],
                        meta={"reservation_id": reservation_id})
        meta["inference_requests"] = [reservation_id]
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                      (json.dumps(meta, ensure_ascii=False), row["artifact_id"]))
    db.db.commit()


@pytest.fixture
def cloud_over_local(tmp_path, monkeypatch):
    """Both members PASS with cloud summaries over facts of local model A."""
    # the stub never calls llm_chat, which is what resets a failure left by an earlier test
    monkeypatch.setattr(semantic_transport, "_LLM_FAILURE", None)
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    _run(db, _Model(), rounds=2)
    before = {mid: _summary(db, mid) for mid in (1, 2)}
    assert all(before.values()) and _state(db) == "done"
    for row in _kind_rows(db, semantic_policy.KIND_SUMMARY):
        assert json.loads(row["meta"])["provider_provenance"]["providers"] == ["anthropic"]
    _prove_cloud_summaries(db)
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
@pytest.mark.parametrize("restored", [False, True])
def test_failed_regeneration_is_regenerated_on_resume_not_reaudited(
        cloud_over_local, monkeypatch, mode, fails, restored):
    """A PENDING stub written by a failed regeneration does not hide which facts the old summary had.

    The audit written with the summary decides, not the later stub: when the
    earlier local model is restored and redoes the same facts, the earlier
    summary is current again and re-audited (the stub is PENDING) without a
    summarizer call, although no candidate is left to fall back on."""
    db, before = cloud_over_local
    _change_local_model(db, monkeypatch)
    model = _FailSummary(fails, mode)
    _run(db, model)
    assert _state(db) == "pending" and _summary(db, 1) is None
    stub = _kind_rows(db, semantic_policy.KIND_AUDIT, 1)[-1]
    assert json.loads(stub["content"])["status"] == "PENDING"
    assert "claim_input_binding" not in json.loads(stub["meta"])
    failed = model.summaries()
    if restored:
        db.db.execute("DELETE FROM artifacts WHERE kind=? AND message_id=1", (semantic_policy.KIND_CANDIDATE,))
        db.db.commit()
        monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        jev = _run(db, model, rounds=2)
        assert _state(db) == "done" and model.summaries() == failed and _claim_audits(jev) == 1
        assert _summary(db, 2) == before[2] and _claims(db, _summary(db, 1)) == _claims(db, before[1])
        return
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
    assert _claim_audits(jev) == 1       # the candidate was audited again, once
    assert _claims(db, after) == _claims(db, before[1])
    assert {mid: _summary(db, mid) for mid in (2,)} == {2: before[2]}


class _LocalSummaryModel(_Model):
    """Facts from the cloud, summaries from the local model."""

    def __call__(self, prompt):
        self.calls.append(prompt)
        semantic_transport._record_inference("local" if "要約器" in prompt else "anthropic")
        return _llm_v2(prompt)


@pytest.mark.parametrize("variant,stage", [
    ("reword", "interrupted"), ("identical", "interrupted"), ("nochange", "interrupted"),
    ("local_summary", "interrupted"), ("action", "completed"), ("local_summary", "completed")])
def test_empty_repair_candidate_over_earlier_facts(tmp_path, monkeypatch, variant, stage):
    """An empty repair holds nothing once its facts are redone or its result was committed.

    ``interrupted``: the audit of the repaired text never ran. ``completed``:
    the repaired text was audited and its PASS committed, then the summary
    is dropped — by a fact document change that leaves the projection
    unchanged (``action``), or because a local summary is redone after a
    local model change while the cloud facts stay (``local_summary``). With
    ``local_summary``/``interrupted`` the first audit after the model change
    stops once too, so a redone pre-audit candidate follows the released one."""
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
    stop = [float("inf") if stage == "interrupted" else 0]

    def audit(client, bundle, summary, *args, **kwargs):
        if stop[0] and summary["target_message_id"] == 1:
            stop[0] -= 1
            raise runtime.LLMNotSent("synthetic_audit_stop")
        return real_audit(client, bundle, summary, *args, **kwargs)
    monkeypatch.setattr(semantic_audit, "audit_claims", audit)

    def model():
        if variant == "local_summary":
            return _LocalSummaryModel()
        return _ActionModel() if variant == "action" else _Model(reword=variant == "reword")
    try:
        _run(db, model(), rounds=2)
        if stage == "interrupted":
            assert calls == [(1, False), (1, True)] and _state(db) == "pending"
        else:
            assert calls == [(1, False), (1, True), (2, False)] and _state(db) == "done"
            assert _summary(db, 1)
        restop = (variant, stage) == ("local_summary", "interrupted")
        stop[0] = 1 if restop else 0
        if variant != "nochange":
            _change_local_model(db, monkeypatch)
        reasons = _reasons(monkeypatch)
        del calls[:]
        _run(db, model(), rounds=1 if stage == "completed" else 3)
        assert _state(db) == "done" and _summary(db, 1)
        assert "empty_repair_candidate_binding" not in reasons and (restop or not reasons)
        # reworded facts or a redone local summary: summarized once more,
        # never repaired again; otherwise the repaired candidate bound to
        # the current facts is audited
        assert [call for call in calls if call[0] == 1] == (
            [(1, False)] if variant in ("reword", "local_summary") else [])
        assert (_B_STATEMENT in _claims(db, _summary(db, 1))) is (variant == "reword")
        assert len(_kind_rows(db, semantic_policy.KIND_REPAIR, 1)) == 1
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


@pytest.mark.parametrize("consulted", [False, True])
def test_rows_without_bindings_rerun_without_calls(cloud_over_local, consulted):
    """Candidate and coverage rows written before bindings existed: nothing changed, no call.

    ``consulted``: member 1's publication receipt lacks its document hash, so
    the summary is judged again and the unbound candidate (no fact row
    written after it) is audited instead of summarizing again."""
    db, before = cloud_over_local
    for kind, key in ((semantic_policy.KIND_COVERAGE, "fact_input_binding"),
                      (semantic_policy.KIND_CANDIDATE, "claim_input_binding")):
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$." + key + "') WHERE kind=?", (kind,))
    if consulted:
        db.db.execute("UPDATE artifacts SET content=json_remove(content,'$.doc_hash') WHERE kind='v4_stage' "
                      "AND message_id=1 AND json_extract(meta,'$.stage')='s8_publish'")
    db.db.commit()
    db.semantic_seed(1, [1, 2], {"source": "history_import"})
    model = _Model()
    jev = _run(db, model, rounds=2)
    assert _state(db) == "done" and model.summaries() == 0
    assert (_claim_audits(jev), jev.requests_made > 0) == ((1, True) if consulted else (0, False))
    assert not model.calls or consulted
    assert {mid: _summary(db, mid) for mid in (1, 2)} == before


_UNSUPPORTED = "（未確認の追記）"


class _RejectMarkedJev(_PassJev):
    """Claims carrying ``_UNSUPPORTED`` are judged not supported (the audit asks for a repair)."""

    def evaluate(self, state, questions, deadline):
        out = super().evaluate(state, questions, deadline)
        for claim in ((state or {}).get("target") or {}).get("claims") or []:
            if _UNSUPPORTED in claim.get("text", "") and claim["id"] in out["answers"]:
                out["answers"][claim["id"]]["choice"] = "not_supported"
        return out


class _RepairedModel(_ActionModel):
    """Member 1's first summary carries an unsupported addition; the repair drops it."""

    def __init__(self, action=False):
        super().__init__(reword=False)
        self.action, self.marked = action, False
        self.repairs = 0

    def __call__(self, prompt):
        if "要約器" in prompt:
            self.calls.append(prompt)
            semantic_transport._record_inference("anthropic")
            out = json.loads(_llm_v2(prompt))
            target = prompt.split("事実候補(番号付き):")[1][:200]
            self.repairs += "前回の出力は監査で不合格でした" in prompt
            if "アムロジピン" in prompt and "メトホルミン500mgが出ています" not in target:
                if not self.marked:
                    self.marked = True
                    for claim in out["claims"]:
                        claim["text"] += _UNSUPPORTED
            return json.dumps(out, ensure_ascii=False)
        if self.action:
            return super().__call__(prompt)
        return _Model.__call__(self, prompt)


def _mark_cloud(db, status):
    """Record member 1's repaired summary as cloud-produced (a proven cloud repair)."""
    db.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.provider_provenance.providers',"
        "json('[\"anthropic\"]'),'$.provider_provenance.complete',json('true')) "
        "WHERE kind=? AND message_id=1 AND json_extract(meta,'$.audit_status')=?",
        (semantic_policy.KIND_SUMMARY, status))
    db.db.commit()


@pytest.mark.parametrize("case", ["document_changed_cloud_current", "identical_unproven_repair"])
def test_repaired_summary_is_not_replaced_by_its_rejected_candidate(tmp_path, monkeypatch, case):
    """The pre-audit candidate of a repaired summary was rejected by its audit: never reused.

    When the repaired PASS summary is dropped (its fact document changed, or
    the local model changed and the repair has no proven repair evidence),
    the summarizer runs once without a repair; the spent repair is not repeated."""
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    try:
        model = _RepairedModel()
        _run(db, model, rounds=3, jev=_RejectMarkedJev())
        assert _state(db) == "done" and model.marked
        repaired = _summary(db, 1)
        assert repaired and _UNSUPPORTED not in _claims(db, repaired)
        repairs = len(_kind_rows(db, semantic_policy.KIND_REPAIR, 1))
        assert repairs >= 1
        candidates = _kind_rows(db, semantic_policy.KIND_CANDIDATE, 1)
        assert _UNSUPPORTED in candidates[-1]["content"]
        if case == "document_changed_cloud_current":
            # the repaired summary counts as cloud-produced: kept across the model change
            _mark_cloud(db, "PASS")
        _change_local_model(db, monkeypatch)
        model = _RepairedModel(action=case == "document_changed_cloud_current")
        model.marked = True
        _run(db, model, rounds=2, jev=_RejectMarkedJev())
        after = _summary(db, 1)
        assert _state(db) == "done" and after and after != repaired
        assert model.summaries() == 1 and model.repairs == 0
        row = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (after,)).fetchone()
        assert json.loads(row[0])["audit_status"] == "PASS"
        assert _UNSUPPORTED not in _claims(db, after)
        assert len(_kind_rows(db, semantic_policy.KIND_REPAIR, 1)) == repairs
    finally:
        db.close()


def _claim_audits(jev):
    return sum(any(qid.startswith("claim") for qid in questions) for questions in jev.calls)


def _s8(db, mid):
    return db.db.execute(
        "SELECT artifact_id, content FROM artifacts WHERE kind='v4_stage' AND message_id=? "
        "AND json_extract(meta,'$.stage')='s8_publish' ORDER BY artifact_id", (mid,)).fetchall()


def _latest_summary(db, mid):
    row = _kind_rows(db, semantic_policy.KIND_SUMMARY, mid)[-1]
    return row["artifact_id"], json.loads(row["meta"])["audit_status"]


@pytest.mark.parametrize("candidate", [True, False])
def test_publication_receipt_without_document_hash_is_republished_once(
        cloud_over_local, monkeypatch, candidate):
    """An S8 receipt without ``doc_hash`` cannot show the summary's document: judged again once.

    With a usable candidate: one claim audit and no summarizer call; without
    one: one summarizer call and one claim audit. An identical result keeps
    the stored summary row; the new S8 receipt carries the hash."""
    db, before = cloud_over_local
    receipts = len(_s8(db, 1))
    for row_id, content in _s8(db, 1):
        stripped = {k: v for k, v in json.loads(content).items() if k != "doc_hash"}
        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(stripped), row_id))
    if not candidate:
        db.db.execute("DELETE FROM artifacts WHERE kind=? AND message_id=1", (semantic_policy.KIND_CANDIDATE,))
    db.db.commit()
    db.semantic_seed(1, [1, 2], {"source": "history_import"})
    model = _Model()
    jev = _run(db, model, rounds=2)
    after = _summary(db, 1)
    assert _state(db) == "done" and after == before[1] and _summary(db, 2) == before[2]
    assert model.summaries() == (0 if candidate else 1) and _claim_audits(jev) == 1
    assert len(_s8(db, 1)) == receipts + 1 and "doc_hash" in json.loads(_s8(db, 1)[-1][1])
    # one-time: the next re-run reuses the republished summary without a call
    db.semantic_seed(1, [1, 2], {"source": "history_import"})
    model, jev = _Model(), _PassJev()
    _run(db, model, jev=jev)
    assert _state(db) == "done" and not model.calls and jev.requests_made == 0 and _summary(db, 1) == after


@pytest.mark.parametrize("rewritten", [False, True])
def test_without_publication_receipt_the_fact_order_decides(cloud_over_local, monkeypatch, rewritten):
    """No S8 receipt: the summary is reused only while no fact row was written after it."""
    db, before = cloud_over_local
    db.db.execute("DELETE FROM artifacts WHERE kind='v4_stage' AND message_id=1 "
                  "AND json_extract(meta,'$.stage')='s8_publish'")
    db.db.commit()
    if rewritten:
        _change_local_model(db, monkeypatch)       # identical projection, another document
        model = _ActionModel()
    else:
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        model = _Model()
    jev = _run(db, model, rounds=2)
    assert _state(db) == "done"
    if rewritten:
        assert _latest_summary(db, 1)[0] != before[1] and _claim_audits(jev) >= 1
    else:
        assert not model.calls and jev.requests_made == 0 and _latest_summary(db, 1)[0] == before[1]


def test_needs_review_summary_over_another_document_is_regenerated(tmp_path, monkeypatch):
    """A reused NEEDS_REVIEW summary must also match the current fact document."""
    class Reject(_PassJev):
        def evaluate(self, state, questions, deadline):
            out = super().evaluate(state, questions, deadline)
            target = (state or {}).get("target") or {}
            for claim in target.get("claims") or []:
                if "アムロジピン" in claim.get("text", "") and claim["id"] in out["answers"]:
                    out["answers"][claim["id"]]["choice"] = "not_supported"
            return out

    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")
    db = _seeded_two(tmp_path)
    try:
        _run(db, _Model(), rounds=3, jev=Reject())
        before, status = _latest_summary(db, 1)
        assert status == "NEEDS_REVIEW"
        repairs = len(_kind_rows(db, semantic_policy.KIND_REPAIR, 1))
        _mark_cloud(db, "NEEDS_REVIEW")      # kept across the model change by its producer
        _change_local_model(db, monkeypatch)
        model = _ActionModel()
        _run(db, model, rounds=2, jev=Reject())
        after, status = _latest_summary(db, 1)
        # regenerated over the current document; the spent repair is not repeated
        assert after != before and status == "NEEDS_REVIEW" and model.summaries() == 1
        assert len(_kind_rows(db, semantic_policy.KIND_REPAIR, 1)) == repairs
    finally:
        db.close()


def _run_legacy(db, llm, jev):
    db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
    db.db.commit()
    semantic_drain.run_due(db, _cfg("enforce", budget=100, loop_mode="off"), {"errors": []},
                           time.monotonic() + 300, jev_client=jev, llm_fn=llm)


def _legacy_passed(db, mid):
    scfg = semantic_policy.runtime_config(_cfg("enforce", budget=100, loop_mode="off"))[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [mid])
    member = next(m for m in bundle["members"] if m["message_id"] == mid)
    return semantic_send_gate.passed_summary_current(
        db.db, 1, mid, bundle["source_fingerprint"], policy, member["revision"], "enforce", bundle=bundle)


def test_unbound_prior_pass_is_reaudited_after_reseed(tmp_path, monkeypatch):
    """An audit stored without its summary id or claim binding cannot prove which
    summary and facts it covered: the stored summary is audited again over the
    current facts and published only from that new, bound pair."""
    import semantic_job_processor

    db = _seeded(tmp_path)
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return _llm(prompt)

    try:
        _run_legacy(db, llm, _FakeJev())
        assert db.job_state("semantic", 1, 1) == "done" and _legacy_passed(db, 1)
        before = db.artifacts("semantic_audit", message_id=1)[-1]
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.summary_artifact_id',"
                      "'$.claim_input_binding') WHERE artifact_id=?", (before["artifact_id"],))
        db.db.commit()
        assert not _legacy_passed(db, 1)
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        assert db.job_state("semantic", 1, 1) == "pending"

        reasons = []
        real_why = semantic_job_processor._why
        monkeypatch.setattr(semantic_job_processor, "_why",
                            lambda diagnostics, reason: reasons.append(reason) or real_why(diagnostics, reason))
        summaries = sum("要約器" in prompt for prompt in prompts)
        jev = _FakeJev()
        for _ in range(3):
            _run_legacy(db, llm, jev)
            if db.job_state("semantic", 1, 1) != "pending":
                break
        assert "prior_pass_unverified" not in reasons
        assert db.job_state("semantic", 1, 1) == "done"
        assert sum("要約器" in prompt for prompt in prompts) == summaries   # the stored summary is reused
        assert jev.requests_made >= 1                                     # ... and audited again
        assert _legacy_passed(db, 1)
        audit = db.artifacts("semantic_audit", message_id=1)[-1]
        summary = db.artifacts("semantic_summary", message_id=1)[-1]
        assert audit["artifact_id"] > before["artifact_id"]
        assert json.loads(audit["meta"])["summary_artifact_id"] == summary["artifact_id"]
    finally:
        db.close()


def _legacy_skipped(db, mid):
    scfg = semantic_policy.runtime_config(_cfg("enforce", budget=100, loop_mode="off"))[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    bundle = semantic_store.thread_bundle(db.db, 1, 1, [mid])
    member = next(m for m in bundle["members"] if m["message_id"] == mid)
    return semantic_send_gate.verified_skipped_summary_current(
        db.db, 1, mid, bundle["source_fingerprint"], policy, member["revision"], "enforce", bundle=bundle)


@pytest.mark.parametrize("tamper", ["binding", "summary_text"])
def test_unverifiable_prior_pass_is_not_cleared_by_skipped_reaudit(tmp_path, tamper):
    """With Jev skipped, an unverifiable PASS is not audited at all: like an
    unbound PENDING prior, the skipped re-audit cannot clear it, so the target
    stays pending (``prior_audit_unverified``) and nothing becomes publishable
    until a real audit runs."""
    db = _seeded(tmp_path)
    try:
        _run_legacy(db, _llm, _FakeJev())
        assert db.job_state("semantic", 1, 1) == "done" and _legacy_passed(db, 1)
        before = db.artifacts("semantic_audit", message_id=1)[-1]
        if tamper == "binding":
            db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.summary_artifact_id',"
                          "'$.claim_input_binding') WHERE artifact_id=?", (before["artifact_id"],))
        else:
            summary = db.artifacts("semantic_summary", message_id=1)[-1]
            content = json.loads(summary["content"])
            content["limitations"] = [*content.get("limitations", []), "synthetic-tamper"]
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps(content, ensure_ascii=False), summary["artifact_id"]))
        db.db.commit()
        assert not _legacy_passed(db, 1)
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        for _ in range(2):
            _run_legacy(db, _llm, None)
            assert db.job_state("semantic", 1, 1) == "pending"
            assert not _legacy_passed(db, 1) and not _legacy_skipped(db, 1)
        audit = db.artifacts("semantic_audit", message_id=1)[-1]
        assert json.loads(audit["content"])["status"] == "PENDING"
        assert "prior_audit_unverified" in audit["content"]
    finally:
        db.close()


_PASS_TAMPER = {
    # One reason each for the send gate to refuse the stored PASS pair while
    # its summary id and claim binding stay in place.
    "summary_hash": ("semantic_audit", "json_remove(meta,'$.summary_hash')", None),
    "audit_content_hash": ("semantic_audit", "json_remove(meta,'$.audit_content_hash')", None),
    "audit_schema": ("semantic_audit", "json_remove(meta,'$.schema')", None),
    "audit_revision": ("semantic_audit", "json_set(meta,'$.target_revision',-1)", None),
    "audit_mode": ("semantic_audit", "json_set(meta,'$.publication_mode','assist')", None),
    "summary_stale": ("semantic_summary", "json_set(meta,'$.stale',1)", None),
    "summary_text": ("semantic_summary", None, "synthetic-tamper"),
    "audit_target": ("semantic_audit", None, 2),
}


@pytest.mark.parametrize("tamper", sorted(_PASS_TAMPER))
def test_reaudited_unverifiable_pass_writes_a_new_pair(tmp_path, tamper):
    """A PASS pair the send gate refuses is audited again and that PASS is written as a new pair.

    The pair keeps its summary id and claim binding but fails another check
    of the send gate. The re-audit's PASS equals the stored outcome, yet it
    is not dropped as a duplicate: one new, verifiable pair is written,
    which publishes the summary, and a further pass writes nothing."""
    db = _seeded(tmp_path)
    try:
        _run_legacy(db, _llm, _FakeJev())
        assert db.job_state("semantic", 1, 1) == "done" and _legacy_passed(db, 1)
        kind, meta_sql, content_change = _PASS_TAMPER[tamper]
        row = db.artifacts(kind, message_id=1)[-1]
        if meta_sql is not None:
            db.db.execute(f"UPDATE artifacts SET meta={meta_sql} WHERE artifact_id=?", (row["artifact_id"],))
        else:
            content = json.loads(row["content"])
            if kind == "semantic_summary":
                content["limitations"] = [*content.get("limitations", []), content_change]
            else:
                content["target_message_id"] = content_change
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps(content, ensure_ascii=False), row["artifact_id"]))
        db.db.commit()
        assert not _legacy_passed(db, 1)
        before = db.artifacts("semantic_audit", message_id=1)[-1]["artifact_id"]
        db.semantic_seed(1, [1, 2], {"source": "history_import"})
        jev = _FakeJev()
        for _ in range(3):
            _run_legacy(db, _llm, jev)
            if db.job_state("semantic", 1, 1) != "pending":
                break
        assert jev.requests_made >= 1
        audits = db.artifacts("semantic_audit", message_id=1)
        assert audits[-1]["artifact_id"] > before
        assert db.job_state("semantic", 1, 1) == "done" and _legacy_passed(db, 1)
        summary = db.artifacts("semantic_summary", message_id=1)[-1]
        assert json.loads(audits[-1]["meta"])["summary_artifact_id"] == summary["artifact_id"]
        _run_legacy(db, _llm, jev)
        assert len(db.artifacts("semantic_audit", message_id=1)) == len(audits)
    finally:
        db.close()
