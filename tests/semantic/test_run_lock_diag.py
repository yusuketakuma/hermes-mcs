"""Log-only run-lock timing: transport vs. reacquire seconds and the window
a lost reacquire had — never persisted, never carrying a job identity."""
import json
import os
import time

import pytest

import mcs_util
import semantic
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

KEYS = {"llm_transport_s", "llm_reacquire_s", "jev_transport_s", "jev_reacquire_s"}


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_util, "code_stamp", lambda: 1)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    path = str(tmp_path / "run.lock")
    fd = mcs_util.acquire_run_lock(path)
    yield tmp_path, path, fd
    os.close(fd)


class _HookableJev(_FakeJev):
    """Routes evaluate() through _http_request so run_due can unlock it."""
    _mcs_jev_hookable = True

    def __init__(self):
        super().__init__()
        self.http = 0

    def _http_request(self, *args, **kwargs):
        self.http += 1

    def evaluate(self, state, questions, deadline):
        self._http_request()
        return super().evaluate(state, questions, deadline)


def test_a_normal_pass_reports_transport_seconds_only_in_its_output(lock_env):
    tmp_path, _, fd = lock_env
    db = _seeded(tmp_path)
    jev = _HookableJev()
    try:
        out = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 480,
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
    """2026-10-10: two losses fell inside one tick's 347 s hold. The lost
    pass writes nothing, so its output is the only place the window shows."""
    tmp_path, path, fd = lock_env
    monkeypatch.setattr(mcs_util, "RUN_LOCK_REACQUIRE_S", 0.3)
    monkeypatch.setattr(mcs_util, "RUN_LOCK_POLL_S", 0.01)
    db = _seeded(tmp_path)
    other = []

    def model(prompt):
        other.append(mcs_util.acquire_run_lock(path))   # the tick holds it
        return _llm(prompt)

    try:
        out = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 480,
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
        out = semantic.run_due(db, _cfg(), {"errors": []}, clock[0] + 480,
                               llm_fn=model, jev_client=_FakeJev(), run_lock_fd=fd)
        assert len(calls) >= 2
        assert out["transport"]["llm_transport_s"] == pytest.approx(0.04 * len(calls))
        assert out["transport"]["llm_reacquire_s"] == 0
    finally:
        db.close()
