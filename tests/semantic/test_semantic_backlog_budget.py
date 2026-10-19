"""Backlog semantic passes must outwait the tick's run lock inside the
deadline run_due was given, keep the resident up, keep their wire slot
and leave a diagnosable run record (2026-10-09: run_lock_lost timeouts
left jobs untouched and restarted the resident while nothing completed).

Synthetic only: temp ledger, stub model, no services."""
import writer_lock
import semantic_policy
import semantic_runtime
import semantic_transport
import os
import time
from contextlib import contextmanager

import pytest

import extract_llm
import local_llm
import run_check
import semantic
import semantic_drain
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def test_backlog_reacquire_outwaits_a_whole_tick():
    """The tick may wait LOCK_WAIT_S for the lock and then hold it for
    RUN_DEADLINE_S; a shorter reacquire bound discards finished work."""
    assert semantic_drain.RUN_LOCK_REACQUIRE_S >= (
        run_check.RUN_DEADLINE_S + run_check.LOCK_WAIT_S)
    assert semantic_drain.RUN_LOCK_REACQUIRE_S > writer_lock.RUN_LOCK_REACQUIRE_S


@pytest.mark.parametrize("lane,expected", [("backlog", "tick"), ("realtime", None)])
def test_only_the_backlog_lane_passes_the_long_reacquire_bound(
        tmp_path, monkeypatch, lane, expected):
    if expected == "tick":
        expected = semantic_drain.RUN_LOCK_REACQUIRE_S
    calls = []

    @contextmanager
    def recorder(ledger, lock_fd, wait_s=None, deadline=None):
        calls.append((wait_s, deadline))
        yield

    monkeypatch.setattr(writer_lock, "unlocked_transport", recorder)
    db = _seeded(tmp_path)
    deadline = time.monotonic() + 300
    try:
        if lane == "realtime":
            # arrival-seeded rows are what the realtime lane serves
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1) "
                    "WHERE kind='semantic'")
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []}, deadline,
                               jev_client=_FakeJev(), llm_fn=_llm,
                               lane=lane, max_jobs=1)
        assert out["done"] == 1
        # every transport waits at most until the deadline run_due was
        # given (the backlog pass budget / the tick deadline); the
        # per-call runtime guard inside the job stays shorter
        assert calls and set(calls) == {(expected, deadline)}
    finally:
        db.close()


@pytest.fixture
def held_lock(tmp_path, monkeypatch):
    """Our fd plus a second holder, as when the tick owns the run lock."""
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 1)
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(writer_lock, "RUN_LOCK_POLL_S", 0.0)
    path = str(tmp_path / "run.lock")
    ours = writer_lock.acquire_run_lock(path)
    yield ours, path
    os.close(ours)


@pytest.fixture
def connection():
    import sqlite3
    db = sqlite3.connect(":memory:")
    try:
        yield db
    finally:
        db.close()


def test_reacquire_never_waits_past_the_caller_deadline(held_lock, monkeypatch, connection):
    ours, path = held_lock
    clock = [1000.0]
    monkeypatch.setattr(writer_lock.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(writer_lock.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 1))
    other = []
    with pytest.raises(writer_lock.RunLockLost) as lost:
        with writer_lock.unlocked_transport(connection, ours, wait_s=600, deadline=1005.0):
            other.append(writer_lock.acquire_run_lock(path))   # the tick takes it
    os.close(other[0])
    assert lost.value.held is False and lost.value.updated is False
    assert clock[0] == pytest.approx(1005.0, abs=1.0)   # the deadline, not wait_s


def test_updater_during_an_expiring_wait_is_reported(held_lock, monkeypatch, connection):
    ours, path = held_lock
    monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0.0)
    other = []
    stamps = iter([1, 2, 2])
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: next(stamps))
    with pytest.raises(writer_lock.RunLockLost) as lost:
        with writer_lock.unlocked_transport(connection, ours):
            other.append(writer_lock.acquire_run_lock(path))
    os.close(other[0])
    assert lost.value.held is False and lost.value.updated is True
    assert str(lost.value) == "run_lock_timeout_updated"


def _resident(monkeypatch, calls, background, argv_extra=()):
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm.sys, "argv",
                        ["extract_llm", "--all", "--semantic", "--stop-after", "600",
                         *argv_extra])
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm.time, "sleep", lambda *a: None)
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **k: calls.append(1) or
                        {"done": 1, "failed": 0, "left": 1, "selected": 1})
    monkeypatch.setattr(extract_llm, "_background_semantic", background)


@pytest.mark.parametrize("lost,passes", [("timeout", 2), ("updated", 1), ("held", 1)])
def test_resident_exits_only_when_an_updater_ran(monkeypatch, lost, passes):
    """A reacquire timeout under unchanged code leaves the job pending —
    the resident carries on; an updater (held/updated) still forces the
    respawn exit."""
    calls = []
    results = [{"run_lock_lost": lost, "errors": []},
               KeyboardInterrupt("synthetic stop after the second pass")]

    def background(*a):
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    _resident(monkeypatch, calls, background)
    if passes == 2:
        with pytest.raises(KeyboardInterrupt):
            extract_llm.main()
    else:
        assert extract_llm.main() == 0
    assert calls == [1] * passes


@pytest.mark.parametrize("lane", [2, 3])
def test_second_worker_runs_semantic_on_its_own_slot(monkeypatch, tmp_path, lane):
    """main() pins every resident to BACKGROUND_SLOT for extraction; the
    semantic pass must follow the validated --lane (wire id lane - 1)
    instead of queueing on lane 2 behind the other worker."""
    import semantic_drain as drain
    seen = []
    lock = str(tmp_path / "run.lock")
    monkeypatch.setattr(extract_llm, "acquire_run_lock",
                        lambda *a, **k: writer_lock.acquire_run_lock(lock))

    def run_due(*a, **k):
        seen.append(local_llm.request_slot())
        raise KeyboardInterrupt("synthetic stop inside the semantic pass")

    monkeypatch.setattr(drain, "run_due", run_due)
    calls = []
    _resident(monkeypatch, calls, extract_llm._background_semantic,
              argv_extra=("--lane", str(lane)))
    with pytest.raises(KeyboardInterrupt):
        extract_llm.main()
    assert seen == [local_llm.LANES[lane]]
    assert "MCS_LLM_SLOT" not in os.environ          # main() restored the process


def test_direct_background_pass_keeps_the_default_lane(monkeypatch, tmp_path):
    import semantic_drain as drain
    seen = []
    lock = str(tmp_path / "run.lock")
    monkeypatch.setattr(extract_llm, "acquire_run_lock",
                        lambda *a, **k: writer_lock.acquire_run_lock(lock))
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.setattr(drain, "run_due", lambda *a, **k: seen.append(
        local_llm.request_slot()) or {"done": 0})
    assert extract_llm._background_semantic(object()) == {"done": 0, "errors": []}
    assert seen == [local_llm.BACKGROUND_SLOT]
    assert "MCS_LLM_SLOT" not in os.environ


def test_run_record_names_silent_skips_and_the_preamble(tmp_path):
    db = _seeded(tmp_path)
    try:
        # a deadline already inside the first-call floor: the semantic row
        # is skipped without a transition — now counted, not invisible
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 5, jev_client=_FakeJev(),
                               llm_fn=_llm, lane="backlog", max_jobs=1)
        assert out["done"] == 0 and out["floor_skipped"] == {"semantic": 1}
        assert out["lock_skipped"] == {} and out["preamble_s"] >= 0
        row = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert tuple(row) == ("pending", 0)
    finally:
        db.close()


@pytest.mark.parametrize("made_call,expected", [(True, "length_stop"), (False, None)])
def test_llm_error_is_reported_only_for_passes_that_called_the_model(
        tmp_path, monkeypatch, made_call, expected):
    """The failure code is a module-level latest value: a job that served
    cached stages (or a QC row) must not inherit the previous job's."""
    def process(ledger, scfg, job, jev_client, llm_fn, deadline, **kw):
        if made_call:
            llm_fn("完全合成の呼出し")
        return "retry"

    monkeypatch.setattr(semantic_drain, '_process_job', process)
    monkeypatch.setattr(semantic_transport, '_LLM_FAILURE', "length_stop")   # left by an earlier job
    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 300, jev_client=_FakeJev(),
                               llm_fn=lambda prompt: None, lane="backlog", max_jobs=1)
        assert [m["llm_error"] for m in out["job_metrics"]] == [expected]
    finally:
        db.close()


@pytest.mark.parametrize("response,code", [
    ({"text": "{}", "finish_reason": "length", "status": 200}, "length_stop"),
    ({"text": "", "finish_reason": "stop", "status": 200}, "empty"),
    (None, "transport"),
])
def test_llm_chat_records_its_failure_code(monkeypatch, tmp_path, response, code):
    monkeypatch.setattr(semantic_transport, 'llm_conf', lambda: ("http://127.0.0.1:8080/v1/chat/completions", "m"))
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a, **k: "plain")
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / "long.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: response)
    assert semantic_transport.llm_chat("完全合成の本文", timeout=30) is None
    assert semantic_transport.last_llm_failure() == code
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: {
        "text": "{\"ok\":1}", "finish_reason": "stop", "status": 200})
    # a different prompt: the length mark above binds to the first one
    assert semantic_transport.llm_chat("完全合成の別の本文", timeout=30) == "{\"ok\":1}"
    assert semantic_transport.last_llm_failure() is None


@pytest.mark.parametrize("status,code", [(503, "unavailable"), (500, "http_status")])
def test_a_loading_server_defers_without_an_attempt(monkeypatch, tmp_path, status, code):
    """2026-10-10 04:03: the daily llama-server restart answered 503
    (Loading model) and the pass was charged an attempt. A 503 did no
    work, like a refused connection; other HTTP errors stay failures."""
    monkeypatch.setattr(semantic_transport, 'llm_conf', lambda: ("http://127.0.0.1:8080/v1/chat/completions", "m"))
    monkeypatch.setattr(semantic_transport, '_probe_format', lambda *a, **k: "plain")
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / "long.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: {
        "text": None, "finish_reason": None, "usage": None, "status": status})
    if status == 503:
        with pytest.raises(semantic_runtime.LLMNotSent):
            semantic_transport.llm_chat("完全合成の本文", timeout=30)
    else:
        assert semantic_transport.llm_chat("完全合成の本文", timeout=30) is None
    assert semantic_transport.last_llm_failure() == code


@pytest.mark.parametrize("lane,lifted", [("backlog", True), ("realtime", False),
                                         (None, False)])
def test_only_the_backlog_lane_lifts_the_per_call_cap(tmp_path, monkeypatch, lane, lifted):
    """2026-10-10: a long-output call needing more than 600 s burned an
    attempt every pass. The backlog lane bounds one call by the job deadline
    (at most job_budget_seconds); realtime, the tick and direct runs keep
    semantic.LLM_TIMEOUT."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    timeouts = []

    def llm(prompt, timeout=None):
        timeouts.append(timeout)
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        if lane == "realtime":
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1) "
                    "WHERE kind='semantic'")
        semantic_drain.run_due(db, _cfg("shadow", job_budget_seconds=900), {"errors": []},
                         clock[0] + 930, jev_client=_FakeJev(), llm_fn=llm,
                         lane=lane, max_jobs=1)
        assert timeouts and timeouts[0] <= 900
        assert (timeouts[0] > semantic_transport.LLM_TIMEOUT) is lifted
    finally:
        db.close()


# artifacts built from a local-model result (facts, summary, publication)
_RESULT_KINDS = ("semantic_facts", "semantic_facts_v2", "semantic_summary",
                 "semantic_candidate", "canonical_projection", "v4_stage",
                 "loop_candidate", "notify_plan")


def _result_rows(db):
    marks = ",".join("?" * len(_RESULT_KINDS))
    return db.db.execute(
        f"SELECT COUNT(*) FROM artifacts WHERE kind IN ({marks})",
        _RESULT_KINDS).fetchone()[0]


@pytest.mark.parametrize("late", [False, True])
def test_a_lifted_call_ending_past_the_job_deadline_is_not_saved(tmp_path, monkeypatch, late):
    """Review condition: a result returning after the 900 s job deadline but
    inside run_due's 930 s, with the run lock retaken, is still refused by
    the post-call guard — no artifact built from it — and charges one
    bounded attempt; lifting the cap never extends the job deadline. The
    in-time run is the control that the same result is otherwise saved.
    (The call's own timeout ends a commit reserve earlier; "late" runs past
    both.)"""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    cfg = _cfg("shadow", job_budget_seconds=900)
    reserve = semantic_drain._backlog_commit_reserve(
        semantic_policy.semantic_config(cfg)[0])

    def llm(prompt, timeout=None):
        clock[0] += timeout + reserve + 5 if late else 10
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        before = _result_rows(db)
        out = semantic_drain.run_due(db, cfg, {"errors": []}, clock[0] + 930,
                               jev_client=_FakeJev(), llm_fn=llm,
                               lane="backlog", max_jobs=1)
        row = db.db.execute(
            "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        if late:
            assert tuple(row) == ("pending", 1)
            assert out["job_metrics"][0]["status"] == "retry"
            assert _result_rows(db) == before
        else:
            assert out["job_metrics"][0]["status"] == "done"
            assert _result_rows(db) > before
    finally:
        db.close()


def test_a_lifted_call_whose_lock_is_not_retaken_keeps_its_attempt(tmp_path, monkeypatch):
    """Review condition: when the run lock cannot be retaken after a long
    call, nothing is written and the attempt count is unchanged (the GPU
    time and any Jev calls already made are still spent)."""
    @contextmanager
    def lost(ledger, lock_fd, wait_s=None, deadline=None):
        yield
        raise writer_lock.RunLockLost(held=False)

    monkeypatch.setattr(writer_lock, "unlocked_transport", lost)
    db = _seeded(tmp_path)
    try:
        before = _result_rows(db)
        out = semantic_drain.run_due(db, _cfg("shadow", job_budget_seconds=900),
                               {"errors": []}, time.monotonic() + 930,
                               jev_client=_FakeJev(), llm_fn=_llm,
                               lane="backlog", max_jobs=1)
        assert out["run_lock_lost"] == "timeout"
        row = db.db.execute(
            "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert tuple(row) == ("pending", 0)
        assert _result_rows(db) == before
    finally:
        db.close()


@pytest.mark.parametrize("llm,expected", [
    # the summary model answered, but not with a usable claims document
    (lambda prompt, timeout=None: "要約できません" if "要約器" in prompt
     else _llm(prompt), "summary_model"),
    (_llm, None),
])
def test_run_record_names_why_a_pass_will_retry(tmp_path, llm, expected):
    """2026-10-10: a ~600 s pass ended "retry" with neither llm_error nor
    jev_error, so the run record could not say why the job kept burning
    attempts. job_metrics.retry_reason names the first cause (a code,
    never content); a pass that completed carries none."""
    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 300, jev_client=_FakeJev(),
                               llm_fn=llm, lane="backlog", max_jobs=1)
        metrics = out["job_metrics"][0]
        assert metrics["retry_reason"] == expected
        assert metrics["status"] == ("retry" if expected else "done")
    finally:
        db.close()


def test_retry_reason_is_per_job_and_keeps_the_first_cause(tmp_path, monkeypatch):
    """A later job that stops without a recorded cause must not inherit
    the earlier job's code; the first cause of each job wins."""
    import json
    db = _seeded(tmp_path)
    payload = json.loads(db.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()[0])
    db.job_add("semantic", 1, 2, payload=payload)
    db.db.commit()
    passes = iter([("first", "second", "retry"), (None, None, "deferred")])

    def process(*a, **k):
        first, second, status = next(passes)
        if first:
            semantic_drain._why(k["diagnostics"], first)
            semantic_drain._why(k["diagnostics"], second)
        return status

    monkeypatch.setattr(semantic_drain, '_process_job', process)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 300, jev_client=_FakeJev(),
                               llm_fn=_llm, lane="backlog", max_jobs=2)
        assert [m["retry_reason"] for m in out["job_metrics"]] == ["first", None]
    finally:
        db.close()


def _admitted_per_pass(monkeypatch, passes, base=None):
    """Run the resident over `passes` (each advancing the clock by just over
    half SEMANTIC_FIRST_S) and return the admission set of every extraction."""
    clock = [1000.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])
    admitted = []
    passes = [*passes, KeyboardInterrupt("synthetic stop")]

    def background(*a):
        clock[0] += extract_llm.SEMANTIC_FIRST_S / 2 + 1
        result = passes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return {"errors": [], **result}

    class DB:
        def execute(self, sql, params=()):
            assert "notified_at" in sql
            return [(7,)]

    _resident(monkeypatch, [], background, ("--stop-after", "100000"))
    monkeypatch.setattr(extract_llm, "Ledger",
                        lambda *a: type("L", (), {"db": DB(), "close": lambda self: None})())
    monkeypatch.setattr(extract_llm, "legacy_admissions",
                        lambda *a: None if base is None else set(base))
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **k: admitted.append(
        k.get("admitted_ids")) or {"done": 1, "failed": 0, "left": 1, "selected": 1})
    with pytest.raises(KeyboardInterrupt):
        extract_llm.main()
    return admitted


@pytest.mark.parametrize("base", [None, {7, 9}, {9}, set()])
def test_semantic_work_goes_first_while_semantic_jobs_run(monkeypatch, base):
    """Owner decision 2026-10-10: semantic backlog before re-extraction.
    After a pass that ran a semantic job, the resident extracts only
    recently notified arrivals (the tick's realtime set); once no semantic
    job has run for SEMANTIC_FIRST_S, re-extraction resumes. A restricted
    admission set (e.g. a canonical cohort) is only narrowed, never widened."""
    import run_check
    assert extract_llm._ARRIVAL_CARRY_S == run_check.REALTIME_CARRY_S
    admitted = _admitted_per_pass(monkeypatch, [
        {"job_metrics": [{"kind": "semantic", "status": "done"}]},
        {"job_metrics": [{"kind": "extract_qc", "status": "done"}]},  # QC turn
        {"job_metrics": []}], base)                                   # nothing due
    # the base admission, then only its recent arrivals (semantic ran; still
    # inside the window after the QC turn), then the base again once expired
    narrowed = {7} if base is None else base & {7}
    assert admitted == [base, narrowed, narrowed, base]


def test_semantic_passes_that_only_wait_do_not_hold_re_extraction(monkeypatch):
    """Review condition: a semantic job that keeps deferring without work
    (a resource wait, an LLM that was never sent) must not extend the
    window forever — re-extraction resumes SEMANTIC_FIRST_S after the last
    pass that did semantic work."""
    admitted = _admitted_per_pass(monkeypatch, [
        {"job_metrics": [{"kind": "semantic", "status": "retry"}]},
        {"job_metrics": [{"kind": "semantic", "status": "deferred"}]},
        {"job_metrics": [{"kind": "semantic", "status": "deferred_backoff"}]}])
    assert admitted == [None, {7}, {7}, None]


def test_a_semantic_pass_does_not_idle_on_an_empty_extraction(monkeypatch):
    """While semantic goes first the restricted extraction usually selects
    nothing; a semantic pass that ended without progress must not then
    trigger the 120 s queue-drained poll."""
    sleeps = []
    passes = [{"job_metrics": [{"kind": "semantic", "status": "retry"}]},
              KeyboardInterrupt("stop")]

    def background(*a):
        result = passes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return {"errors": [], **result}

    _resident(monkeypatch, [], background, ("--stop-after", "100000"))
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: type("L", (), {
        "db": type("DB", (), {"execute": lambda self, *a: []})(),
        "close": lambda self: None})())
    monkeypatch.setattr(extract_llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **k: {
        "done": 0, "failed": 0, "left": 0, "selected": 0})
    with pytest.raises(KeyboardInterrupt):
        extract_llm.main()
    assert sleeps and max(sleeps) <= 1
