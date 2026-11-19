"""The wire slot follows the acquired turn on every shared-chat path."""
import json
import time

import pytest

import local_llm
import llm_slot_schedule
from test_local_llm import _lock_is_free


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("override", [None, "2", "99"])
@pytest.mark.parametrize("extra", [None, {"id_slot": 0}, {"id_slot": 2},
                                    {"id_slot": 3}, {"id_slot": -1},
                                    {"id_slot": True}, {"id_slot": "1"}])
def test_chat_projects_valid_turn_to_wire(tmp_path, monkeypatch, scheduled, override, extra):
    monkeypatch.setattr(local_llm, "SLOT_COUNT", 3)
    monkeypatch.setattr(llm_slot_schedule, "night", lambda: True)
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    monkeypatch.delenv(local_llm.TURN_LOCK_ENV, raising=False)
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    if override is not None:
        monkeypatch.setenv("MCS_LLM_SLOT", override)
    if scheduled:
        monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
        monkeypatch.setenv(local_llm.WORK_CLASS_ENV, "BACKLOG")
    captured = []

    def request(endpoint, method, body, timeout, deadline):
        captured.append(dict(body))
        return 200, {}, json.dumps({"choices": [{"message": {"content": "synthetic"},
                                               "finish_reason": "stop"}]}).encode()

    result = local_llm.chat("synthetic", extra_payload=extra, request_fn=request)
    assert result["text"] == "synthetic"
    assert len(captured) == 1
    requested = extra.get("id_slot") if extra else None
    expected = requested if type(requested) is int and 0 <= requested < 3 else local_llm.request_slot()
    assert type(captured[0]["id_slot"]) is int
    assert captured[0]["id_slot"] == expected
    if extra is not None:
        assert extra == {"id_slot": requested}, "do not mutate caller-owned payload"


@pytest.mark.parametrize("scheduled", [False, True])
def test_owned_turn_wins_over_payload_and_process_override(tmp_path, monkeypatch, scheduled):
    monkeypatch.setenv("MCS_LLM_SLOT", "1")
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    if scheduled:
        monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
    captured = []

    def request(endpoint, method, body, timeout, deadline):
        captured.append(body["id_slot"])
        return 200, {}, b'{"choices":[{"message":{"content":"synthetic"},"finish_reason":"stop"}]}'

    token = local_llm._OWNED_TURN.set(2)
    try:
        assert local_llm.chat("synthetic", extra_payload={"id_slot": 0}, request_fn=request)
    finally:
        local_llm._OWNED_TURN.reset(token)
    assert captured == [2]


def test_busy_preferred_slot_uses_the_other_acquired_slot(tmp_path, monkeypatch):
    monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(tmp_path))
    monkeypatch.setenv(local_llm.WORK_CLASS_ENV, "BACKLOG")
    monkeypatch.delenv(local_llm.TURN_LOCK_ENV, raising=False)
    monkeypatch.setattr(llm_slot_schedule, "night", lambda: False)
    captured = []

    def request(endpoint, method, body, timeout, deadline):
        captured.append(body["id_slot"])
        return 200, {}, b'{"choices":[{"message":{"content":"synthetic"},"finish_reason":"stop"}]}'

    with llm_slot_schedule.turn(tmp_path, realtime=False, preferred=1) as held:
        assert held == 1
        assert local_llm.chat("synthetic", timeout=0.5, extra_payload={"id_slot": 1}, request_fn=request)
    assert captured == [2]


def _transport_failure(tmp_path, monkeypatch, *, scheduled, extra):
    """POST dies in transport after send; count the /slots idle polls."""
    monkeypatch.setattr(local_llm, "SLOT_COUNT", 3)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_POLL_S", 0.01)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 0.05)
    monkeypatch.setattr(llm_slot_schedule, "night", lambda: True)
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, str(tmp_path / "turn.lock"))
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    if scheduled:
        schedule_root = tmp_path / "sched"
        schedule_root.mkdir()
        monkeypatch.setenv(local_llm.SCHEDULE_ENV, str(schedule_root))
        monkeypatch.setenv(local_llm.WORK_CLASS_ENV, "BACKLOG")
    posts, gets = [], []

    def request(endpoint, method, body, timeout, deadline):
        if method == "POST":
            posts.append(body["id_slot"])
            raise TimeoutError("synthetic transport timeout")
        gets.append(method)
        return 200, {}, json.dumps([{"id": s, "is_processing": False}
                                    for s in range(3)]).encode()

    err = {}
    assert local_llm.chat("synthetic", extra_payload=extra, request_fn=request,
                          error_out=err) is None
    assert err["kind"] == "transport"
    return posts, gets


@pytest.mark.parametrize("extra", [None, {"id_slot": "1"}])
def test_unpinned_legacy_request_gets_slot_but_skips_idle_wait(tmp_path, monkeypatch, extra):
    posts, gets = _transport_failure(tmp_path, monkeypatch, scheduled=False, extra=extra)
    assert posts == [local_llm.request_slot()] and gets == []


@pytest.mark.parametrize("scheduled, extra", [(False, {"id_slot": 0}),
                                              (False, {"id_slot": 3}),
                                              (True, None)])
def test_pinned_or_scheduled_request_still_waits_for_idle(tmp_path, monkeypatch, scheduled, extra):
    posts, gets = _transport_failure(tmp_path, monkeypatch, scheduled=scheduled, extra=extra)
    expected = 0 if extra == {"id_slot": 0} else local_llm.request_slot()
    assert len(posts) == 1 and type(posts[0]) is int and gets
    if not scheduled:
        assert posts == [expected]


@pytest.mark.parametrize("env_value, slot", [("1", 1), ("0", 0)])
def test_env_pinned_request_waits_for_the_same_slot_idle(
        tmp_path, monkeypatch, env_value, slot):
    # MCS_LLM_SLOT is an explicit choice like a payload pin — slot 0
    # included — and the wire slot is confirmed idle while the turn
    # lock is still held
    monkeypatch.setenv("MCS_LLM_SLOT", env_value)
    monkeypatch.setattr(local_llm, "SLOT_COUNT", 3)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_POLL_S", 0.01)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 0.05)
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, str(tmp_path / "turn.lock"))
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    lock = tmp_path / "turn.lock"
    posts, gets, locked = [], [], []

    def request(endpoint, method, body, timeout, deadline):
        if method == "POST":
            posts.append(body["id_slot"])
            raise TimeoutError("synthetic transport timeout")
        gets.append(method)
        locked.append(_lock_is_free(str(lock)))
        # the pinned slot stays busy for one sample, then reports idle twice
        busy = len(gets) == 1
        return 200, {}, json.dumps([{"id": s, "is_processing": busy and s == slot}
                                    for s in range(3)]).encode()

    err = {}
    assert local_llm.chat("synthetic", request_fn=request, error_out=err) is None
    assert posts == [slot] and len(gets) == 3 and locked == [False] * 3
    assert err["kind"] == "transport" and err["slot_wait_confirmed"] == 1


def test_held_turn_slot_waits_for_idle_after_transport_timeout(tmp_path, monkeypatch):
    # a turn held through call_turn is an explicit slot choice: the
    # inner chat reuses it on the wire and, after a sent timeout, still
    # confirms that same slot's idle while the outer lock is held
    monkeypatch.setattr(local_llm, "SLOT_COUNT", 3)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_POLL_S", 0.01)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 0.05)
    lock = tmp_path / "turn.lock"
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, str(lock))
    monkeypatch.delenv(local_llm.SCHEDULE_ENV, raising=False)
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    posts, gets, locked = [], [], []

    def request(endpoint, method, body, timeout, deadline):
        if method == "POST":
            posts.append(body["id_slot"])
            raise TimeoutError("synthetic transport timeout")
        gets.append(method)
        locked.append(_lock_is_free(str(lock)))
        # the held slot stays busy for one sample, then reports idle twice
        busy = len(gets) == 1
        return 200, {}, json.dumps([{"id": s, "is_processing": busy and s == 2}
                                    for s in range(3)]).encode()

    err = {}
    with local_llm.call_turn(time.monotonic() + 5, 0.0, slot=2) as held:
        assert held == 2
        assert local_llm.chat("synthetic", request_fn=request, error_out=err) is None
    assert posts == [2] and len(gets) == 3 and locked == [False] * 3
    assert err["kind"] == "transport" and err["slot_wait_confirmed"] == 1


def test_busy_turn_returns_before_any_send(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def busy(*_a, **_k):
        yield None
    monkeypatch.setattr(local_llm, "_request_turn", busy)
    sent = []
    err = {}
    assert local_llm.chat("synthetic", request_fn=lambda *a: sent.append(a),
                          error_out=err) is None
    assert sent == [] and err == {"kind": "busy"}
