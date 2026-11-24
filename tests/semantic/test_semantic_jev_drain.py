"""Jev-only semantic/QC work drains in its own bounded pass: several jobs per
resident turn, ahead of the local-model turn, still stopped by the Jev daily
cap and backlog reserve; QC and semantic share the backlog window fairly.

Synthetic only: temp ledger, Jev/LLM stubs, no services."""
import os
import time

import pytest

import extract_llm
import extract_llm_resident
import semantic_drain
import semantic_due_selection
import semantic_policy
import writer_lock
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _ledger, _message, _patient

JEV_ONLY = {"summary_mode": "off", "loop_mode": "off"}


def _no_local_model(prompt, *args, **kwargs):
    raise AssertionError("a Jev-only job must not call the local model")


@pytest.fixture
def threads(tmp_path, monkeypatch):
    """Four synthetic patients, one pending semantic job each."""
    monkeypatch.setattr(semantic_due_selection, "_BACKLOG_TURN", {"since_qc": 0})
    monkeypatch.setattr(semantic_drain, "_BACKLOG_TURN", semantic_due_selection._BACKLOG_TURN)
    db = _ledger(tmp_path)
    for pid in range(1, 5):
        p = _patient(db, pid)
        p.messages = [_message(pid * 10, pid=pid)]
        db.save_patient(p, notify={"source": "unread"}, semantic=True)
    try:
        yield db
    finally:
        db.close()


def _pending(db):
    return db.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind=? AND state='pending'",
                         (semantic_drain.JOB_KIND,)).fetchone()[0]


def test_predicate_reads_the_resolved_semantic_config():
    scfg = semantic_policy.semantic_config(_cfg("shadow", **JEV_ONLY))[0]
    assert not semantic_policy.needs_local_model(scfg)
    for stage in ("summary_mode", "loop_mode"):
        scfg = semantic_policy.semantic_config(_cfg("shadow", **{**JEV_ONLY, stage: "shadow"}))[0]
        assert semantic_policy.needs_local_model(scfg)
    assert extract_llm_resident._jev_only_semantic(_cfg("shadow", **JEV_ONLY))
    assert not extract_llm_resident._jev_only_semantic(_cfg("shadow"))
    assert not extract_llm_resident._jev_only_semantic(_cfg("off", **JEV_ONLY))


def test_jev_only_due_set_drains_several_jobs_in_one_pass(threads):
    jev = _FakeJev()
    out = semantic_drain.run_due(threads, _cfg("shadow", **JEV_ONLY), {"errors": []},
                                 time.monotonic() + 300, jev_client=jev, llm_fn=_no_local_model,
                                 lane="backlog", max_jobs=extract_llm_resident._JEV_DRAIN_MAX_JOBS)
    assert out["done"] == 4 and _pending(threads) == 0
    assert jev.requests_made >= 4


@pytest.mark.parametrize("usage,stops", [(8, False), (9, True), (10, True)])
def test_daily_cap_and_backlog_reserve_stop_the_jev_only_drain(threads, monkeypatch, usage, stops):
    # budget 10 leaves the backlog lane 9 requests; the 10% reserve stays for realtime
    monkeypatch.setattr(semantic_drain, "jev_usage_today", lambda ledger: usage)
    jev = _FakeJev()
    out = semantic_drain.run_due(threads, _cfg("shadow", budget=10, **JEV_ONLY), {"errors": []},
                                 time.monotonic() + 300, jev_client=jev, llm_fn=_no_local_model,
                                 lane="backlog", max_jobs=8)
    if stops:
        # nothing completes as a skipped assessment: the jobs wait for quota
        assert out["done"] == 0 and out["budget_exhausted"] is True
        assert out["defer_reasons"] == {"backlog_reserved": 1}
        assert jev.requests_made == 0 and _pending(threads) == 4
        rows = threads.db.execute("SELECT attempts FROM fetch_jobs WHERE kind=?",
                                  (semantic_drain.JOB_KIND,)).fetchall()
        assert {r[0] for r in rows} == {0}
    else:
        assert out["done"] >= 1 and jev.requests_made >= 1


def _background(monkeypatch, cfg):
    """_background_semantic with a stub lock and drain; returns the drain's kwargs."""
    seen = []
    fd = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(extract_llm_resident, "load_config", lambda: cfg)
    monkeypatch.setattr(writer_lock, "wait_run_lock", lambda *a, **k: fd)
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 7)
    monkeypatch.setattr(semantic_drain, "run_due", lambda ledger, cfg, result, deadline, **k: seen.append(
        {**k, "budget": deadline - time.monotonic()}) or {"done": 0})
    extract_llm_resident._background_semantic(object())
    return seen[0]


def test_background_pass_drains_jev_only_work_and_keeps_one_local_job(monkeypatch):
    drain = _background(monkeypatch, _cfg("shadow", **JEV_ONLY))
    assert drain["max_jobs"] == extract_llm_resident._JEV_DRAIN_MAX_JOBS > 1
    assert drain["lane"] == "backlog"
    assert extract_llm_resident._JEV_DRAIN_BUDGET_S - 5 < drain["budget"] <= extract_llm_resident._JEV_DRAIN_BUDGET_S
    local = _background(monkeypatch, _cfg("shadow"))
    job_budget = semantic_policy.semantic_config(_cfg("shadow"))[0]["job_budget_seconds"]
    assert local["max_jobs"] == 1 and local["budget"] <= job_budget + 30


def _resident_loop(monkeypatch, cfg, events, *, carry=False):
    monkeypatch.setattr(extract_llm_resident, "load_config", lambda: cfg)
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--semantic", "--stop-after", "600"])
    monkeypatch.setattr(extract_llm_resident, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm_resident.time, "sleep", lambda *a: None)
    monkeypatch.setattr(extract_llm_resident.cloud_backlog, "CloudBacklogPool",
                        lambda *a, **k: type("Pool", (), {"sync": lambda *a, **k: False,
                                                          "close": lambda self: None})())

    def run_pending(*a, **k):
        events.append("extract")
        if events.count("extract") > 3:
            raise KeyboardInterrupt("synthetic stop")
        return {"done": 1, "failed": 0, "left": 1, "selected": 1,
                **({"carry": ["synthetic-carry"]} if carry else {})}

    def background(*a):
        events.append("semantic")
        if events.count("semantic") > 1:
            raise KeyboardInterrupt("synthetic stop")
        return {"done": 0}

    monkeypatch.setattr(extract_llm_resident, "run_pending", run_pending)
    monkeypatch.setattr(extract_llm_resident, "_background_semantic", background)
    with pytest.raises(KeyboardInterrupt):
        extract_llm.main()


@pytest.mark.parametrize("carry", [False, True])
def test_resident_runs_the_jev_only_drain_ahead_of_extraction_and_carry(monkeypatch, carry):
    events = []
    _resident_loop(monkeypatch, _cfg("shadow", **JEV_ONLY), events, carry=carry)
    assert events == ["semantic", "extract", "semantic"]


@pytest.mark.parametrize("carry,expected", [
    (False, ["extract", "semantic", "extract", "semantic"]),
    # a carried extraction result still holds back local-model semantic work
    (True, ["extract", "extract", "extract", "extract"])])
def test_local_model_semantic_keeps_the_trailing_single_job_turn(monkeypatch, carry, expected):
    events = []
    _resident_loop(monkeypatch, _cfg("shadow"), events, carry=carry)
    assert events == expected


def _jobs(led, kind, count, start):
    for mid in range(start, start + count):
        led.job_add(kind, 1, message_id=mid, payload={VERSION_KEY: CURRENT_VERSION})


@pytest.mark.parametrize("since_qc,expected", [
    (0, "SSSQSSSQ"),
    (3, "QSSSQSSS")])
def test_backlog_window_gives_qc_a_bounded_fair_share(tmp_path, monkeypatch, since_qc, expected):
    monkeypatch.setattr(semantic_due_selection, "_BACKLOG_TURN", {"since_qc": since_qc})
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        _jobs(led, semantic_drain.QC_JOB_KIND, 10, 100)
        _jobs(led, semantic_drain.JOB_KIND, 10, 200)
        due, *_ = semantic_drain._select_due(
            led, "backlog", (semantic_drain.JOB_KIND, semantic_drain.QC_JOB_KIND), True, 8)
        kinds = "".join("Q" if r["kind"] == semantic_drain.QC_JOB_KIND else "S" for r in due[:8])
        assert kinds == expected
        # each kind keeps its own oldest-first order
        for kind in (semantic_drain.JOB_KIND, semantic_drain.QC_JOB_KIND):
            ids = [r["job_id"] for r in due if r["kind"] == kind]
            assert ids == sorted(ids)
    finally:
        led.close()


def test_fair_share_leaves_spare_capacity_to_the_only_due_kind(monkeypatch):
    monkeypatch.setattr(semantic_due_selection, "_BACKLOG_TURN", {"since_qc": 0})
    assert semantic_due_selection._fair_backlog_order([], ["q1", "q2"]) == ["q1", "q2"]
    assert semantic_due_selection._fair_backlog_order(["s1", "s2"], []) == ["s1", "s2"]
    assert semantic_due_selection._fair_backlog_order(["s1", "s2", "s3", "s4", "s5"], ["q1"]) == \
        ["s1", "s2", "s3", "q1", "s4", "s5"]


def test_served_jobs_advance_the_fair_share(monkeypatch):
    turn = {"since_qc": 0}
    monkeypatch.setattr(semantic_due_selection, "_BACKLOG_TURN", turn)
    for _ in range(10):
        semantic_due_selection._note_backlog_turn(semantic_drain.JOB_KIND)
    assert turn["since_qc"] == semantic_due_selection._QC_FAIR_EVERY - 1   # bounded
    semantic_due_selection._note_backlog_turn(semantic_drain.QC_JOB_KIND)
    assert turn["since_qc"] == 0
