"""Jev-only semantic/QC work drains in its own bounded pass: several jobs per
resident turn, ahead of the local-model turn, still stopped by the Jev daily
cap and backlog reserve; QC and semantic share the backlog window fairly.

Synthetic only: temp ledger, Jev/LLM stubs, no services."""
import contextlib
import json
import os
import time

import pytest

import extract_llm
import extract_llm_resident
import semantic_drain
import semantic_due_selection
import semantic_jev
import semantic_policy
import semantic_qc
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
        {**k, "budget": deadline - time.monotonic(),
         "start_window": None if k.get("start_by") is None else k["start_by"] - time.monotonic()})
        or {"done": 0})
    extract_llm_resident._background_semantic(object())
    return seen[0]


def test_background_pass_drains_jev_only_work_and_keeps_one_local_job(monkeypatch):
    drain = _background(monkeypatch, _cfg("shadow", **JEV_ONLY))
    assert drain["max_jobs"] == extract_llm_resident._JEV_DRAIN_MAX_JOBS > 1
    assert drain["lane"] == "backlog"
    job_budget = semantic_policy.semantic_config(_cfg("shadow"))[0]["job_budget_seconds"]
    # the cap closes only the start window; a started job keeps one job's room after it
    window = extract_llm_resident._JEV_DRAIN_BUDGET_S
    assert window - 5 < drain["start_window"] <= window
    assert drain["budget"] - drain["start_window"] == pytest.approx(job_budget + 30, abs=1)
    local = _background(monkeypatch, _cfg("shadow"))
    assert local["max_jobs"] == 1 and local["start_window"] is None
    assert local["budget"] <= job_budget + 30


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


class _CappedJev(_FakeJev):
    """Honors the per-pass request_cap the drain sets, as JevClient does."""
    request_cap = None

    def evaluate(self, state, questions, deadline):
        if self.request_cap is not None and self.requests_made >= self.request_cap:
            raise semantic_jev.JevError("budget_exceeded", "daily_cap", retryable=True)
        return super().evaluate(state, questions, deadline)


def _usage(monkeypatch, value):
    monkeypatch.setattr(semantic_drain, "jev_usage_today", lambda ledger: value)


def test_mid_job_daily_cap_leaves_the_semantic_job_due_with_its_answers(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        p = _patient(db, 1)
        p.messages = [_message(10, pid=1)]
        db.save_patient(p, notify={"source": "unread"}, semantic=True)
        # 12 propositions in 3 requests; the backlog lane has 2 requests left
        cfg = _cfg("shadow", budget=10, max_questions_per_request=4, **JEV_ONLY)
        _usage(monkeypatch, 7)
        jev = _CappedJev()
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert jev.requests_made == 2
        assert out["done"] == 0 and out["budget_exhausted"] is True
        job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                            (semantic_drain.JOB_KIND,)).fetchone()
        assert tuple(job) == ("pending", 0)
        rows = db.db.execute("SELECT content,meta FROM artifacts WHERE kind=?",
                             (semantic_policy.KIND_ASSESS,)).fetchall()
        assert all(json.loads(r["meta"])["technical_status"] != "skipped" for r in rows)
        assert len(json.loads(rows[-1]["content"])["verdicts"]) == 8
        # the next quota window asks only the unanswered propositions
        _usage(monkeypatch, 0)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0")
        jev = _CappedJev()
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert out["done"] == 1 and jev.requests_made == 1
    finally:
        db.close()


def test_mid_job_daily_cap_leaves_the_qc_job_due_and_uncharged(tmp_path, monkeypatch):
    from extract_testkit import _hash, _v2_artifact
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1)])
        content = {"meds": [{"name": f"合成薬M{i}", "action": "start", "subject": "patient"}
                            for i in range(3)], "urgency": "routine"}
        _v2_artifact(db, 1, _hash(db), content=content)
        cfg = _cfg("shadow", budget=10, max_questions_per_request=1, extract_qc="annotate", **JEV_ONLY)
        _usage(monkeypatch, 7)
        jev = _CappedJev()
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert jev.requests_made == 2
        assert out["done"] == 0 and out["budget_exhausted"] is True
        job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                            (semantic_drain.QC_JOB_KIND,)).fetchone()
        assert tuple(job) == ("pending", 0)
        assert not db.db.execute("SELECT 1 FROM artifacts WHERE kind IN ('extract_qc','extract_qc_skipped')"
                                 ).fetchall()
    finally:
        db.close()


def test_a_long_job_budget_does_not_stretch_the_drain_or_block_its_jobs(threads, monkeypatch):
    cfg = _cfg("shadow", job_budget_seconds=1260, **JEV_ONLY)
    run_due = semantic_drain.run_due
    drain = _background(monkeypatch, cfg)
    assert drain["start_window"] <= extract_llm_resident._JEV_DRAIN_BUDGET_S
    scfg = semantic_policy.semantic_config(cfg)[0]
    assert semantic_drain._backlog_commit_reserve(scfg) == 0.0
    out = run_due(threads, cfg, {"errors": []},
                  time.monotonic() + extract_llm_resident._JEV_DRAIN_BUDGET_S,
                  jev_client=_FakeJev(), llm_fn=_no_local_model, lane="backlog", max_jobs=8)
    assert out["done"] == 4 and not out["floor_skipped"]


@pytest.mark.parametrize("stages,runs", [(JEV_ONLY, True), ({}, False)])
def test_local_breaker_stops_only_work_that_needs_the_local_model(threads, monkeypatch, stages, runs):
    monkeypatch.setattr(semantic_drain.llm_availability, "circuit_open_s", lambda db: 120)
    with threads.db:
        threads.db.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1)")
    out = semantic_drain.run_due(threads, _cfg("shadow", **stages), {"errors": []}, time.monotonic() + 300,
                                 jev_client=_FakeJev(), llm_fn=_no_local_model, lane="realtime", max_jobs=4)
    assert (out["done"] == 4) is runs
    assert ("circuit_open_s" in out) is not runs


def _clock(monkeypatch, start=1000.0):
    clock = [start]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    return clock


def _tick_holds_lock(monkeypatch, threads, hold_s):
    """One drain whose first Jev answer meets a tick holding run.lock for hold_s."""
    clock = _clock(monkeypatch)
    windows = []

    @contextlib.contextmanager
    def tick_holds_lock(ledger, lock_fd, wait_s=None, deadline=None):
        yield
        if not windows:
            clock[0] += hold_s
        windows.append(deadline)
        if clock[0] >= deadline:   # the bounded retake ran out
            raise writer_lock.RunLockLost(held=False, updated=False)

    monkeypatch.setattr(writer_lock, "unlocked_transport", tick_holds_lock)
    fd = os.open(os.devnull, os.O_RDONLY)
    started = clock[0]
    try:
        start_by = started + 10
        out = semantic_drain.run_due(threads, _cfg("shadow", **JEV_ONLY), {"errors": []},
                                     start_by + 45 + 30, jev_client=_HookedJev(), llm_fn=_no_local_model,
                                     lane="backlog", max_jobs=8, run_lock_fd=fd, start_by=start_by)
    finally:
        os.close(fd)
    # the retake is bounded by the job's own run deadline, not the drain's
    assert windows[0] == started + 45
    return out


def test_a_tick_holding_the_lock_past_the_start_window_keeps_the_answer(threads, monkeypatch):
    """The drain cap closes the start window only: the job in flight retakes
    the lock after a tick held it past that point and saves its answer;
    no new job starts after it."""
    out = _tick_holds_lock(monkeypatch, threads, 20)
    assert "run_lock_lost" not in out
    assert out["done"] == 1 and out["start_window_closed"] is True
    assert _pending(threads) == 3


def test_a_tick_holding_the_lock_past_the_job_deadline_keeps_the_attempt(threads, monkeypatch):
    """A retake that cannot finish inside the job's run deadline gives up
    there: nothing is written and the job keeps its attempts."""
    out = _tick_holds_lock(monkeypatch, threads, 50)
    assert out["run_lock_lost"] == "timeout" and out["done"] == 0
    rows = threads.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                              (semantic_drain.JOB_KIND,)).fetchall()
    assert {tuple(r) for r in rows} == {("pending", 0)}


class _HookedJev(_FakeJev):
    """Runs the per-attempt hooks before each send, as JevClient does."""
    supports_attempt_hooks = True
    attempt_timeout = 20.0

    def evaluate(self, state, questions, deadline, before_attempt=None, after_result=None,
                 reserve_fn=None, transport=None):
        if before_attempt is not None:
            before_attempt()
        send = super().evaluate
        out = (transport(send, state, questions, deadline) if transport is not None
               else send(state, questions, deadline))
        if after_result is not None:
            after_result()
        return out


def _one_job(tmp_path):
    db = _ledger(tmp_path)
    p = _patient(db, 1)
    p.messages = [_message(10, pid=1)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


def _job_row(db):
    return tuple(db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                               (semantic_drain.JOB_KIND,)).fetchone())


def test_jev_only_start_floor_covers_the_pre_send_cutoff(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    db = _one_job(tmp_path)
    try:
        cfg = _cfg("shadow", **JEV_ONLY)
        reserve = semantic_drain.runtime.jev_start_reserve(semantic_policy.semantic_config(cfg)[0])
        assert reserve > _HookedJev.attempt_timeout
        jev = _HookedJev()
        # 22 s left: above the QC floor, below one Jev attempt plus margin
        out = semantic_drain.run_due(db, cfg, {"errors": []}, clock[0] + 22, jev_client=jev,
                                     llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert out["floor_skipped"] == {"semantic": 1} and jev.requests_made == 0
        assert _job_row(db) == ("pending", 0)
    finally:
        db.close()


def test_a_first_request_refused_before_sending_charges_no_attempt(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    db = _one_job(tmp_path)
    reserve = semantic_drain._reserve_long_defer

    def slow_reserve(*args, **kwargs):
        clock[0] += 12             # the job passed the floor, then lost time before its first send
        return reserve(*args, **kwargs)

    monkeypatch.setattr(semantic_drain, "_reserve_long_defer", slow_reserve)
    try:
        jev = _HookedJev()
        out = semantic_drain.run_due(db, _cfg("shadow", **JEV_ONLY), {"errors": []}, clock[0] + 30,
                                     jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert jev.requests_made == 0 and out["done"] == 0
        assert [m["status"] for m in out["job_metrics"]] == ["deferred_short"]
        assert _job_row(db) == ("pending", 0)
    finally:
        db.close()


def test_a_qc_larger_than_one_window_completes_over_several_days(tmp_path, monkeypatch):
    from extract_testkit import _hash, _v2_artifact
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1)])
        content = {"meds": [{"name": f"合成薬M{i}", "action": "start", "subject": "patient"}
                            for i in range(4)], "urgency": "routine"}
        _v2_artifact(db, 1, _hash(db), content=content)
        cfg = _cfg("shadow", budget=10, max_questions_per_request=1, extract_qc="annotate", **JEV_ONLY)
        _usage(monkeypatch, 7)      # two backlog requests per quota window
        asked = []
        for day in range(3):
            jev = _CappedJev()
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0")
            out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                         jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
            asked += [q for call in jev.calls for q in call]
            qc = db.db.execute("SELECT content FROM artifacts WHERE kind='extract_qc'").fetchone()
            if day < 2:
                assert qc is None and out["budget_exhausted"] is True
                job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                                    (semantic_drain.QC_JOB_KIND,)).fetchone()
                assert tuple(job) == ("pending", 0)
        questions, _, _ = semantic_qc._qc_questions(content)
        # every question was asked exactly once across the three windows
        assert sorted(asked) == sorted(questions) and len(questions) == 5
        assert json.loads(qc[0])["coverage"]["checked"] == 5
        assert not db.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (semantic_qc.QC_WAIT_KIND,)).fetchall()
    finally:
        db.close()


@pytest.mark.parametrize("sends", [False, True])
def test_realtime_needs_jev_start_room_only_for_a_job_that_will_send(tmp_path, monkeypatch, sends):
    """22 s left: a realtime job that Jev is switched off for sends nothing
    and still completes; one that would send waits for start room."""
    clock = _clock(monkeypatch)
    db = _one_job(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1)")
        jev = _HookedJev() if sends else None
        out = semantic_drain.run_due(db, _cfg("shadow", budget=50 if sends else 0, **JEV_ONLY),
                                     {"errors": []}, clock[0] + 22, jev_client=jev,
                                     llm_fn=_no_local_model, lane="realtime", max_jobs=4)
        if sends:
            assert out["done"] == 0 and out["floor_skipped"] == {"semantic": 1}
            assert jev.requests_made == 0 and _job_row(db) == ("pending", 0)
        else:
            assert out["done"] == 1 and out["jev_skipped"] == {"disabled": 1}
    finally:
        db.close()


def _qc_source(db, items=4):
    from extract_testkit import _hash, _v2_artifact
    db.save_messages([_message(1)])
    content = {"meds": [{"name": f"合成薬M{i}", "action": "start", "subject": "patient"}
                        for i in range(items - 1)], "urgency": "routine"}
    _v2_artifact(db, 1, _hash(db), content=content)


def _qc_state(db):
    job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind=?",
                        (semantic_drain.QC_JOB_KIND,)).fetchone()
    kinds = {r[0] for r in db.db.execute(
        "SELECT kind FROM artifacts WHERE kind IN ('extract_qc','extract_qc_skipped',?)",
        (semantic_qc.QC_WAIT_KIND,))}
    return tuple(job), kinds


@pytest.mark.parametrize("usage,requests", [(9, 0), (7, 2)], ids=["at_reserve", "mid_job"])
def test_backlog_qc_waits_for_quota_with_local_model_stages_on(tmp_path, monkeypatch, usage, requests):
    """QC's whole result is Jev's whatever the semantic stages: in the backlog
    lane it waits for the next quota window instead of completing as skipped."""
    db = _ledger(tmp_path)
    try:
        _qc_source(db)
        cfg = _cfg("shadow", budget=10, max_questions_per_request=1, extract_qc="annotate")
        assert semantic_policy.needs_local_model(semantic_policy.semantic_config(cfg)[0])
        _usage(monkeypatch, usage)
        jev = _CappedJev()
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     jev_client=jev, llm_fn=_no_local_model, lane="backlog", max_jobs=8)
        assert jev.requests_made == requests and out["done"] == 0
        job, kinds = _qc_state(db)
        assert job == ("pending", 0)
        assert kinds == ({semantic_qc.QC_WAIT_KIND} if requests else set())
    finally:
        db.close()
