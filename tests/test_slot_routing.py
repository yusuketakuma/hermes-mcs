"""Slot routing: local_llm.request_slot env override and
extract_llm._choose_slot --lend-rt / --slot precedence."""
import json

import extract_llm
import local_llm


def test_request_slot_default(monkeypatch):
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    assert local_llm.request_slot() == local_llm.BACKGROUND_SLOT


def test_request_slot_env_override(monkeypatch):
    monkeypatch.setenv("MCS_LLM_SLOT", "1")
    assert local_llm.request_slot() == 1


def test_request_slot_env_invalid(monkeypatch):
    for bad in ("", "x", "-1", "  "):
        monkeypatch.setenv("MCS_LLM_SLOT", bad)
        assert local_llm.request_slot() == local_llm.BACKGROUND_SLOT


def _set_state(monkeypatch, slot=None, lend=False):
    monkeypatch.setattr(extract_llm, "_SLOT_OVERRIDE", slot)
    monkeypatch.setattr(extract_llm, "_LEND_RT", lend)
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)


def test_choose_slot_default(monkeypatch):
    _set_state(monkeypatch)
    assert extract_llm._choose_slot() == local_llm.BACKGROUND_SLOT


def test_choose_slot_override_wins(monkeypatch):
    _set_state(monkeypatch, slot=3, lend=True)
    assert extract_llm._choose_slot() == 3


def test_choose_slot_lend_idle(monkeypatch):
    _set_state(monkeypatch, lend=True)
    slots = [{"id": local_llm.BACKGROUND_SLOT, "is_processing": True},
             {"id": local_llm.REALTIME_SLOT, "is_processing": False}]
    monkeypatch.setattr(extract_llm, "_opener_request",
                        lambda *a, **k: (200, {}, json.dumps(slots).encode()))
    assert extract_llm._choose_slot() == local_llm.REALTIME_SLOT


def test_choose_slot_lend_busy_falls_back(monkeypatch):
    _set_state(monkeypatch, lend=True)
    slots = [{"id": local_llm.BACKGROUND_SLOT, "is_processing": True},
             {"id": local_llm.REALTIME_SLOT, "is_processing": True}]
    monkeypatch.setattr(extract_llm, "_opener_request",
                        lambda *a, **k: (200, {}, json.dumps(slots).encode()))
    assert extract_llm._choose_slot() == local_llm.BACKGROUND_SLOT


def test_choose_slot_lend_probe_failure_falls_back(monkeypatch):
    _set_state(monkeypatch, lend=True)

    def boom(*a, **k):
        raise OSError("server down")

    monkeypatch.setattr(extract_llm, "_opener_request", boom)
    assert extract_llm._choose_slot() == local_llm.BACKGROUND_SLOT


def _capture_bodies():
    bodies = []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        reply = {"choices": [{"message": {"content": '{"ok": true}'},
                              "finish_reason": "stop"}]}
        return 200, {}, json.dumps(reply).encode()

    return bodies, send


def test_probe_format_honors_env_override(monkeypatch):
    """MCS_LLM_SLOT is the documented per-process wire id_slot override —
    the format probe must follow request_slot() like every other call."""
    monkeypatch.setenv("MCS_LLM_SLOT", "3")
    bodies, send = _capture_bodies()
    mode = local_llm.probe_format("http://127.0.0.1:8080/v1/chat/completions",
                                  "m", None, request_fn=send)
    assert mode == "object"
    assert bodies and all(b.get("id_slot") == 3 for b in bodies)


def test_probe_format_explicit_slot_wins(monkeypatch):
    """A caller's own slot decision takes precedence over the env."""
    monkeypatch.setenv("MCS_LLM_SLOT", "3")
    bodies, send = _capture_bodies()
    mode = local_llm.probe_format("http://127.0.0.1:8080/v1/chat/completions",
                                  "m", None, request_fn=send, slot=2)
    assert mode == "object"
    assert bodies and all(b.get("id_slot") == 2 for b in bodies)


def test_probe_format_bad_slot_falls_back(monkeypatch):
    """A malformed explicit slot must never become an unpinned request —
    llama.cpp treats out-of-range id_slot as unpinned, which could land
    the probe on the real-time slot."""
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    for bad in (-1, "1", 0.5, True):
        bodies, send = _capture_bodies()
        mode = local_llm.probe_format(
            "http://127.0.0.1:8080/v1/chat/completions",
            "m", None, request_fn=send, slot=bad)
        assert mode == "object"
        assert bodies and all(b.get("id_slot") == local_llm.BACKGROUND_SLOT
                              for b in bodies)


def test_extract_probe_format_uses_choose_slot(monkeypatch):
    """extract_llm's probe resolves its slot through _choose_slot — the
    same single point as the extraction calls it precedes."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", None)
    monkeypatch.setattr(extract_llm, "_FMT_TS", 0.0)
    monkeypatch.setattr(extract_llm, "_SLOT_OVERRIDE", 3)
    monkeypatch.setattr(extract_llm, "_LEND_RT", False)
    bodies, send = _capture_bodies()
    monkeypatch.setattr(extract_llm, "_opener_request", send)
    assert extract_llm._probe_format() == "schema"
    assert bodies and all(b.get("id_slot") == 3 for b in bodies)
