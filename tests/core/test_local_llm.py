"""Shared local-LLM transport/response-metadata adapter tests.

Fake loopback only — no real service is touched.  Canonical acceptance
rejects truncation/empty/malformed output; legacy callers keep their
existing ``{}`` result while recording bounded integrity metadata.
"""
import json

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

    monkeypatch.setattr(extract_llm, "_opener_request", _fake_request(_response(
        "{}", usage={"prompt_tokens": 3, "completion_tokens": 1,
                     "total_tokens": 4})))
    extract_llm._note_list().clear()
    assert extract_llm._llm_call("prompt") == {}
    notes = extract_llm._note_list()
    assert notes[0]["usage"]["total_tokens"] == 4


def test_extract_llm_valid_empty_object_still_empty(monkeypatch):
    """Legacy/shadow contract: a valid ``{}`` stays a result, not an
    error — canonical cutover decides later."""
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")

    monkeypatch.setattr(extract_llm, "_opener_request",
                        _fake_request(_response("{}")))
    assert extract_llm.llm_extract("なにも記載なし") == {}


def test_extract_llm_format_degrade_still_walks_ladder(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "schema")
    calls = []

    def reject(endpoint, method, body, timeout, deadline=None):
        calls.append(body.get("response_format"))
        return 422, {}, b""

    monkeypatch.setattr(extract_llm, "_opener_request", reject)
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

    monkeypatch.setattr(extract_llm, "_opener_request", _fake_request(_response(
        json.dumps(payload), usage={"prompt_tokens": 9, "completion_tokens": 4,
                                   "total_tokens": 13})))
    meta = {}
    out = extract_llm.llm_extract("薬Aを服用中", meta_out=meta)
    assert "_integrity" not in out       # visible dict unchanged
    assert meta["calls"] == 1
    assert meta["usage"]["total_tokens"] == 13
    assert meta["length_stops"] == 0


def test_chat_returns_llama_timings_sanitized():
    """llama.cpp `timings` rides the response dict — prompt eval and
    decode are measured separately so throughput tuning has data."""
    payload = _response('{"a": 1}')
    payload["timings"] = {"prompt_n": 900, "prompt_ms": 3100.5,
                          "predicted_n": 40, "predicted_ms": 2200,
                          "cache_n": 800, "text": "ignored",
                          "nope": -1}
    response = local_llm.chat("prompt", request_fn=_fake_request(payload))
    assert response["timings"] == {
        "prompt_n": 900, "prompt_ms": 3100.5, "predicted_n": 40,
        "predicted_ms": 2200, "cache_n": 800}
    # absent timings -> absent key content, never a crash
    response = local_llm.chat("prompt",
                              request_fn=_fake_request(_response("{}")))
    assert response["timings"] is None


def test_llm_extract_integrity_aggregates_timings(monkeypatch):
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    payload = _response('{"summary": "s"}')
    payload["timings"] = {"prompt_n": 100, "prompt_ms": 500,
                          "predicted_n": 10, "predicted_ms": 200,
                          "cache_n": 60}
    monkeypatch.setattr(extract_llm, "_opener_request",
                        _fake_request(payload))
    meta = {}
    extract_llm.llm_extract("本文", meta_out=meta)
    assert meta["timings"]["cache_n"] == 60
    assert meta["timings"]["predicted_ms"] == 200


def test_chat_ignores_unrepresentable_timing():
    payload = _response('{"summary": "synthetic"}')
    payload["timings"] = {"prompt_ms": 10 ** 400, "predicted_ms": 10}
    response = local_llm.chat("prompt", request_fn=_fake_request(payload))
    assert response["text"] == '{"summary": "synthetic"}'
    assert response["timings"] == {"predicted_ms": 10}


def test_llm_chat_pins_background_slot(monkeypatch):
    """semantic.llm_chat must pin every call to slot 1 (wire id_slot 0)
    so slot 2 stays reserved for real-time traffic."""
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    seen = {}

    def fake_chat(prompt, **kw):
        seen.update(kw)
        return {"status": 200, "text": "ok", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    assert semantic.llm_chat("hi") == "ok"
    assert seen["extra_payload"] == {"id_slot": local_llm.BACKGROUND_SLOT}
    assert local_llm.BACKGROUND_SLOT == local_llm.SLOT_1 - 1 == 0


def test_probe_format_pins_background_slot(monkeypatch):
    """probe_format request bodies also carry the background slot pin."""
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    bodies = []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        return 200, {}, json.dumps(_response('{"ok": true}')).encode()

    mode = local_llm.probe_format("http://127.0.0.1:8080/v1/chat/completions",
                                  "m", None, request_fn=send)
    assert mode == "object"
    assert bodies and all(b.get("id_slot") == local_llm.BACKGROUND_SLOT
                          for b in bodies)


def test_request_slot_rejects_non_ascii_digits(monkeypatch):
    monkeypatch.setenv("MCS_LLM_SLOT", "²")
    assert local_llm.request_slot() == local_llm.BACKGROUND_SLOT
    monkeypatch.setenv("MCS_LLM_SLOT", " 1 ")
    assert local_llm.request_slot() == 1


def test_request_slot_rejects_out_of_range(monkeypatch):
    """A slot index at/past the deployed count is invalid — llama.cpp
    treats out-of-range id_slot as UNPINNED, which could land a
    background call on the real-time slot (T19)."""
    for bad in (str(local_llm.SLOT_COUNT), str(local_llm.SLOT_COUNT + 1),
                "99", "9" * 5000):
        monkeypatch.setenv("MCS_LLM_SLOT", bad)
        assert local_llm.request_slot() == local_llm.BACKGROUND_SLOT


def test_probe_rejects_schema_ignored_by_server():
    formats = []

    def send(endpoint, method, body, timeout, deadline=None):
        formats.append(body["response_format"]["type"])
        return 200, {}, json.dumps(_response('{"ok":true}')).encode()

    mode = local_llm.probe_format(local_llm.ENDPOINT, "synthetic",
                                  {"name": "synthetic"}, request_fn=send)
    assert mode == "object"
    assert formats == ["json_schema", "json_object"]


def test_probe_accepts_enforced_schema_marker():
    def send(endpoint, method, body, timeout, deadline=None):
        required = body["response_format"]["json_schema"]["schema"]
        assert required["required"] == ["probe"]
        return 200, {}, json.dumps(
            _response('{"probe":"schema"}')).encode()

    assert local_llm.probe_format(
        local_llm.ENDPOINT, "synthetic", {"name": "synthetic"},
        request_fn=send) == "schema"


@pytest.mark.parametrize(("schema", "content", "expected"), [
    ({"name": "synthetic"}, '{"probe":"schema"}', "object"),
    (None, '{"ok":true}', "plain"),
])
def test_probe_rejects_length_stops(schema, content, expected):
    def send(endpoint, method, body, timeout, deadline=None):
        finish = ("length" if schema is None
                  or body["response_format"]["type"] == "json_schema"
                  else "stop")
        text = content if finish == "length" else '{"ok":true}'
        return 200, {}, json.dumps(_response(text, finish=finish)).encode()

    assert local_llm.probe_format(
        local_llm.ENDPOINT, "synthetic", schema,
        request_fn=send) == expected


def test_extract_llm_call_pins_background_slot(monkeypatch):
    """The legacy extract path pins the same background slot."""
    monkeypatch.delenv("MCS_LLM_SLOT", raising=False)
    monkeypatch.setattr(extract_llm, "_FMT_MODE", "plain")
    seen = {}

    def fake_chat(prompt, **kw):
        seen.update(kw)
        return {"status": 200, "text": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(local_llm, "chat", fake_chat)
    extract_llm.llm_extract("test")
    assert seen["extra_payload"] == {"id_slot": local_llm.BACKGROUND_SLOT}


def test_semantic_llm_chat_rejects_parseable_length_truncation(
        monkeypatch):
    """C05: finish_reason=length stays incomplete even when the text
    happens to parse — llm_chat must return None, not the payload."""
    monkeypatch.setattr(
        local_llm, "bounded_request",
        _fake_request(_response('{"facts": []}', finish="length")))
    assert semantic.llm_chat("hello") is None


def test_resolve_defaults_and_config_override():
    assert local_llm.resolve(None) == (local_llm.ENDPOINT,
                                     local_llm.MODEL)
    assert local_llm.resolve({}) == (local_llm.ENDPOINT,
                                    local_llm.MODEL)
    assert local_llm.resolve({"local_llm": {
        "url": "http://127.0.0.1:9999/v1/chat/completions",
        "model": "Other-Model"}}) == (
        "http://127.0.0.1:9999/v1/chat/completions", "Other-Model")
    # malformed values keep the pin — a typo can't reroute PHI
    assert local_llm.resolve({"local_llm": {"url": 7, "model": " "}}) \
        == (local_llm.ENDPOINT, local_llm.MODEL)
    assert local_llm.resolve({"local_llm": "junk"}) == \
        (local_llm.ENDPOINT, local_llm.MODEL)


def test_probe_urls_follow_endpoint_authority():
    models, slots = local_llm.probe_urls(local_llm.ENDPOINT)
    assert models == "http://127.0.0.1:8080/v1/models"
    assert slots == "http://127.0.0.1:8080/slots"
    models, slots = local_llm.probe_urls(
        "http://localhost:1234/v1/chat/completions")
    assert models == "http://localhost:1234/v1/models"
    assert slots == "http://localhost:1234/slots"


def test_semantic_llm_chat_requests_json_object_after_probe(monkeypatch):
    """Every semantic prompt expects JSON: once the probe accepts
    json_object, the constraint rides every call."""
    monkeypatch.setattr(semantic, "_FMT_MODE", None)
    monkeypatch.setattr(semantic, "_FMT_TS", 0.0)
    bodies = []

    def send(endpoint, method, body, timeout, deadline=None):
        bodies.append(body)
        text = '{"ok": true}' if "Reply with" in body["messages"][0]["content"] \
            else '{"facts": []}'
        return 200, {}, json.dumps(_response(text)).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("抽出 JSON:") == '{"facts": []}'
    assert semantic._FMT_MODE == "object"
    assert bodies[-1]["response_format"] == {"type": "json_object"}
    assert bodies[-1]["id_slot"] == local_llm.BACKGROUND_SLOT
    # cached: a second call does not re-probe
    semantic.llm_chat("次 JSON:")
    assert sum("Reply with" in b["messages"][0]["content"] for b in bodies) == 1


def test_semantic_llm_chat_degrades_to_plain_when_format_rejected(monkeypatch):
    monkeypatch.setattr(semantic, "_FMT_MODE", "object")
    monkeypatch.setattr(semantic, "_FMT_TS", 10.0 ** 9)
    formats = []

    def send(endpoint, method, body, timeout, deadline=None):
        formats.append(body.get("response_format"))
        if body.get("response_format"):
            return 422, {}, b""
        return 200, {}, json.dumps(_response("{}")).encode()

    monkeypatch.setattr(local_llm, "bounded_request", send)
    assert semantic.llm_chat("p JSON:") == "{}"
    assert formats == [{"type": "json_object"}, None]
    assert semantic._FMT_MODE == "plain"
