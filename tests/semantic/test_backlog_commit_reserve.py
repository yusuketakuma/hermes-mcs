"""Backlog transports end a commit reserve before the job deadline, so a
result can wait out a tick's hold on the run lock and still be written.

A long-output call must leave enough time to reacquire the run lock
and preserve its completed work before the job deadline."""
import semantic_job_processor
import semantic_summary_stages
import writer_lock
import semantic_audit
import semantic_policy
import semantic_transport
import contextlib
import json

import pytest

import semantic
import semantic_drain
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded, _seeded_two


def _scfg(budget):
    return semantic_policy.semantic_config(_cfg("shadow", job_budget_seconds=budget))[0]


@pytest.mark.parametrize("budget,reserve", [
    (45, 0.0), (680, 0.0), (900, 220.0), (1200, 520.0), (1220, 540.0), (1260, 540.0)])
def test_the_reserve_never_squeezes_out_a_long_output_call(budget, reserve):
    """Below ~1220 s the reserve shrinks so a long-output call (plus the
    time a resumed pass needs before it) still fits; budgets that cannot
    hold one at all (the 45 s default) keep the old timing exactly."""
    assert semantic_drain._backlog_commit_reserve(_scfg(budget)) == reserve
    if reserve:
        window = budget - reserve
        assert window >= semantic_transport.LONG_MIN_CALL_S + semantic_drain._LONG_START_S


def _first_timeout(tmp_path, monkeypatch, budget, lane):
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    seen = []

    def llm(prompt, timeout=None):
        seen.append(timeout)
        return _llm(prompt)

    tmp_path.mkdir(parents=True, exist_ok=True)
    db = _seeded(tmp_path)
    try:
        if lane == "realtime":
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1) "
                    "WHERE kind='semantic'")
        semantic_drain.run_due(db, _cfg("shadow", job_budget_seconds=budget),
                         {"errors": []}, clock[0] + budget + 30,
                         jev_client=_FakeJev(), llm_fn=llm, lane=lane, max_jobs=1)
        return seen[0]
    finally:
        db.close()


@pytest.mark.parametrize("budget", [45, 900, 1260])
def test_backlog_calls_end_a_reserve_early_and_other_lanes_are_unchanged(
        tmp_path, monkeypatch, budget):
    reserve = semantic_drain._backlog_commit_reserve(_scfg(budget))
    backlog = _first_timeout(tmp_path / "b", monkeypatch, budget, "backlog")
    assert backlog == pytest.approx(budget - reserve)
    for lane in (None, "realtime"):
        other = _first_timeout(tmp_path / str(lane), monkeypatch, budget, lane)
        assert other == pytest.approx(min(budget, semantic_transport.LLM_TIMEOUT))


def test_a_long_result_waiting_out_a_tick_is_saved_and_not_generated_again(
        tmp_path, monkeypatch):
    """590 s of generation, then 477 s waiting for a tick to release the run
    lock (both measured 2026-10-10): the result returns at 1067 s, inside the
    1260 s job deadline, and is kept; the next pass resumes after it."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    waits = [477.0]

    @contextlib.contextmanager
    def tick_hold(ledger, lock_fd, wait_s=None, deadline=None):
        yield
        clock[0] += waits.pop(0) if waits else 0.0

    monkeypatch.setattr(writer_lock, "unlocked_transport", tick_hold)
    prompts = []

    def llm(prompt, timeout=None):
        prompts.append(prompt)
        clock[0] += 590.0 if len(prompts) == 1 else 10.0
        return _llm(prompt)

    cfg = _cfg("shadow", job_budget_seconds=1260)
    db = _seeded_two(tmp_path)        # distinct bodies: distinct prompts
    try:
        statuses = []
        for _ in range(4):                    # resume until the job is done
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            out = semantic_drain.run_due(db, cfg, {"errors": []}, clock[0] + 1290,
                                   jev_client=_FakeJev(), llm_fn=llm,
                                   lane="backlog", max_jobs=1)
            statuses += [m["status"] for m in out["job_metrics"]]
            state, attempts = db.db.execute(
                "SELECT state, attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
            if state == "done":
                break
            clock[0] += 2000.0
        assert state == "done" and attempts == 0, statuses
        assert statuses[0] == "deferred_short"   # the long result was kept
        assert prompts.count(prompts[0]) == 1    # never generated again
        targets = json.loads(db.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()[0])["targets"]
        for kind in ("semantic_summary", "semantic_candidate"):
            saved = {r[0] for r in db.db.execute(
                "SELECT message_id FROM artifacts WHERE kind=?", (kind,))}
            assert set(targets) <= saved, kind
    finally:
        db.close()


def test_no_follow_up_model_call_starts_after_the_transport_deadline(
        tmp_path, monkeypatch):
    """Past the transport deadline the next call is a free defer of the
    pass (llm_next_call), not a call that could only end in a lost lock."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def llm(prompt, timeout=None):
        calls.append(timeout)
        clock[0] += timeout - 1.0           # the first call uses its window
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow", job_budget_seconds=1260),
                               {"errors": []}, clock[0] + 1290,
                               jev_client=_FakeJev(), llm_fn=llm,
                               lane="backlog", max_jobs=1)
        assert len(calls) == 1
        assert out["job_metrics"][0]["status"] == "deferred_short"
        attempts = db.db.execute(
            "SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        assert attempts == 0
    finally:
        db.close()


def _answer_body():
    return json.dumps({"model": jev.JEV_MODEL,
                       "answers": {"q": {"type": "noul", "noul": 0.5}},
                       "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def test_a_jev_answer_arriving_after_the_cutoff_is_kept_and_no_new_send_starts(
        monkeypatch):
    """Review condition: the real client discards an answer that returns at
    or past the deadline it was given. The cutoff only gates sending — the
    answer is accepted until the job deadline — and the next request is a
    free defer once it could not finish before the cutoff."""
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jev.time, "monotonic", lambda: clock[0])
    posts = []

    def post(body, timeout):
        posts.append(timeout)
        clock[0] += 400.0         # answered, then the run lock was retaken late
        return 200, {}, _answer_body()

    client = jev.JevClient(api_key="synthetic", post_fn=post, max_attempts=1,
                           attempt_timeout=30)
    guarded = runtime.bind_jev(client, lambda stage: None, None,
                               cutoff=1000.0 + 300.0)
    question = {"q": jev.noul_question("synthetic", "t", "f")}
    out = guarded.evaluate({}, question, 1000.0 + 900.0)
    assert out["answers"]["q"]["noul"] == 0.5 and len(posts) == 1
    with pytest.raises(runtime.RuntimeBudgetShort):
        guarded.evaluate({}, question, 1000.0 + 900.0)
    assert len(posts) == 1


def test_a_jev_send_that_could_outlast_the_cutoff_is_not_started(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jev.time, "monotonic", lambda: clock[0])
    client = jev.JevClient(api_key="synthetic", max_attempts=1, attempt_timeout=30,
                           post_fn=lambda body, timeout: (200, {}, _answer_body()))
    guarded = runtime.bind_jev(client, lambda stage: None, None,
                               cutoff=1000.0 + 20.0)    # < attempt_timeout away
    with pytest.raises(runtime.RuntimeBudgetShort):
        guarded.evaluate({}, {"q": jev.noul_question("synthetic", "t", "f")},
                         1000.0 + 900.0)
    assert client.requests_made == 0


_TERMINAL = ("PASS", "NEEDS_REVIEW")


def _job(db):
    return db.db.execute(
        "SELECT state, attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()


def _audits(db, mid):
    return [json.loads(r[0])["status"] for r in db.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_audit' "
        "AND message_id=? ORDER BY artifact_id", (mid,))]


def _rerun(db, cfg, **kw):
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
    return semantic_drain.run_due(db, cfg, {"errors": []}, semantic.time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm, **kw)


def test_a_jev_cutoff_in_a_later_targets_audit_keeps_the_earlier_target(
        tmp_path, monkeypatch):
    """F07: the cutoff on target 2's claim audit commits target 1's audited
    summary instead of discarding it, and charges no attempt."""
    audited = []

    def audit_claims(client, bundle, summary, *a, **k):
        audited.append(1)
        if len(audited) == 2:
            raise runtime.RuntimeBudgetShort("jev_next_call")
        return [], True

    monkeypatch.setattr(semantic_audit, 'audit_claims', audit_claims)
    db = _seeded_two(tmp_path)
    try:
        out = _rerun(db, _cfg("shadow", loop_mode="off"), lane="backlog", max_jobs=1)
        assert out["job_metrics"][0]["status"] == "deferred_short"
        assert _job(db)["state"] != "done" and _job(db)["attempts"] == 0
        assert _audits(db, 1)[-1] in _TERMINAL
        assert db.artifacts("semantic_summary", message_id=1)
        assert not db.artifacts("semantic_summary", message_id=2)
        _rerun(db, _cfg("shadow", loop_mode="off"), lane="backlog", max_jobs=1)
        assert _job(db)["state"] == "done"
        assert len(audited) == 3        # target 1 was not audited again
    finally:
        db.close()


def test_a_jev_cutoff_in_the_re_audit_keeps_the_repaired_summary(
        tmp_path, monkeypatch):
    """F07: a repaired summary whose re-audit hits the cutoff is stored as
    PENDING and re-audited next pass — never replaced by the pre-repair
    text, and the one repair is not spent again (INV-10)."""
    audited, repairs = [], []
    real_summarize = semantic_drain.summarize

    def summarize(*args, **kwargs):
        out = real_summarize(*args, **kwargs)
        if kwargs.get("feedback"):
            repairs.append(1)
            out["limitations"] = ["合成の修復済み"]
        return out

    def audit_claims(client, bundle, summary, *a, **k):
        audited.append(1)
        if len(audited) == 1:
            return [{"code": "claim_not_supported", "claim": "synthetic"}], True
        if len(audited) == 2:
            raise runtime.RuntimeBudgetShort("jev_next_call")
        return [], True

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    monkeypatch.setattr(semantic_audit, 'audit_claims', audit_claims)
    db = _seeded(tmp_path)
    with db.db:
        row = db.db.execute(
            "SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
        payload = json.loads(row["payload"])
        payload["targets"] = [1]
        db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                      (json.dumps(payload), row["job_id"]))
    cfg = _cfg("shadow", loop_mode="off")
    try:
        out = _rerun(db, cfg, lane="backlog", max_jobs=1)
        assert out["job_metrics"][0]["status"] == "deferred_short"
        assert _job(db)["attempts"] == 0
        rows = db.artifacts("semantic_audit", message_id=1)
        assert _audits(db, 1) == ["PENDING", "PENDING"]
        first, latest = (json.loads(row["content"]) for row in rows)
        assert {item["code"] for item in first["findings"]} >= {"claim_not_supported"}
        assert {item["code"] for item in latest["findings"]} >= {"support_unevaluated"}
        assert all(json.loads(row["meta"])["repair_count"] == 1 for row in rows)
        _rerun(db, cfg, lane="backlog", max_jobs=1)
        assert _job(db)["state"] == "done"
        assert _audits(db, 1)[-1] == "PASS"
        final = json.loads(db.artifacts("semantic_summary", message_id=1)[-1]["content"])
        assert final["limitations"] == ["合成の修復済み"]
        assert repairs == [1]
        assert len(db.artifacts("semantic_repair", message_id=1)) == 1
    finally:
        db.close()


def test_a_jev_cutoff_in_the_relation_pass_commits_results_and_resumes(
        tmp_path, monkeypatch):
    """F07: the cutoff inside update_loops still commits the audited
    summaries; the job stays pending (deferred_short, no attempt) and the
    next pass finishes the relation pass."""
    real_update = semantic_drain.update_loops
    cut = [True]

    def update_loops(ledger, pid, bundle, facts, jev_client, *a, **k):
        if jev_client is not None and cut[0]:
            cut[0] = False
            raise runtime.RuntimeBudgetShort("jev_next_call")
        return real_update(ledger, pid, bundle, facts, jev_client, *a, **k)

    monkeypatch.setattr(semantic_drain, "update_loops", update_loops)
    monkeypatch.setattr(semantic_job_processor, 'update_loops', semantic_drain.update_loops)
    db = _seeded_two(tmp_path)
    try:
        out = _rerun(db, _cfg("shadow"), lane="backlog", max_jobs=1)
        assert out["job_metrics"][0]["status"] == "deferred_short"
        assert _job(db)["state"] != "done" and _job(db)["attempts"] == 0
        for mid in (1, 2):
            assert _audits(db, mid)[-1] in _TERMINAL
        _rerun(db, _cfg("shadow"), lane="backlog", max_jobs=1)
        assert _job(db)["state"] == "done"
    finally:
        db.close()


def test_a_budget_stop_after_a_repair_dispatch_keeps_the_earlier_target(
        tmp_path, monkeypatch):
    """F07: a post-dispatch budget stop of target 2's repair still commits
    target 1 and keeps target 2's spent repair reservation (INV-10)."""
    audited = []
    real_summarize = semantic_drain.summarize

    def summarize(*args, **kwargs):
        if kwargs.get("feedback"):
            raise runtime.RuntimeBudget("llm_result")
        return real_summarize(*args, **kwargs)

    def audit_claims(client, bundle, summary, *a, **k):
        audited.append(1)
        if len(audited) == 2:
            return [{"code": "claim_not_supported", "claim": "synthetic"}], True
        return [], True

    monkeypatch.setattr(semantic_drain, "summarize", summarize)
    monkeypatch.setattr(semantic_summary_stages, 'summarize', semantic_drain.summarize)
    monkeypatch.setattr(semantic_audit, 'audit_claims', audit_claims)
    db = _seeded(tmp_path)
    try:
        out = _rerun(db, _cfg("shadow", loop_mode="off"))
        assert out["job_metrics"][0]["status"] == "retry"
        assert _job(db)["state"] != "done"
        assert _audits(db, 1)[-1] in _TERMINAL
        assert len(db.artifacts("semantic_repair", message_id=2)) == 1
    finally:
        db.close()
