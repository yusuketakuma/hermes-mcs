"""Log-only run-lock timing: transport vs. reacquire seconds and the window
a lost reacquire had — never persisted, never carrying a job identity."""
import writer_lock
import json
import os
import time

import pytest

import semantic
import semantic_drain
import semantic_policy
import semantic_store
import semantic_runtime as runtime
from semantic_testkit import _cfg, _FakeJev, _llm, _llm_v2, _seeded

KEYS = {"llm_transport_s", "llm_reacquire_s", "jev_transport_s", "jev_reacquire_s"}


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 1)
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    path = str(tmp_path / "run.lock")
    fd = writer_lock.acquire_run_lock(path)
    yield tmp_path, path, fd
    os.close(fd)


class _HookableJev(_FakeJev):
    """Routes evaluate() through request so run_due can unlock it."""
    supports_attempt_hooks = True

    def __init__(self):
        super().__init__()
        self.http = 0

    def request(self, *args, **kwargs):
        self.http += 1

    def evaluate(self, state, questions, deadline, *, before_attempt=None,
                 after_result=None, reserve_fn=None, transport=None):
        if before_attempt is not None:
            before_attempt()
        if reserve_fn is not None:
            reserve_fn({}, 0.0)
        if transport is None:
            self.request()
        else:
            transport(self.request)
        value = super().evaluate(state, questions, deadline)
        if after_result is not None:
            after_result()
        return value


def _direct_shadow_stage(db, deadline):
    """A standalone stage has no run_due accounting capability by default."""
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    member = bundle["members"][0]
    calls = []

    def model(prompt):
        if "原子事実抽出器" in prompt:
            calls.append(1)
            return _llm_v2(prompt)
        return _llm(prompt)

    scfg, errors = semantic_policy.semantic_config(_cfg("shadow", fact_source="shadow"))
    assert not errors
    semantic_drain._fact_stage(
        db, scfg, member, 1, member["message_id"], "direct-synthetic-source", "direct-policy",
        _FakeJev(), model, deadline)
    assert calls                       # an actual standalone comparison ran


def test_a_normal_pass_reports_transport_seconds_only_in_its_output(lock_env):
    tmp_path, _, fd = lock_env
    db = _seeded(tmp_path)
    jev = _HookableJev()
    try:
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 480,
                               llm_fn=_llm, jev_client=jev, run_lock_fd=fd)
        assert out["done"] == 1 and "run_lock_lost_detail" not in out
        assert set(out["transport"]) == KEYS
        assert all(type(v) is float and v >= 0 for v in out["transport"].values())
        assert jev.http > 0
        [run] = db.db.execute("SELECT content FROM artifacts "
                              "WHERE kind='semantic_drain_run'").fetchall()
        assert "transport" not in json.loads(run[0])      # log only
    finally:
        db.close()


def test_a_lost_reacquire_reports_its_window_without_job_identity(lock_env, monkeypatch):
    """A bounded reacquire loss retains wait diagnostics without changing jobs."""
    tmp_path, path, fd = lock_env
    monkeypatch.setattr(writer_lock, "RUN_LOCK_REACQUIRE_S", 0.3)
    monkeypatch.setattr(writer_lock, "RUN_LOCK_POLL_S", 0.01)
    db = _seeded(tmp_path)
    other = []

    def model(prompt):
        other.append(writer_lock.acquire_run_lock(path))   # the tick holds it
        return _llm(prompt)

    try:
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 480,
                               llm_fn=model, jev_client=_FakeJev(), run_lock_fd=fd)
        assert out["run_lock_lost"] == "timeout"
        detail = out["run_lock_lost_detail"]
        assert set(detail) == {"kind", "transport_s", "reacquire_s", "window_s"}
        assert detail["kind"] == "llm" and detail["window_s"] == 0.3
        assert detail["reacquire_s"] >= 0.3 and detail["transport_s"] >= 0
        assert out["transport"]["llm_reacquire_s"] >= 0.3
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE "
                             "kind='semantic_drain_run'").fetchone()[0] == 0
    finally:
        for o in other:
            if o is not None:
                os.close(o)
        db.close()


def test_short_calls_add_up_instead_of_rounding_away(lock_env, monkeypatch):
    """Review condition: per-call rounding to 0.1 s would report 0 for any
    number of 0.04 s calls. The sum is kept raw."""
    tmp_path, _, fd = lock_env
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    calls = []

    def model(prompt):
        calls.append(1)
        clock[0] += 0.04                     # transport only; reacquire is 0
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg(), {"errors": []}, clock[0] + 480,
                               llm_fn=model, jev_client=_FakeJev(), run_lock_fd=fd)
        assert len(calls) >= 2
        assert out["transport"]["llm_transport_s"] == pytest.approx(0.04 * len(calls))
        assert out["transport"]["llm_reacquire_s"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("complete", [True, False])
def test_a_pass_reports_what_the_shadow_v2_comparison_cost(tmp_path, monkeypatch, complete):
    """Log-only: the shadow comparison is evaluation only, so its time per
    pass is reported (never persisted) to size what sampling it would save."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    sent = []

    def model(prompt):
        if "原子事実抽出器" in prompt:              # a shadow v2 chunk
            sent.append(1)
            clock[0] += 30.0
            return _llm_v2(prompt) if complete else "not a document"
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow", fact_source="shadow"),
                               {"errors": []}, clock[0] + 480,
                               jev_client=_FakeJev(), llm_fn=model)
        shadow = out["shadow_v2"]
        assert shadow["calls"] >= 1 and sent
        assert shadow["s"] == pytest.approx(30.0 * len(sent))
        assert shadow["incomplete"] == (0 if complete else shadow["calls"])
        [run] = db.db.execute("SELECT content FROM artifacts "
                              "WHERE kind='semantic_drain_run'").fetchall()
        assert "shadow_v2" not in json.loads(run[0])     # log only
        # released with the pass: a later direct _fact_stage cannot add to it
        before = dict(out["shadow_v2"])
        _direct_shadow_stage(db, clock[0] + 480)
        assert out["shadow_v2"] == before
    finally:
        db.close()


def test_a_shadow_call_ended_by_the_budget_counts_as_incomplete(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])

    def model(prompt):
        if "原子事実抽出器" in prompt:
            clock[0] += 30.0
            raise runtime.RuntimeBudgetShort("synthetic")
        return _llm(prompt)

    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow", fact_source="shadow"),
                               {"errors": []}, clock[0] + 480,
                               jev_client=_FakeJev(), llm_fn=model)
        assert out["shadow_v2"] == {"calls": 1, "incomplete": 1, "s": 30.0}
        assert out["deferred"] == 1 and out["done"] == 0   # still propagates
        before = dict(out["shadow_v2"])
        _direct_shadow_stage(db, clock[0] + 480)
        assert out["shadow_v2"] == before
    finally:
        db.close()


def test_other_fact_sources_report_no_shadow_cost(tmp_path):
    db = _seeded(tmp_path)
    try:
        out = semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                               time.monotonic() + 480, jev_client=_FakeJev(),
                               llm_fn=_llm)
        assert out["shadow_v2"] == {"calls": 0, "incomplete": 0, "s": 0.0}
    finally:
        db.close()
