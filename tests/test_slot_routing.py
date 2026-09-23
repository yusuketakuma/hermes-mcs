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
