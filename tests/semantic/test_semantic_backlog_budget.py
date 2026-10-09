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
