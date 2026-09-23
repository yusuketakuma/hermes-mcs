"""Shared local-LLM transport/response-metadata adapter tests.

Fake loopback only — no real service is touched.  Canonical acceptance
rejects truncation/empty/malformed output; legacy callers keep their
existing ``{}`` result while recording bounded integrity metadata.
"""
import io
import json
import urllib.error

import pytest

import extract_llm
import local_llm
import semantic


def _fake_request(payload, *, status=200):
    def send(endpoint, method, body, timeout, deadline=None):
        return status, {}, json.dumps(payload).encode()
    return send


def _response(text, finish="stop", usage=None):
    out = {"choices": [{"message": {"content": text},
                        "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out


def test_chat_returns_text_finish_reason_and_usage():
    response = local_llm.chat(
        "prompt", request_fn=_fake_request(
            _response('{"a": 1}', finish="stop",
                      usage={"prompt_tokens": 5, "completion_tokens": 7,
                             "total_tokens": 12})))
    assert response["status"] == 200
    assert response["text"] == '{"a": 1}'
    assert response["finish_reason"] == "stop"
    assert response["usage"]["total_tokens"] == 12


def test_canonical_acceptance_rejects_length_stop_and_empty():
    assert local_llm.acceptance_error(None) == "transport"
    assert local_llm.acceptance_error(
        {"status": 500, "text": None}) == "http_status"
    assert local_llm.acceptance_error(
        {"status": 200, "text": "", "finish_reason": "stop"}) == "empty"
    assert local_llm.acceptance_error(
        {"status": 200, "text": "  ", "finish_reason": "stop"}) == "empty"
    assert local_llm.acceptance_error(
        {"status": 200, "text": '{"a": 1',
         "finish_reason": "length"}) == "length_stop"
    assert local_llm.acceptance_error(
        {"status": 200, "text": "{}", "finish_reason": "stop"}) is None


def test_non_loopback_endpoint_and_bad_args_fail_closed():
    with pytest.raises(ValueError, match="local_endpoint_not_allowed"):
        local_llm.chat("p", endpoint="https://example.com/v1/chat")
    with pytest.raises(ValueError, match="prompt_invalid"):
        local_llm.chat("")
    with pytest.raises(ValueError, match="timeout_invalid"):
        local_llm.chat("p", timeout=0)


def test_transport_failure_and_malformed_payloads_return_none():
    assert local_llm.chat(
        "p", request_fn=lambda *a, **k: 1 / 0) is None
    assert local_llm.chat("p", request_fn=_fake_request(
        {"unexpected": True})) is None
    assert local_llm.chat("p", request_fn=_fake_request(
        {"choices": [{"message": {"content": 5}}]})) is None


def test_semantic_llm_chat_uses_shared_adapter(monkeypatch):
    captured = {}

    def send(endpoint, method, body, timeout, deadline=None):
        captured["endpoint"] = endpoint
        return 200, {}, json.dumps(_response("text-out")).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("hello") == "text-out"
    assert captured["endpoint"] == semantic.LLM_ENDPOINT


def test_extract_llm_keeps_empty_object_contract_and_records_integrity(
        monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")

    class FakeOpener:
        def open(self, req, timeout=None):
            return io.BytesIO(json.dumps(_response(
                "{}", usage={"prompt_tokens": 3, "completion_tokens": 1,
                             "total_tokens": 4})).encode())

    monkeypatch.setattr(extract_llm, "_OPENER", FakeOpener())
    extract_llm._note_list().clear()
    assert extract_llm._llm_call("prompt") == {}
    notes = extract_llm._note_list()
    assert notes[0]["usage"]["total_tokens"] == 4


def test_extract_llm_valid_empty_object_still_empty(monkeypatch):
    """Legacy/shadow contract: a valid ``{}`` stays a result, not an
    error — canonical cutover decides later."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")

    class FakeOpener:
        def open(self, req, timeout=None):
            return io.BytesIO(json.dumps(_response("{}")).encode())

    monkeypatch.setattr(extract_llm, "_OPENER", FakeOpener())
    assert extract_llm.llm_extract("なにも記載なし") == {}


def test_extract_llm_format_degrade_still_walks_ladder(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    calls = []

    class Rejecting:
        def open(self, req, timeout=None):
            calls.append(json.loads(req.data.decode())
                         .get("response_format"))
            raise urllib.error.HTTPError(
                req.full_url, 422, "unprocessable", {}, None)

    monkeypatch.setattr(extract_llm, "_OPENER", Rejecting())
    assert extract_llm._llm_call("prompt") is None
    assert calls == [{"type": "json_schema",
                      "json_schema": extract_llm._SCHEMA},
                     {"type": "json_object"}, None]
    assert extract_llm._FMT_MODE == "plain"


def test_llm_extract_attaches_bounded_integrity_to_nonempty_output(
        monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    payload = {"meds": [{"name": "薬A", "status": "current",
                         "subject": "patient", "negated": False}]}

    class FakeOpener:
        def open(self, req, timeout=None):
            return io.BytesIO(json.dumps(_response(
                json.dumps(payload),
                usage={"prompt_tokens": 9, "completion_tokens": 4,
                       "total_tokens": 13})).encode())

    monkeypatch.setattr(extract_llm, "_OPENER", FakeOpener())
    meta = {}
    out = extract_llm.llm_extract("薬Aを服用中", meta_out=meta)
    assert "_integrity" not in out       # visible dict unchanged
    assert meta["calls"] == 1
    assert meta["usage"]["total_tokens"] == 13
    assert meta["length_stops"] == 0


def test_llm_chat_pins_background_slot(monkeypatch):
    """semantic.llm_chat must pin every call to slot 1 (wire id_slot 0)
    so slot 2 stays reserved for real-time traffic."""
    seen = {}

    def fake_chat(prompt, **kw):
        seen.update(kw)
        return {"status": 200, "text": "ok", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    assert semantic.llm_chat("hi") == "ok"
    assert seen["extra_payload"] == {"id_slot": local_llm.BACKGROUND_SLOT}
    assert local_llm.BACKGROUND_SLOT == local_llm.SLOT_1 - 1 == 0


def test_probe_format_pins_background_slot():
    """probe_format request bodies also carry the background slot pin."""
    bodies = []

    class CapOpener:
        def open(self, req, timeout=0):
            bodies.append(json.loads(req.data))
            return io.BytesIO(json.dumps(_response('{"ok": true}')).encode())

    mode = local_llm.probe_format("http://127.0.0.1:8080/v1/chat/completions",
                                  "m", None, CapOpener())
    assert mode == "object"
    assert bodies and all(b.get("id_slot") == local_llm.BACKGROUND_SLOT
                          for b in bodies)


def test_extract_llm_call_pins_background_slot(monkeypatch):
    """The legacy extract path pins the same background slot."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    seen = {}

    def fake_chat(prompt, **kw):
        seen.update(kw)
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    extract_llm.llm_extract("test")
    assert seen["extra_payload"] == {"id_slot": local_llm.BACKGROUND_SLOT}
