"""Regression: token expiry survives host sleep; token-fetch failures provably sent nothing."""
import io
import json
from types import SimpleNamespace

import pytest
from adapters.lineworks import __main__ as cli
from adapters.lineworks import client as module
from adapters.lineworks.client import ClientError
from adapters.lineworks.delivery import failure
from lineworks_testkit import CONFIG, SCOPE, SECRET, raw, world
from lineworks_client_testkit import TOKEN, Wire, client


def test_wall_clock_expiry_refreshes_when_monotonic_stalled_in_sleep():
    wall, mono = [1_700_000_000.0], [100.0]
    wire = Wire([(200, TOKEN), (201, b""), (200, TOKEN), (201, b"")])
    api = client(wire, clock=lambda: wall[0], monotonic=lambda: mono[0])
    api.send_message({"type": "text", "text": "x"}, channel_id="room")
    # The host slept for two hours: wall clock advanced, monotonic did not.
    wall[0] += 7200
    api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert sum(c[1] == module.AUTH_URL for c in wire.calls) == 2


def test_cached_token_is_reused_while_both_clocks_are_inside_lifetime():
    wall, mono = [1_700_000_000.0], [100.0]
    wire = Wire([(200, TOKEN), (201, b""), (201, b"")])
    api = client(wire, clock=lambda: wall[0], monotonic=lambda: mono[0])
    api.send_message({"type": "text", "text": "x"}, channel_id="room")
    wall[0] += 3000
    mono[0] += 3000
    api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert sum(c[1] == module.AUTH_URL for c in wire.calls) == 1


def _auth_unreachable():
    def wire(method, url, headers, body, timeout):
        if url == module.AUTH_URL:
            raise OSError("synthetic network down")
        raise AssertionError("message endpoint must not be contacted")
    return wire


@pytest.mark.parametrize("responses", [
    "transport",                       # DNS/connect failure on the token endpoint
    [(500, {"error": "synthetic"})],   # token endpoint 5xx
    [(200, b"not-json")],              # garbled token response
])
def test_token_fetch_failure_is_not_sent(responses):
    wire = _auth_unreachable() if responses == "transport" else Wire(responses)
    api = client(wire)
    with pytest.raises(ClientError) as caught:
        api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert caught.value.pre_send is True
    assert failure(caught.value)["result"] == "not_sent"
    if isinstance(wire, Wire):
        assert all(c[1] == module.AUTH_URL for c in wire.calls)


def test_message_post_transport_failure_stays_unknown():
    calls = []

    def wire(method, url, headers, body, timeout):
        calls.append(url)
        if url == module.AUTH_URL:
            return 200, {}, json.dumps(TOKEN).encode()
        raise OSError("synthetic reset after send")
    api = client(wire)
    with pytest.raises(ClientError) as caught:
        api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert getattr(caught.value, "pre_send", False) is False
    assert failure(caught.value)["result"] == "unknown"


def test_cli_send_exits_tempfail_when_token_endpoint_unreachable(monkeypatch, tmp_path, capsys):
    world(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    api = client(_auth_unreachable())
    monkeypatch.setattr(cli, "load_credentials", lambda *args: (api, SECRET))
    monkeypatch.setattr(cli.sys, "stdin",
                        SimpleNamespace(buffer=io.BytesIO(raw({"text": "合成通知"}))))
    code = cli.main(["send", "--root", str(tmp_path), "--to", "lineworks:" + SCOPE["channel_id"]])
    assert code == 75
    assert capsys.readouterr().err.strip().splitlines()[-1] == "transport_unknown"


def test_token_endpoint_5xx_reports_a_transient_code():
    from notify_flush import _TRANSIENT_SEND_CODES
    api = client(Wire([(503, {"error": "synthetic"})]))
    with pytest.raises(ClientError) as caught:
        api.send_message({"type": "text", "text": "x"}, channel_id="room")
    out = failure(caught.value)
    assert out == {"result": "not_sent", "error_code": "lineworks_token_unavailable"}
    assert out["error_code"] in _TRANSIENT_SEND_CODES


def test_token_endpoint_4xx_keeps_its_permanent_code():
    api = client(Wire([(401, {"error": "synthetic"})]))
    with pytest.raises(ClientError) as caught:
        api.send_message({"type": "text", "text": "x"}, channel_id="room")
    assert failure(caught.value)["error_code"] == "http_error"
