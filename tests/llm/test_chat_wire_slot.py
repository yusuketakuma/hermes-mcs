"""The wire slot follows the acquired turn on every shared-chat path."""
import json

import pytest

import local_llm
import llm_slot_schedule


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
