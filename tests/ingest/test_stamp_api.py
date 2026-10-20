"""Own-stamp wire attempts are explicit, session-bound and never retried."""
import base64
from urllib.parse import urlsplit

import pytest

from mcs_adapter import MCSAdapter
from mcs_contract import API, MCSError, SessionExpired


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def request(envelope, **bounds):
        calls.append((envelope, bounds))
        return {"status": 200, "body": base64.b64encode(b"not JSON").decode(),
                "headers": {}}

    adapter = MCSAdapter(worker=request)
    adapter._token = "synthetic-session"

    def forbidden(*args, **kwargs):
        pytest.fail("stamp mutation attempted login, session probe or retry sleep")

    for name in ("bootstrap_token", "_probe_session_ok", "_sleep_bounded"):
        monkeypatch.setattr(adapter, name, forbidden)
    return adapter, calls


@pytest.mark.parametrize("kind", ["good", "viewed", "accepted", "thanked", "completed"])
def test_add_uses_exact_form_wire_once(wire, kind):
    adapter, calls = wire
    assert adapter.write_reaction(123, kind) == 200
    assert len(calls) == 1
    request, bounds = calls[0]
    assert request["operation"] == "api" and request["method"] == "POST"
    assert request["url"] == API + "/messages/123/reactions?no_extend_session=1"
    assert base64.b64decode(request["data"]) == ("reaction_type=" + kind).encode()
    assert request["headers"] == {
        "Authorization": "Bearer synthetic-session", "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
    assert bounds["timeout"] > 0


def test_cancel_has_no_body_or_type_header(wire):
    adapter, calls = wire
    assert adapter.write_reaction(123, cancel=True) == 200
    assert len(calls) == 1
    request = calls[0][0]
    assert request["url"] == API + "/messages/123/reactions/delete?no_extend_session=1"
    assert request["method"] == "POST" and request["data"] is None
    assert "Content-Type" not in request["headers"]
    assert "reaction_type" not in urlsplit(request["url"]).query


@pytest.mark.parametrize("message_id,kind,cancel", [
    (True, "viewed", False), (0, "viewed", False), (-1, "viewed", False),
    (1.0, "viewed", False), ("1", "viewed", False), (2**63, "viewed", False),
    (1, None, False), (1, "all", False), (1, "future_kind", False),
    (1, [], False), (1, True, False), (1, "viewed", True), (1, None, 1),
])
def test_bad_arguments_never_dispatch(wire, message_id, kind, cancel):
    adapter, calls = wire
    with pytest.raises(ValueError, match="invalid reaction arguments"):
        adapter.write_reaction(message_id, kind, cancel=cancel)
    assert calls == []


@pytest.mark.parametrize("token", [None, "", " ", True, 123, "bad\r\ntoken"])
def test_missing_or_invalid_loaded_session_never_bootstraps(wire, token):
    adapter, calls = wire
    adapter._token = token
    with pytest.raises(MCSError) as caught:
        adapter.write_reaction(1, "viewed")
    assert caught.value.kind == "no_token" and caught.value.retryable is False
    assert calls == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
def test_http_failure_is_single_attempt_sanitized_and_not_retryable(wire, status):
    adapter, calls = wire
    original = adapter._worker

    def denied(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, "status": status,
                "body": base64.b64encode(b"PRIVATE_RESPONSE_CANARY").decode()}

    adapter._worker = denied
    with pytest.raises(MCSError) as caught:
        adapter.write_reaction(1, "viewed")
    assert len(calls) == 1
    assert caught.value.status == status and caught.value.retryable is False
    assert caught.value.detail == "" and "PRIVATE_RESPONSE_CANARY" not in str(caught.value)
    assert isinstance(caught.value, SessionExpired) is (status in (401, 403))


@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE_TIMEOUT"),
                                    MCSError("network_error", "PRIVATE_NETWORK", retryable=True),
                                    KeyboardInterrupt("synthetic interruption")])
def test_possible_effect_failure_never_retries(wire, failure):
    adapter, calls = wire

    def interrupted(envelope, **bounds):
        calls.append((envelope, bounds))
        raise failure

    adapter._worker = interrupted
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as caught:
            adapter.write_reaction(1, "viewed")
        assert caught.value is failure
    else:
        with pytest.raises(MCSError) as caught:
            adapter.write_reaction(1, "viewed")
        assert caught.value.kind == "network_error" and caught.value.retryable is False
        assert caught.value.detail == "" and "PRIVATE_" not in str(caught.value)
    assert len(calls) == 1


def test_expired_deadline_cannot_dispatch(wire):
    adapter, calls = wire
    adapter.set_deadline(0)
    with pytest.raises(MCSError) as caught:
        adapter.write_reaction(1, "viewed")
    assert caught.value.kind == "deadline_exceeded" and caught.value.retryable is False
    assert calls == []
