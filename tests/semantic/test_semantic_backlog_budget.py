"""Backlog semantic passes must outwait the tick's run lock inside the
deadline run_due was given, keep the resident up, keep their wire slot
and leave a diagnosable run record (2026-10-09: run_lock_lost timeouts
left jobs untouched and restarted the resident while nothing completed).

Synthetic only: temp ledger, stub model, no services."""
import os
import time
from contextlib import contextmanager

import pytest

import extract_llm
import local_llm
import mcs_util
import run_check
import semantic
import semantic_drain
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def test_backlog_reacquire_outwaits_a_whole_tick():
    """The tick may wait LOCK_WAIT_S for the lock and then hold it for
    RUN_DEADLINE_S; a shorter reacquire bound discards finished work."""
    assert semantic_drain.RUN_LOCK_REACQUIRE_S >= (
        run_check.RUN_DEADLINE_S + run_check.LOCK_WAIT_S)
    assert semantic_drain.RUN_LOCK_REACQUIRE_S > mcs_util.RUN_LOCK_REACQUIRE_S


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

    monkeypatch.setattr(mcs_util, "unlocked_transport", recorder)
    db = _seeded(tmp_path)
    deadline = time.monotonic() + 300
    try:
        if lane == "realtime":
            # arrival-seeded rows are what the realtime lane serves
            with db.db:
                db.db.execute(
                    "UPDATE fetch_jobs SET payload=json_set(payload,'$.eligible',1) "
                    "WHERE kind='semantic'")
        out = semantic.run_due(db, _cfg("shadow"), {"errors": []}, deadline,
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
    monkeypatch.setattr(mcs_util, "code_stamp", lambda: 1)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(mcs_util, "RUN_LOCK_POLL_S", 0.0)
    path = str(tmp_path / "run.lock")
    ours = mcs_util.acquire_run_lock(path)
    yield ours, path
    os.close(ours)


class _Ledger:
    class db:
        in_transaction = False


def test_reacquire_never_waits_past_the_caller_deadline(held_lock, monkeypatch):
    ours, path = held_lock
    clock = [1000.0]
    monkeypatch.setattr(mcs_util.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mcs_util.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 1))
    other = []
    with pytest.raises(mcs_util.RunLockLost) as lost:
        with mcs_util.unlocked_transport(_Ledger(), ours, wait_s=600, deadline=1005.0):
            other.append(mcs_util.acquire_run_lock(path))   # the tick takes it
    os.close(other[0])
    assert lost.value.held is False and lost.value.updated is False
    assert clock[0] == pytest.approx(1005.0, abs=1.0)   # the deadline, not wait_s


def test_updater_during_an_expiring_wait_is_reported(held_lock, monkeypatch):
    ours, path = held_lock
    monkeypatch.setattr(mcs_util, "RUN_LOCK_REACQUIRE_S", 0.0)
    other = []
    stamps = iter([1, 2, 2])
    monkeypatch.setattr(mcs_util, "code_stamp", lambda: next(stamps))
    with pytest.raises(mcs_util.RunLockLost) as lost:
        with mcs_util.unlocked_transport(_Ledger(), ours):
            other.append(mcs_util.acquire_run_lock(path))
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


@pytest.mark.parametrize("slot", [0, 2])
def test_second_worker_runs_semantic_on_its_own_slot(monkeypatch, tmp_path, slot):
    """main() pins every resident to BACKGROUND_SLOT for extraction; the
    semantic pass must follow the validated --slot lane instead of
    queueing on slot 0 behind the other worker."""
    import semantic_drain as drain
    seen = []
    lock = str(tmp_path / "run.lock")
    monkeypatch.setattr(extract_llm, "acquire_run_lock",
                        lambda *a, **k: mcs_util.acquire_run_lock(lock))

    def run_due(*a, **k):
        seen.append(local_llm.request_slot())
        raise KeyboardInterrupt("synthetic stop inside the semantic pass")

    monkeypatch.setattr(drain, "run_due", run_due)
    calls = []
    _resident(monkeypatch, calls, extract_llm._background_semantic,
              argv_extra=("--slot", str(slot)))
    with pytest.raises(KeyboardInterrupt):
        extract_llm.main()
    assert seen == [slot]
    assert "MCS_LLM_SLOT" not in os.environ          # main() restored the process


def test_direct_background_pass_keeps_the_default_lane(monkeypatch, tmp_path):
    import semantic_drain as drain
    seen = []
    lock = str(tmp_path / "run.lock")
    monkeypatch.setattr(extract_llm, "acquire_run_lock",
                        lambda *a, **k: mcs_util.acquire_run_lock(lock))
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
        out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
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

    monkeypatch.setattr(semantic, "_process_job", process)
    monkeypatch.setattr(semantic, "_LLM_FAILURE", "length_stop")   # left by an earlier job
    db = _seeded(tmp_path)
    try:
        out = semantic.run_due(db, _cfg("shadow"), {"errors": []},
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
    monkeypatch.setattr(semantic, "llm_conf", lambda: ("http://127.0.0.1:8080/v1/chat/completions", "m"))
    monkeypatch.setattr(semantic, "_probe_format", lambda *a, **k: "plain")
    monkeypatch.setattr(semantic, "_LONG_PATH", str(tmp_path / "long.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: response)
    assert semantic.llm_chat("完全合成の本文", timeout=30) is None
    assert semantic.last_llm_failure() == code
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: {
        "text": "{\"ok\":1}", "finish_reason": "stop", "status": 200})
    # a different prompt: the length mark above binds to the first one
    assert semantic.llm_chat("完全合成の別の本文", timeout=30) == "{\"ok\":1}"
    assert semantic.last_llm_failure() is None


@pytest.mark.parametrize("status,code", [(503, "unavailable"), (500, "http_status")])
def test_a_loading_server_defers_without_an_attempt(monkeypatch, tmp_path, status, code):
    """2026-10-10 04:03: the daily llama-server restart answered 503
    (Loading model) and the pass was charged an attempt. A 503 did no
    work, like a refused connection; other HTTP errors stay failures."""
    monkeypatch.setattr(semantic, "llm_conf", lambda: ("http://127.0.0.1:8080/v1/chat/completions", "m"))
    monkeypatch.setattr(semantic, "_probe_format", lambda *a, **k: "plain")
    monkeypatch.setattr(semantic, "_LONG_PATH", str(tmp_path / "long.json"))
    monkeypatch.setattr(local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: {
        "text": None, "finish_reason": None, "usage": None, "status": status})
    if status == 503:
        with pytest.raises(semantic.runtime.LLMNotSent):
            semantic.llm_chat("完全合成の本文", timeout=30)
    else:
        assert semantic.llm_chat("完全合成の本文", timeout=30) is None
    assert semantic.last_llm_failure() == code


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
        semantic.run_due(db, _cfg("shadow", job_budget_seconds=900), {"errors": []},
                         clock[0] + 930, jev_client=_FakeJev(), llm_fn=llm,
                         lane=lane, max_jobs=1)
        assert timeouts and timeouts[0] <= 900
        assert (timeouts[0] > semantic.LLM_TIMEOUT) is lifted
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
    in-time run is the control that the same result is otherwise saved."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])

    def llm(prompt, timeout=None):
        clock[0] += timeout + 5 if late else 10
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        before = _result_rows(db)
        out = semantic.run_due(db, _cfg("shadow", job_budget_seconds=900),
                               {"errors": []}, clock[0] + 930,
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
        raise mcs_util.RunLockLost(held=False)

    monkeypatch.setattr(mcs_util, "unlocked_transport", lost)
    db = _seeded(tmp_path)
    try:
        before = _result_rows(db)
        out = semantic.run_due(db, _cfg("shadow", job_budget_seconds=900),
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
