"""Synthetic Jev key changes stop later attempts without sending old keys."""
from contextlib import closing
import json
import time

import pytest

import semantic_drain as drain
import semantic_jev as jev
from semantic_testkit import _cfg, _seeded


def _answer(body):
    return 200, {}, json.dumps({"model": jev.JEV_MODEL,
        "answers": {key: {"type": "noul", "noul": .9} for key in body["questions"]},
        "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


def _question():
    return {"q": jev.noul_question("synthetic", "yes", "no")}


@pytest.mark.parametrize("changed", [None, "synthetic-new-key"])
def test_key_removed_or_rotated_before_attempt_blocks_reservation(changed):
    key = ["synthetic-old-key"]
    sent, reserved = [], []
    client = jev.JevClient(api_key=key[0], key_current=lambda: key[0] == "synthetic-old-key",
                           post_fn=lambda *args: sent.append(True))
    key[0] = changed
    with pytest.raises(jev.JevError) as error:
        client.evaluate({}, _question(), time.monotonic() + 10,
                        reserve_fn=lambda *args: reserved.append(True))
    assert (error.value.kind, error.value.detail, error.value.retryable) == ("no_api_key", "jev_key_changed", False)
    assert not sent and not reserved and client.requests_made == 0
    assert "synthetic-old-key" not in str(error.value) and "synthetic-new-key" not in str(error.value)


@pytest.mark.parametrize("changed", [None, "synthetic-new-key"])
def test_key_change_during_reservation_keeps_reservation_and_blocks_post(changed):
    key = ["synthetic-old-key"]
    sent, reserved = [], []
    def reserve(*args):
        reserved.append(True)
        key[0] = changed
    client = jev.JevClient(api_key=key[0], key_current=lambda: key[0] == "synthetic-old-key",
                           post_fn=lambda *args: sent.append(True))
    with pytest.raises(jev.JevError, match="no_api_key"):
        client.evaluate({}, _question(), time.monotonic() + 10, reserve_fn=reserve)
    assert reserved == [True] and not sent and client.requests_made == 0


def test_key_rotation_after_retryable_response_blocks_retry(monkeypatch):
    key = ["synthetic-old-key"]
    sent, reserved = [], []
    monkeypatch.setattr(jev.time, "sleep", lambda seconds: None)
    def post(*args):
        sent.append(True)
        key[0] = "synthetic-new-key"
        return 429, {"Retry-After": "0"}, b"{}"
    client = jev.JevClient(api_key=key[0], key_current=lambda: key[0] == "synthetic-old-key",
                           post_fn=post, max_attempts=3)
    with pytest.raises(jev.JevError, match="no_api_key"):
        client.evaluate({}, _question(), time.monotonic() + 10,
                        reserve_fn=lambda *args: reserved.append(True))
    assert sent == [True] and reserved == [True] and client.requests_made == 1


def test_key_change_inside_transport_blocks_actual_wire(monkeypatch):
    key = ["synthetic-old-key"]
    sent, reserved = [], []
    def no_wire(*args, **kwargs):
        sent.append(True)
        return 200, {}, b"{}"
    monkeypatch.setattr(jev.bounded_http, "bounded_http_request", no_wire)
    def transport(request, *args, **kwargs):
        key[0] = None
        return request(*args, **kwargs)
    client = jev.JevClient(api_key=key[0], key_current=lambda: key[0] == "synthetic-old-key", max_attempts=1)
    with pytest.raises(jev.JevError, match="no_api_key"):
        client.evaluate({}, _question(), time.monotonic() + 10,
                        reserve_fn=lambda *args: reserved.append(True), transport=transport)
    assert reserved == [True] and not sent and client.requests_made == 0


def test_valid_or_explicit_unmanaged_client_keeps_existing_behavior():
    sent = []
    def post(body, timeout):
        sent.append(True)
        return _answer(body)
    for client in (jev.JevClient(api_key="synthetic", key_current=lambda: True, post_fn=post),
                   jev.JevClient(api_key="synthetic", post_fn=post)):
        result = client.evaluate({}, _question(), time.monotonic() + 10)
        assert result["answers"] and client.requests_made == 1
    assert len(sent) == 2


@pytest.mark.parametrize("guard", [lambda: None, lambda: 1, lambda: (_ for _ in ()).throw(ValueError("synthetic-secret"))])
def test_key_guard_failure_is_closed_without_leaking_error(guard):
    client = jev.JevClient(api_key="synthetic-key", key_current=guard,
                           post_fn=lambda *args: pytest.fail("must not send"))
    with pytest.raises(jev.JevError) as error:
        client.evaluate({}, _question(), time.monotonic() + 10)
    assert error.value.detail == "jev_key_changed"
    assert "synthetic-secret" not in repr(error.value) and client.requests_made == 0


def test_normal_drain_binds_captured_key_to_current_key_check(tmp_path, monkeypatch):
    key = ["synthetic-old-key"]
    captured = {}
    class Constructed(Exception):
        pass
    def capture(**kwargs):
        captured.update(kwargs)
        raise Constructed()
    monkeypatch.setattr(jev, "api_key", lambda *args: key[0])
    monkeypatch.setattr(jev, "JevClient", capture)
    monkeypatch.setattr(drain.v4, "invalidate_projections", lambda *args, **kwargs: None)
    monkeypatch.setattr(drain.v4, "reproject_stale", lambda *args, **kwargs: {})
    monkeypatch.setattr(drain, "reprocess_step", lambda *args, **kwargs: {})
    monkeypatch.setattr(drain.llm_availability, "disk_free_mb", lambda *args: None)
    monkeypatch.setattr(drain.llm_availability, "circuit_open_s", lambda *args: None)
    with closing(_seeded(tmp_path)) as ledger:
        with pytest.raises(Constructed):
            drain.run_due(ledger, _cfg("shadow", budget=50), {"errors": []},
                          time.monotonic() + 60, llm_fn=lambda prompt: None)
    assert captured["api_key"] == "synthetic-old-key"
    assert captured["key_current"]() is True
    key[0] = "synthetic-new-key"
    assert captured["key_current"]() is False
    key[0] = None
    assert captured["key_current"]() is False
