"""After a sent request times out, chat() holds the GPU turn until the
pinned slot reports idle twice (bounded), then releases it either way."""
import json
import os
import subprocess
import sys
import time

import pytest

import bounded_http
import local_llm
from test_local_llm import _lock_is_free


def _fake(path, samples, *, error=TimeoutError("http worker timeout")):
    """POST raises `error`; GET answers /slots from `samples` (a bool means
    slot 1's is_processing, anything else is returned raw)."""
    state = {"gets": 0, "locked": []}

    def send(endpoint, method, body, timeout, deadline):
        if method == "POST":
            raise error
        state["gets"] += 1
        state["locked"].append(_lock_is_free(path))
        sample = samples[min(state["gets"] - 1, len(samples) - 1)]
        if isinstance(sample, bool):
            raw = json.dumps([{"id": 0, "is_processing": True},
                              {"id": 1, "is_processing": sample}]).encode()
            return 200, {}, raw
        if isinstance(sample, Exception):
            raise sample
        return sample
    return send, state


def _chat(send, err, **kw):
    return local_llm.chat("prompt", request_fn=send, error_out=err,
                          extra_payload={"id_slot": 1}, **kw)


@pytest.fixture
def turn(tmp_path, monkeypatch):
    path = str(tmp_path / "turn.lock")
    monkeypatch.setenv(local_llm.TURN_LOCK_ENV, path)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_POLL_S", 0.01)
    return path


def test_sent_timeout_holds_turn_until_two_consecutive_idle_samples(turn):
    send, state = _fake(turn, [True, True, False, False])
    err = {}
    assert _chat(send, err) is None
    assert state["gets"] == 4 and state["locked"] == [False] * 4
    assert err["kind"] == "transport" and err["slot_wait_confirmed"] == 1
    assert type(err["slot_wait_ms"]) is int
    assert _lock_is_free(turn)


def test_single_idle_sample_is_not_trusted(turn):
    send, state = _fake(turn, [True, False, True, False, False])
    err = {}
    assert _chat(send, err) is None
    assert state["gets"] == 5 and err["slot_wait_confirmed"] == 1


@pytest.mark.parametrize("bad", [
    (200, {}, b"not json"),
    (200, {}, json.dumps([{"id": 0, "is_processing": False}]).encode()),
    (200, {}, b"{}"),
    (500, {}, b"[]"),
    OSError("probe failed"),
])
def test_bad_samples_reset_the_idle_run(turn, bad):
    send, state = _fake(turn, [False, bad, False, False])
    err = {}
    assert _chat(send, err) is None
    assert state["gets"] == 4 and err["slot_wait_confirmed"] == 1


def test_never_idle_within_bound_releases_unconfirmed(turn, monkeypatch):
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 0.1)
    send, state = _fake(turn, [True])
    err = {}
    started = time.monotonic()
    assert _chat(send, err) is None
    assert time.monotonic() - started < 1
    assert err["slot_wait_confirmed"] == 0 and 50 <= err["slot_wait_ms"] <= 500
    assert state["gets"] >= 2 and _lock_is_free(turn)


def test_caller_deadline_ends_the_poll(turn, monkeypatch):
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 5.0)
    send, _state = _fake(turn, [True])
    err = {}
    started = time.monotonic()
    # enough margin to reach the send before the deadline on a loaded
    # machine; the poll itself still ends at the deadline, well under 5 s
    assert _chat(send, err, deadline=time.monotonic() + 0.3) is None
    assert time.monotonic() - started < 1
    assert err["slot_wait_confirmed"] == 0 and err["slot_wait_ms"] < 5000


def test_outcomes_are_reported_separately(turn, monkeypatch):
    confirmed, expired = {}, {}
    _chat(_fake(turn, [False, False])[0], confirmed)
    monkeypatch.setattr(local_llm, "SLOT_WAIT_MAX_S", 0.05)
    _chat(_fake(turn, [True])[0], expired)
    assert (confirmed["slot_wait_confirmed"], expired["slot_wait_confirmed"]) == (1, 0)
    assert type(confirmed["slot_wait_ms"]) is int and type(expired["slot_wait_ms"]) is int


def test_no_wait_without_turn_lock_env(tmp_path, monkeypatch):
    monkeypatch.delenv(local_llm.TURN_LOCK_ENV, raising=False)
    send, state = _fake(str(tmp_path / "x"), [False, False])
    calls = []

    def counting(*a):
        calls.append(a[1])
        return send(*a)
    err = {}
    assert _chat(counting, err) is None
    assert calls == ["POST"] and err == {"kind": "transport"}


@pytest.mark.parametrize("error, kind", [
    (bounded_http.NotSentTimeout("not sent"), "unreachable"),
    (ValueError("bad"), "transport"),
])
def test_not_sent_timeout_and_non_transport_errors_skip_the_wait(turn, error, kind):
    send, state = _fake(turn, [False, False], error=error)
    err = {}
    assert _chat(send, err) is None
    assert state["gets"] == 0 and err == {"kind": kind}


def test_unpinned_request_skips_the_wait(turn):
    send, state = _fake(turn, [False, False])
    err = {}
    assert local_llm.chat("prompt", request_fn=send, error_out=err) is None
    assert state["gets"] == 0 and err == {"kind": "transport"}


def test_admitted_chat_keeps_permit_unknown_after_confirmed_idle(turn, tmp_path, monkeypatch):
    import llm_admission as adm
    path = str(tmp_path / "adm.db")
    monkeypatch.setenv("MCS_LLM_ADMISSION", path)
    monkeypatch.setattr(local_llm, "_RECONCILED_AT", {})
    broker = local_llm._broker(path)
    broker.open_epoch(lambda: True)
    assert isinstance(broker, adm.Broker)
    send, state = _fake(turn, [False, False])
    err = {}
    assert local_llm.admitted_chat("mcs.extract", "prompt", request_fn=send, error_out=err,
                                   extra_payload={"id_slot": 1}) is None
    assert err["slot_wait_confirmed"] == 1 and state["gets"] == 2
    assert broker._permit(1)["state"] == "unknown"
    assert local_llm._RECONCILED_AT == {}


def test_worker_restart_releases_the_turn(turn, tmp_path):
    code = """
import json, os, sys, time
sys.path.insert(0, os.environ["MCS_TEST_MODULE_ROOT"])
import _mcs_path
import local_llm
local_llm.SLOT_WAIT_MAX_S = 30
def send(endpoint, method, body, timeout, deadline):
    if method == "POST":
        raise TimeoutError("http worker timeout")
    print("polling", flush=True)
    return 200, {}, json.dumps([{"id": 1, "is_processing": True}]).encode()
local_llm.chat("prompt", request_fn=send, extra_payload={"id_slot": 1})
"""
    root = os.path.dirname(os.path.dirname(local_llm.__file__))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "MCS_TEST_MODULE_ROOT": root})
    assert child.stdout.readline().strip() == "polling"
    assert not _lock_is_free(turn)
    child.kill()
    child.wait(timeout=10)
    assert _lock_is_free(turn)
    sent = []

    def send(endpoint, method, body, timeout, deadline):
        sent.append(method)
        return 200, {}, json.dumps({"choices": [{"message": {"content": "{}"},
                                                 "finish_reason": "stop"}]}).encode()
    assert local_llm.chat("prompt", request_fn=send, extra_payload={"id_slot": 1})["text"] == "{}"
    assert sent == ["POST"]


def test_slots_idle_default_request_and_slot_filter(monkeypatch):
    raw = json.dumps([{"id": 0, "is_processing": True}, {"id": 1, "is_processing": False}]).encode()
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (200, {}, raw))
    assert local_llm._slots_idle(local_llm.ENDPOINT, None) is False
    assert local_llm._slots_idle(local_llm.ENDPOINT, None, slot=1) is True
    assert local_llm._slots_idle(local_llm.ENDPOINT, None, slot=0) is False


def test_integrity_note_records_slot_wait_only_when_present():
    import extract_llm
    notes = extract_llm._note_list()
    notes.clear()
    extract_llm._integrity_note(None, err_out={"kind": "transport", "slot_wait_ms": 420,
                                               "slot_wait_confirmed": 0})
    extract_llm._integrity_note(None, err_out={"kind": "transport"})
    extract_llm._integrity_note(None)
    assert notes[0]["slot_wait_ms"] == 420 and notes[0]["slot_wait_confirmed"] == 0
    assert "slot_wait_ms" not in notes[1] and "slot_wait_ms" not in notes[2]
    summary = extract_llm._integrity_summary(list(notes))
    assert summary["slot_wait_ms"] == 420 and summary["slot_wait_unconfirmed"] == 1
    plain = extract_llm._integrity_summary(notes[1:])
    assert "slot_wait_ms" not in plain and "slot_wait_unconfirmed" not in plain
    assert extract_llm._llm_call_stats([plain])["calls"] == 2
    assert "slot_wait_ms" not in extract_llm._llm_call_stats([summary])
    notes.clear()
