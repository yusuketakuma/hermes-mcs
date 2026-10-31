"""Synthetic tests cannot spawn workers for live cloud provider hosts."""
import subprocess

import bounded_http
import mcs_test_guard as guard
import openai_client
import pytest


@pytest.mark.parametrize("endpoint", [
    bounded_http.ANTHROPIC_ENDPOINT,
    "https://api.anthropic.com/v1/models",
    "https://API.ANTHROPIC.COM:443/v1/messages",
    "https://api.anthropic.com:8443/synthetic",
    openai_client.ENDPOINT,
    "https://api.openai.com/v1/models",
    "https://API.OPENAI.COM:443/v1/responses",
    "http://api.openai.com:80/synthetic",
])
def test_live_cloud_host_is_blocked_before_any_spawn(monkeypatch, endpoint):
    calls = []
    monkeypatch.setattr(guard, "_ORIG_HTTP_REQUEST", lambda *args, **kwargs: calls.append(True))
    assert subprocess.Popen is guard._guarded_popen
    with pytest.raises(RuntimeError, match="endpoints are disabled"):
        bounded_http.bounded_http_request(endpoint, "POST", {}, 1, api_key="synthetic")
    assert not calls


def test_synthetic_loopback_uses_existing_transport_stub(monkeypatch):
    endpoint = next(f"http://127.0.0.1:{port}/synthetic" for port in range(45100, 45200)
                    if guard._live_authority(f"http://127.0.0.1:{port}") not in guard._LIVE_AUTHORITIES)
    sentinel = object()
    calls = []
    monkeypatch.setattr(guard, "_ORIG_HTTP_REQUEST", lambda *args, **kwargs: calls.append(args) or sentinel)
    assert subprocess.Popen is guard._guarded_popen
    assert bounded_http.bounded_http_request(endpoint, "GET", None, 1) is sentinel
    assert calls[0][0] == endpoint


def test_live_endpoint_allows_existing_explicit_synthetic_popen_stub(monkeypatch):
    sentinel = object()
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no process is spawned"))
    monkeypatch.setattr(guard, "_ORIG_HTTP_REQUEST", lambda *args, **kwargs: calls.append(args) or sentinel)
    assert bounded_http.bounded_http_request(openai_client.ENDPOINT, "POST", {}, 1) is sentinel
    assert calls[0][0] == openai_client.ENDPOINT
