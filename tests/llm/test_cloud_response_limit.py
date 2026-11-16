"""Synthetic checks that a long paid answer is read whole up to its own output bound."""
import io
import json
import time
from types import SimpleNamespace

import pytest

import anthropic_client as a
import bounded_http as b
import cloud_client
import openai_client


def _long_reply(size):
    # A long signed thinking block pushes the body past the default 256 KiB read bound.
    return json.dumps({"model": a.MODEL, "stop_reason": "end_turn",
                       "content": [{"type": "thinking", "thinking": "合" * size},
                                   {"type": "text", "text": '{"synthetic":true}'}],
                       "usage": {"input_tokens": 10, "output_tokens": 9000}},
                      ensure_ascii=False).encode()


def test_answer_over_the_default_bound_settles_from_its_usage():
    raw = _long_reply(120_000)
    assert b.MAX_RESPONSE_BYTES < len(raw) <= cloud_client.response_limit(16384)
    results = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (200, {}, raw))
    answer = client.complete("synthetic", deadline=time.monotonic() + 10, max_tokens=16384,
                             after_result=lambda r, meta, **_answer: results.append(meta))
    assert answer["text"] == '{"synthetic":true}'
    assert results[0]["error_kind"] is None and results[0]["usage"]["output_tokens"] == 9000


def test_answer_over_its_own_bound_is_still_rejected():
    raw = _long_reply(120_000)
    assert len(raw) > cloud_client.response_limit(1600)
    results = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (200, {}, raw))
    with pytest.raises(a.AnthropicError, match="protocol_error"):
        client.complete("synthetic", deadline=time.monotonic() + 10, max_tokens=1600,
                        after_result=lambda r, meta, **_answer: results.append(meta))
    assert results[0]["http_class"] == "http_200" and results[0]["usage"] is None


def test_limit_grows_with_output_and_stops_at_the_ceiling():
    assert cloud_client.response_limit(1) > b.MAX_RESPONSE_BYTES
    assert cloud_client.response_limit(32768) > cloud_client.response_limit(16384)
    assert cloud_client.response_limit(cloud_client.MAX_TOKENS) <= b.MAX_RESPONSE_BYTES_CEILING


@pytest.mark.parametrize("module", [a, openai_client])
def test_provider_requests_pass_the_bound_to_the_transport(monkeypatch, module):
    seen = []
    monkeypatch.setattr(module.bounded_http, "bounded_http_request",
                        lambda *args, **kwargs: seen.append(kwargs["max_bytes"]) or (200, {}, b"{}"))
    client = (a.AnthropicClient if module is a else openai_client.OpenAIClient)("synthetic")
    client.request("POST", {}, 1, max_bytes=b.MAX_RESPONSE_BYTES + 1)
    client.request("POST", {}, 1)
    assert seen == [b.MAX_RESPONSE_BYTES + 1, b.MAX_RESPONSE_BYTES]


@pytest.mark.parametrize("value", [b.MAX_RESPONSE_BYTES - 1, b.MAX_RESPONSE_BYTES_CEILING + 1,
                                   True, 1.5e6, "300000"])
def test_parent_rejects_an_invalid_bound_before_spawn(monkeypatch, value):
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="max_bytes_invalid"):
        b.bounded_http_request(b.ANTHROPIC_ENDPOINT, "POST", {}, 1, api_key="synthetic",
                               allowed_endpoints=(b.ANTHROPIC_ENDPOINT,), auth="anthropic",
                               max_bytes=value)


def _worker(monkeypatch, envelope):
    reads = []

    class Response:
        status = 200
        headers = {}

        def read(self, count):
            reads.append(count)
            return b"{}"

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            return Response()

    output = io.StringIO()
    monkeypatch.setattr(b.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode())))
    monkeypatch.setattr(b.sys, "stdout", output)
    monkeypatch.setattr(b, "no_proxy_opener", lambda redirect: Opener())
    b._http_worker_main()
    return json.loads(output.getvalue()), reads


def _envelope(**kw):
    return {"endpoint": b.ANTHROPIC_ENDPOINT, "method": "POST", "body": {}, "timeout": 1,
            "api_key": "synthetic", "auth": "anthropic", "allowed_endpoints": [b.ANTHROPIC_ENDPOINT], **kw}


def test_worker_reads_the_declared_bound_and_defaults_to_the_old_one(monkeypatch):
    result, reads = _worker(monkeypatch, _envelope(max_bytes=b.MAX_RESPONSE_BYTES * 2))
    assert result["ok"] and reads == [b.MAX_RESPONSE_BYTES * 2 + 1]
    result, reads = _worker(monkeypatch, _envelope())
    assert result["ok"] and reads == [b.MAX_RESPONSE_BYTES + 1]
    result, reads = _worker(monkeypatch, _envelope(max_bytes=b.MAX_RESPONSE_BYTES_CEILING + 1))
    assert result == {"ok": False, "error": "transport"} and reads == []
