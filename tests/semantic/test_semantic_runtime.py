"""Durable execution-boundary contracts for the semantic worker."""
import json
import time
from pathlib import Path

import pytest

from ledger import Ledger
import semantic
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_testkit import _cfg, _llm, _seeded


def _job(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.job_add("semantic", 1, 1, payload={
        "targets": [1], "generation": "g1", "source_generation": "s1"})
    row = db.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
    return db, dict(row)


def test_old_worker_cannot_transition_reused_job_id(tmp_path):
    db, row = _job(tmp_path)
    token = runtime.JobToken.from_row(row)
    db.db.execute(
        "UPDATE fetch_jobs SET payload=? WHERE job_id=?",
        (json.dumps({"targets": [1], "generation": "g2",
                     "source_generation": "s2"}), token.job_id))
    db.db.commit()

    assert not runtime.job_matches(db, token)
    assert not runtime.transition(db, token, "retry", max_attempts=2)
    current = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE job_id=?",
        (token.job_id,)).fetchone()
    assert current["state"] == "pending" and current["attempts"] == 0
    db.close()


def test_guard_rechecks_config_generation_and_off(tmp_path):
    db, row = _job(tmp_path)
    token = runtime.JobToken.from_row(row)
    cfg_path = Path(tmp_path) / "config.json"
    cfg = {"semantic": {"mode": "shadow",
                         "daily_request_budget": 10, "project_ids": [1]}}
    cfg_path.write_text(json.dumps(cfg))
    expected = runtime.config_generation(cfg)
    common = {
        "ledger": db, "token": token, "deadline": time.monotonic() + 10,
        "expected_config_generation": expected, "expected_mode": "shadow",
        "cfg_path": str(cfg_path), "load_cfg": semantic.load_config,
        "parse_cfg": semantic.semantic_config}
    runtime.guard(**common)

    cfg_path.write_text(json.dumps({"semantic": {"mode": "off"}}))
    with pytest.raises(runtime.RuntimeOff):
        runtime.guard(**common)
    cfg_path.write_text(json.dumps({"semantic": {
        "mode": "shadow", "daily_request_budget": 11, "project_ids": [1]}}))
    with pytest.raises(runtime.RuntimeStale):
        runtime.guard(**common)
    db.close()


def test_usage_reservation_survives_unknown_post_failure(tmp_path):
    db, row = _job(tmp_path)
    token = runtime.JobToken.from_row(row)
    calls = []

    def post(body, timeout):
        calls.append(timeout)
        raise OSError("connection dropped after send")

    client = jev.JevClient(api_key="synthetic", post_fn=post,
                           max_attempts=1)
    reserve = runtime.usage_reserver(
        db, token, kind="semantic_usage", model=jev.JEV_MODEL,
        project_id=1, message_id=1)
    guarded, _ = runtime.bind_jev(client, lambda stage: None, reserve)
    with pytest.raises(jev.JevError) as error:
        guarded.evaluate({}, {"q": jev.noul_question("i", "t", "f")},
                         time.monotonic() + 10)
    assert error.value.kind == "transport"
    assert calls and semantic.jev_usage_today(db) == 1
    db.close()


def test_retryable_http_failures_reach_terminal_attempt_bound(tmp_path, monkeypatch):
    # Pin the clock to JST noon: advancing +301s per drain must not cross
    # the daily-budget boundary that jev_usage_today() measures against.
    import math
    now = time.time()
    jst_noon = math.floor((now + 9 * 3600) / 86400) * 86400 \
        - 9 * 3600 + 12 * 3600
    clock = [jst_noon]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    db = _seeded(tmp_path)
    calls = []

    def post(body, timeout):
        calls.append(1)
        return 503, {}, b"service unavailable"

    client = jev.JevClient(api_key="synthetic", post_fn=post,
                           max_attempts=1)
    result = {"errors": []}
    for _ in range(6):
        clock[0] += 301  # allow the durable outage circuit to cool down
        db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                      "WHERE kind='semantic'")
        db.db.commit()
        semantic.run_due(db, _cfg("shadow"), result,
                         time.monotonic() + 30,
                         jev_client=client, llm_fn=_llm)
    row = db.db.execute(
        "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert row["state"] == "failed" and row["attempts"] == 6
    assert len(calls) == semantic.jev_usage_today(db)
    db.close()


def test_status_reports_oldest_pending_age_without_writes(tmp_path, monkeypatch):
    import semantic
    from semantic_testkit import _seeded
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET created_at=100, next_try=9999 WHERE kind='semantic'")
        monkeypatch.setattr(semantic.time, "time", lambda: 150)
        changes = db.db.total_changes
        assert semantic.status_report(db)["oldest_pending_job_age_s"] == 50
        assert db.db.total_changes == changes
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
        assert semantic.status_report(db)["oldest_pending_job_age_s"] is None
    finally:
        db.close()


def test_canonical_readiness_reports_shadow_material(tmp_path):
    """The promotion readout must show how much v2 shadow material
    exists and what has passed the audit/publish stages — counts only,
    no model calls, and honest about a disabled/unavailable config."""
    db = _seeded(tmp_path)
    try:
        db.artifact_add("semantic_facts_v2", "{}",
                        project_id=1, message_id=1,
                        meta={"coverage_status": "complete"})
        db.artifact_add("semantic_facts_v2", "{}",
                        project_id=1, message_id=2,
                        meta={"coverage_status": "incomplete",
                              "needs_review": True})
        db.artifact_add("semantic_facts_audit", "{}",
                        project_id=1, message_id=1,
                        meta={"audit_status": "PASS"})
        db.artifact_add("semantic_facts_audit", "{}",
                        project_id=1, message_id=2,
                        meta={"audit_status": "NEEDS_REVIEW"})
        db.artifact_add("canonical_projection", "{}",
                        project_id=1, message_id=1)
        r = semantic._canonical_readiness(
            db, _cfg("shadow", fact_source="shadow"))
        assert r["available"] and r["fact_source"] == "shadow"
        assert r["shadow_v2_docs"] == 2
        assert r["v2_coverage_complete"] == 1
        assert r["v2_needs_review"] == 1
        assert r["fact_audits"] == {"PASS": 1, "NEEDS_REVIEW": 1}
        assert r["canonical_projection"] == 1
        assert r["semantic_facts_v4"] == 0

        assert semantic._canonical_readiness(db, None)["available"] \
            is False
    finally:
        db.close()


@pytest.mark.parametrize("failure", [False, True])
def test_drain_reports_measured_job_work_and_preserves_off(tmp_path, monkeypatch, failure):
    from semantic_testkit import _FakeJev
    db = _seeded(tmp_path)
    client = _FakeJev()
    # controllable clock: the fake LLM spends 2s of wall time per call,
    # every other phase is instant — the phase-split contract is what
    # is asserted, not a tick count
    now = [10.0]
    monkeypatch.setattr(semantic.time, "perf_counter", lambda: now[0])
    if failure:
        def broken(*args, **kwargs):
            client.requests_made += 1
            raise OSError("synthetic")
        monkeypatch.setattr(semantic, "_process_job", broken)
        llm_fn = _llm
    else:
        def llm_fn(prompt):
            now[0] += 2.0
            return _llm(prompt)
    try:
        before = db.db.total_changes
        off = semantic.run_due(db, _cfg("off"), {"errors": []},
                               time.monotonic() + 60, jev_client=client)
        assert "job_metrics" not in off and db.db.total_changes == before
        result = semantic.run_due(db, _cfg(), {"errors": []},
                                  time.monotonic() + 60,
                                  jev_client=client, llm_fn=llm_fn)
        metric, = result["job_metrics"]
        if failure:
            assert metric["llm_s"] == 0 and metric["elapsed_s"] == 0
        else:
            assert metric["llm_s"] >= 2.0
            assert metric["elapsed_s"] >= metric["llm_s"]
        assert (metric["elapsed_s"]
                == pytest.approx(metric["llm_s"] + metric["jev_s"]
                                 + metric["post_s"]))
        assert result["elapsed_s"] == pytest.approx(now[0] - 10.0)
        assert metric["jev_requests"] == semantic.jev_usage_today(db) > 0
        assert metric["usage"]["reported_requests"] == 0
        assert metric["usage"]["unreported_requests"] == metric["jev_requests"]
        assert metric["project_id"] == 1 and metric["generation"]
        assert metric["job_age_s"] >= result["oldest_pending_job_age_s"] >= 0
        assert metric["status"] == ("error" if failure else "done")
        assert "テスト患者" not in json.dumps(result, ensure_ascii=False)
    finally:
        db.close()


def test_real_client_usage_reaches_offline_report_through_worker(tmp_path):
    import semantic_evaluation as evaluation
    from semantic_testkit import _record, MANIFEST, CRITERIA
    db = _seeded(tmp_path)
    calls = []

    def post(body, timeout):
        calls.append(body)
        answers = {}
        for qid, question in body["questions"].items():
            if question["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": .9}
            else:
                options = list(question["criteria"])
                choice = "planned" if qid == "status" else options[0]
                answers[qid] = {"type": "choice", "choice": choice,
                                "confidence": 1,
                                "probabilities": {o: int(o == choice) for o in options}}
        return 200, {}, json.dumps({"model": jev.JEV_MODEL, "answers": answers,
                                    "usage": {"input_tokens": 7, "output_tokens": 2}}).encode()

    try:
        client = jev.JevClient(api_key="synthetic", post_fn=post, max_attempts=1)
        result = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                                  jev_client=client, llm_fn=_llm)
        assert result["done"] == 1 and len(calls) > 1
        metric, = result["job_metrics"]
        assert metric["usage"] == {"input_tokens": 7 * len(calls),
                                    "output_tokens": 2 * len(calls),
                                    "reported_requests": len(calls), "unreported_requests": 0}
        assert semantic.jev_usage_today(db) == len(calls)
        paths = {name: tmp_path / name for name in ("cases", "manifest", "criteria", "runs", "report")}
        paths["cases"].write_text(json.dumps(_record("human")) + "\n")
        paths["manifest"].write_text(json.dumps(MANIFEST))
        paths["criteria"].write_text(json.dumps(CRITERIA))
        paths["runs"].write_text(json.dumps({"account_id": "synthetic", "result": {
            "run_id": 1, "elapsed_s": result["elapsed_s"], "semantic": result}}) + "\n")
        assert evaluation.main(["--input", str(paths["cases"]), "--manifest", str(paths["manifest"]),
                                "--criteria", str(paths["criteria"]), "--runs", str(paths["runs"]),
                                "--output", str(paths["report"])]) == 0
        report = json.loads(paths["report"].read_text())
        usage = report["runtime"]["jev_usage"]["run"]
        assert usage["complete_observations"] == 1
        assert usage["tokens"]["input_tokens"]["complete_p50"] == 7 * len(calls)
    finally:
        db.close()


def test_pre_audit_candidate_survives_interruption_and_is_not_replaced(tmp_path, monkeypatch):
    from semantic_testkit import _FakeJev
    db = _seeded(tmp_path)
    original_audit = semantic.audit_claims

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("synthetic interruption after candidate save")

    monkeypatch.setattr(semantic, "audit_claims", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                             jev_client=_FakeJev(), llm_fn=_llm)
        initial = [dict(r) for r in db.artifacts("semantic_candidate", message_id=1)]
        assert len(initial) == 1
    finally:
        db.close()
    monkeypatch.setattr(semantic, "audit_claims", original_audit)
    db = Ledger(str(tmp_path / "ledger.db"))
    generated = []
    def no_regeneration(prompt):
        if "要約器" in prompt:
            generated.append(prompt)
        return _llm(prompt)

    try:
        result = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 60,
                                  jev_client=_FakeJev(), llm_fn=no_regeneration)
        assert result["done"] == 1
        assert len(generated) == 1  # only the not-yet-processed reply
        assert [dict(r) for r in db.artifacts("semantic_candidate", message_id=1)] == initial
        assert db.artifacts("semantic_summary", message_id=1)
    finally:
        db.close()


def test_guarded_llm_first_call_always_dispatches_next_needs_reserve(monkeypatch):
    import semantic_runtime as runtime
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    calls = []
    guarded = runtime.guarded_llm(lambda prompt: calls.append(prompt) or "ok",
                                  lambda stage: None, clock[0] + 100)
    assert guarded("first") == "ok"          # 100 s left < reserve: still sent
    with pytest.raises(runtime.RuntimeBudgetShort):
        guarded("second")                    # follow-up needs the reserve
    assert calls == ["first"]
    assert issubclass(runtime.RuntimeBudgetShort, runtime.RuntimeBudget)
