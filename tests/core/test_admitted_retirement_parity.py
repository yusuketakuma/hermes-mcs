"""Chat and probe retire a sent permit with the same fail-closed rule.

Pins state, outcome and proof for every post-send path so the shared
retirement helper cannot drift between the two admitted entry points."""
import json
from contextlib import suppress

import pytest

import llm_admission as adm


@pytest.fixture
def broker_path(tmp_path, monkeypatch):
    import local_llm
    path = str(tmp_path / "adm.db")
    b = adm.Broker(path)
    b.open_epoch(lambda: True)
    b.close()
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    local_llm._BROKERS.clear()
    yield path
    for b in local_llm._BROKERS.values():
        with suppress(Exception):
            b.close()
    local_llm._BROKERS.clear()


def _refused(*a):
    raise ConnectionRefusedError("synthetic")


def _dropped(*a):
    raise TimeoutError("synthetic")


def _malformed(*a):
    return 200, {}, b'{"choices": []}'


def _ok(*a):
    return 200, {}, json.dumps({"choices": [{
        "message": {"content": '{"ok": true}'},
        "finish_reason": "stop"}]}).encode()


def _row(path):
    import local_llm
    return tuple(local_llm._broker(path).db.execute(
        "SELECT state,outcome,proof FROM permits").fetchone())


@pytest.mark.parametrize("send,expected", [
    (_refused, ("terminal", "not_sent", "unreachable")),
    (_malformed, ("terminal", "done", "malformed_200")),
    (_dropped, ("unknown", "transport", None)),
    (_ok, ("terminal", "done", None)),
])
def test_chat_retirement(broker_path, send, expected):
    import local_llm
    local_llm.admitted_chat("mcs.extract", "p", request_fn=send)
    assert _row(broker_path) == expected


@pytest.mark.parametrize("send,expected", [
    (_refused, ("terminal", "not_sent", "unreachable")),
    (_malformed, ("terminal", "done", "probe_malformed_200")),
    (_dropped, ("unknown", "probe_transport", None)),
    (_ok, ("terminal", "done", "probe_returned")),
])
def test_probe_retirement(broker_path, send, expected):
    import local_llm
    local_llm.admitted_probe_format(
        "mcs.extract", local_llm.ENDPOINT, "m", None, request_fn=send)
    assert _row(broker_path) == expected
